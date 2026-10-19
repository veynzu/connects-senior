"""Servicio de recordatorios (R1-R6, RF-27 a RF-30).

No conoce FastAPI: lanza excepciones de dominio que el router traduce. Reglas:
- Permisos (D-R2-7, D-R2-8): en rutas con {id} el recordatorio se busca por id, se
  resuelve su adulto y can_access_adult falso es 404 sin revelar existencia (P18);
  el cuidador necesita manage_reminders para R2-R4 (403); R5 y R6 solo el adulto
  mayor sobre lo propio (ajeno -> 404).
- Estados y repetición (D-R2-11): R5, R6, R3 y el planificador (RF-28) cambian el
  estado; la siguiente ocurrencia de un recordatorio diario o semanal se crea una
  sola vez por registro, al pasar a confirmado u omitido.
- Concurrencia (D-R2-23): _LOCK protege cada secuencia leer -> modificar -> escribir
  de R3, R4, R5, R6 y del recorrido del planificador; el planificador además escribe
  con update_if_status. Dentro del lock no se hace red: las push salen después.
"""
import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.security import can_access_adult
from app.models.auth import AuthenticatedUser
from app.models.enums import ReminderRepeat, ReminderStatus, Role
from app.models.reminders import (
    ReminderCreateRequest,
    ReminderRecord,
    ReminderResponse,
    ReminderUpdateRequest,
)
from app.repositories.device_repository import DeviceRepository
from app.repositories.reminder_repository import ReminderRepository
from app.repositories.user_repository import UserRepository
from app.services import push_service


REMINDER_PUSH_TITLE = "Recordatorio"
REMINDER_PUSH_TYPE = "reminder"

_REPEAT_INTERVALS = {
    ReminderRepeat.DIARIA: timedelta(days=1),
    ReminderRepeat.SEMANAL: timedelta(days=7),
}

_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


class ReminderNotFoundError(Exception):
    """El recordatorio no existe o pertenece a un adulto fuera del alcance del usuario."""


class ReminderPermissionDeniedError(Exception):
    """El usuario no tiene permiso para esta acción sobre el recordatorio."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _to_response(record: ReminderRecord) -> ReminderResponse:
    return ReminderResponse(
        id=record.id,
        kind=record.kind,
        description=record.description,
        scheduled_at=record.scheduled_at,
        repeat=record.repeat,
        status=record.status,
    )


def _ensure_can_manage(
    user: AuthenticatedUser, adult_uid: str, users: UserRepository
) -> None:
    """Permiso específico de R2-R4 (D-R2-8): el cuidador necesita manage_reminders.

    adulto_mayor sobre lo propio y familiar vinculado no consultan permissions; el
    llamante ya pasó can_access_adult.
    """
    if user.role != Role.CUIDADOR:
        return
    link = users.link_of(user.uid, adult_uid)
    if link is None or link.permissions is None or not link.permissions.manage_reminders:
        raise ReminderPermissionDeniedError("No tienes permiso para esta acción.")


def _accessible_record(
    user: AuthenticatedUser,
    reminder_id: str,
    reminders: ReminderRepository,
    users: UserRepository,
) -> ReminderRecord:
    """Recordatorio por id dentro del alcance del usuario (404 si no, P18)."""
    record = reminders.get(reminder_id)
    if record is None or not can_access_adult(user, record.adult_uid, users):
        raise ReminderNotFoundError("El recordatorio no existe.")
    return record


def _own_record(
    user: AuthenticatedUser, reminder_id: str, reminders: ReminderRepository
) -> ReminderRecord:
    """Recordatorio propio del adulto mayor (R5, R6); cualquier otro rol -> 403."""
    if user.role != Role.ADULTO_MAYOR:
        raise ReminderPermissionDeniedError("No tienes permiso para esta acción.")
    record = reminders.get(reminder_id)
    if record is None or record.adult_uid != user.uid:
        raise ReminderNotFoundError("El recordatorio no existe.")
    return record


def next_occurrence(
    record: ReminderRecord, now: datetime | None = None
) -> ReminderRecord | None:
    """Siguiente ocurrencia de un recordatorio diario o semanal (D-R2-11).

    None si repeat es ninguna o si ya se creó (next_created). Si no, un registro
    nuevo (id nuevo, pendiente, sin notificar) con scheduled_at = scheduled_at +
    k * intervalo para el menor k >= 1 que deja la fecha en el futuro respecto a now.
    """
    interval = _REPEAT_INTERVALS.get(record.repeat)
    if interval is None or record.next_created:
        return None
    current_time = now or _now()
    steps = max(1, (current_time - record.scheduled_at) // interval + 1)
    return ReminderRecord(
        id=str(uuid4()),
        adult_uid=record.adult_uid,
        kind=record.kind,
        description=record.description,
        scheduled_at=record.scheduled_at + steps * interval,
        repeat=record.repeat,
        status=ReminderStatus.PENDIENTE,
        created_at=current_time,
        notified_at=None,
        next_created=False,
    )


def list_reminders(
    adult_uid: str, reminders: ReminderRepository
) -> list[ReminderResponse]:
    """R1: recordatorios del adulto por scheduled_at ascendente (RF-30)."""
    return [_to_response(record) for record in reminders.list_for_adult(adult_uid)]


def create_reminder(
    user: AuthenticatedUser,
    adult_uid: str,
    payload: ReminderCreateRequest,
    reminders: ReminderRepository,
    users: UserRepository,
    now: datetime | None = None,
) -> ReminderResponse:
    """R2: crea un recordatorio pendiente para el adulto ya resuelto por el router."""
    # Defensa en profundidad: el router ya resolvió el adulto con resolve_adult_scope,
    # que responde 403 ante un adulto no vinculado en las colecciones.
    if not can_access_adult(user, adult_uid, users):
        raise ReminderPermissionDeniedError("No tienes permiso para esta acción.")
    _ensure_can_manage(user, adult_uid, users)

    record = reminders.add(
        ReminderRecord(
            id=str(uuid4()),
            adult_uid=adult_uid,
            kind=payload.kind,
            description=payload.description,
            scheduled_at=payload.scheduled_at,
            repeat=payload.repeat,
            status=ReminderStatus.PENDIENTE,
            created_at=now or _now(),
        )
    )
    return _to_response(record)


def update_reminder(
    user: AuthenticatedUser,
    reminder_id: str,
    payload: ReminderUpdateRequest,
    reminders: ReminderRepository,
    users: UserRepository,
) -> ReminderResponse:
    """R3: aplica solo los campos enviados.

    Un scheduled_at distinto del actual vuelve el recordatorio a pendiente y sin
    notificar, para que el planificador lo avise a la nueva hora (D-R2-11).
    """
    changes = payload.model_dump(exclude_unset=True)
    with _LOCK:
        current = _accessible_record(user, reminder_id, reminders, users)
        _ensure_can_manage(user, current.adult_uid, users)

        updated = replace(current, **changes)
        if "scheduled_at" in changes and changes["scheduled_at"] != current.scheduled_at:
            updated = replace(updated, status=ReminderStatus.PENDIENTE, notified_at=None)
        saved = reminders.update(updated)
    if saved is None:
        raise ReminderNotFoundError("El recordatorio no existe.")
    return _to_response(saved)


def delete_reminder(
    user: AuthenticatedUser,
    reminder_id: str,
    reminders: ReminderRepository,
    users: UserRepository,
) -> None:
    """R4: borra el recordatorio."""
    with _LOCK:
        current = _accessible_record(user, reminder_id, reminders, users)
        _ensure_can_manage(user, current.adult_uid, users)
        deleted = reminders.delete(current.id)
    if not deleted:
        raise ReminderNotFoundError("El recordatorio no existe.")


def confirm_reminder(
    user: AuthenticatedUser,
    reminder_id: str,
    reminders: ReminderRepository,
    now: datetime | None = None,
) -> ReminderResponse:
    """R5 (RF-29): el adulto mayor confirma su recordatorio.

    pendiente -> confirmado (y siguiente ocurrencia si toca); confirmado -> sin
    cambios; omitido -> confirmado sin crear otra ocurrencia (se creó al omitir).
    """
    current_time = now or _now()
    with _LOCK:
        current = _own_record(user, reminder_id, reminders)
        if current.status == ReminderStatus.CONFIRMADO:
            return _to_response(current)

        following = next_occurrence(current, current_time)
        updated = replace(
            current,
            status=ReminderStatus.CONFIRMADO,
            next_created=current.next_created or following is not None,
        )
        saved = reminders.update(updated)
        if saved is not None and following is not None:
            reminders.add(following)
    if saved is None:
        raise ReminderNotFoundError("El recordatorio no existe.")
    return _to_response(saved)


def snooze_reminder(
    user: AuthenticatedUser,
    reminder_id: str,
    minutes: int,
    reminders: ReminderRepository,
) -> ReminderResponse:
    """R6 (RF-29): pospone el recordatorio minutes minutos sobre scheduled_at.

    En cualquier estado: vuelve a pendiente y sin notificar; next_created no cambia.
    """
    with _LOCK:
        current = _own_record(user, reminder_id, reminders)
        updated = replace(
            current,
            scheduled_at=current.scheduled_at + timedelta(minutes=minutes),
            status=ReminderStatus.PENDIENTE,
            notified_at=None,
        )
        saved = reminders.update(updated)
    if saved is None:
        raise ReminderNotFoundError("El recordatorio no existe.")
    return _to_response(saved)


def _push_data(record: ReminderRecord) -> dict[str, str]:
    return {
        "type": REMINDER_PUSH_TYPE,
        "reminder_id": record.id,
        "kind": record.kind.value,
        "scheduled_at": record.scheduled_at.isoformat(),
    }


def process_due_reminders(
    reminders: ReminderRepository,
    devices: DeviceRepository,
    missed_minutes: int,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Recorrido del planificador (RF-28, D-R2-11, D-R2-23).

    (a) Pendiente, scheduled_at <= now y sin notificar: se marca notified_at = now y
        se envía la push al adulto. La marca se guarda siempre, aunque el adulto no
        tenga dispositivos o FCM falle.
    (b) Pendiente y notificado hace missed_minutes o más: pasa a omitido (la ventana
        se cuenta desde la push) y se crea la siguiente ocurrencia si toca.
    Toda escritura usa update_if_status(..., pendiente): si R5, R6 o R3 cambiaron el
    estado entre medias, no se envía ni se marca nada.

    Devuelve (recordatorios notificados, recordatorios omitidos).
    """
    current_time = now or _now()
    missed_window = timedelta(minutes=missed_minutes)
    to_notify: list[ReminderRecord] = []
    omitted = 0

    with _LOCK:
        for record in reminders.list_pending():
            if record.status != ReminderStatus.PENDIENTE:
                continue
            if record.notified_at is None:
                if record.scheduled_at > current_time:
                    continue
                marked = reminders.update_if_status(
                    replace(record, notified_at=current_time),
                    ReminderStatus.PENDIENTE,
                )
                if marked is not None:
                    to_notify.append(marked)
                continue

            if current_time - record.notified_at < missed_window:
                continue
            following = next_occurrence(record, current_time)
            missed = reminders.update_if_status(
                replace(
                    record,
                    status=ReminderStatus.OMITIDO,
                    next_created=record.next_created or following is not None,
                ),
                ReminderStatus.PENDIENTE,
            )
            if missed is None:
                continue
            omitted += 1
            if following is not None:
                reminders.add(following)

    # Fuera del lock: notify_users hace red y nunca lanza.
    for record in to_notify:
        push_service.notify_users(
            [record.adult_uid],
            REMINDER_PUSH_TITLE,
            record.description,
            _push_data(record),
            devices,
        )
    if to_notify or omitted:
        logger.info(
            "Recordatorios: %d notificado(s), %d omitido(s)", len(to_notify), omitted
        )
    return len(to_notify), omitted
