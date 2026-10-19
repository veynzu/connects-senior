"""Servicio de Pedir ayuda (H1-H5; RF-13 a RF-17, RF-23 a RF-26; ADR-10; D-R2-10).

No conoce FastAPI: lanza excepciones de dominio que el router traduce.

- H1: el adulto mayor crea la solicitud (con o sin ubicación) y se envía la push a
  los cuidadores con receive_alerts y a los contactos de confianza (help_alerts)
  vinculados a un uid verificado; si no hay contactos de confianza o ningún
  destinatario tiene dispositivo, también al familiar responsable (RF-26, P9).
- H3: solo quien puede acceder al adulto (si no, no existe: P18).
- H4 y H5: familiar vinculado o cuidador con receive_alerts (P18, D-R2-8).
- Escalamiento (RF-24, P10, P21): reenvía las pendientes cada
  ALERT_ESCALATION_SECONDS, como máximo MAX_ESCALATION_ROUNDS veces, incluyendo
  al familiar responsable.

Concurrencia (D-R2-23): cada secuencia leer -> modificar -> escribir de H4, H5 y
del escalamiento va dentro de _LOCK; dentro del lock no se hace red (las push se
envían fuera, con los datos ya calculados). Una push que falla nunca hace fallar
la acción principal (push_service.notify_users nunca lanza).
"""
import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.security import can_access_adult
from app.models.alerts import (
    AlertCreateRequest,
    AlertCreateResponse,
    AlertDetail,
    AlertRecord,
    AlertSummary,
)
from app.models.auth import AuthenticatedUser
from app.models.enums import AlertStatus, Role
from app.repositories.alert_repository import AlertRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.user_repository import UserRepository
from app.services import push_service


MAX_ESCALATION_ROUNDS = 3
ALERT_PUSH_TITLE = "Solicitud de ayuda"
_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


class AlertNotFoundError(Exception):
    """La solicitud no existe o pertenece a un adulto fuera del alcance del usuario."""


class AlertPermissionDeniedError(Exception):
    """El usuario puede ver la solicitud pero no tiene permiso para esta acción."""


class AlertAlreadyClosedError(Exception):
    """La solicitud ya fue atendida y no puede volver a confirmarse con H4."""


class AlertLocationIncompleteError(Exception):
    """Llegó solo una de las dos coordenadas (latitude y longitude van juntas)."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _location_available(record: AlertRecord) -> bool:
    return record.latitude is not None and record.longitude is not None


def _adult_name(adult: AuthenticatedUser, users: UserRepository) -> str:
    """Nombre del perfil si existe; si no, display_name del token; si no, el uid."""
    profile = users.get_profile(adult.uid)
    if profile is not None and profile.full_name:
        return profile.full_name
    return adult.display_name or adult.uid


def _to_summary(record: AlertRecord) -> AlertSummary:
    return AlertSummary(
        id=record.id,
        adult_name=record.adult_name,
        category=record.category,
        status=record.status,
        created_at=record.created_at,
        location_available=_location_available(record),
    )


def _to_detail(record: AlertRecord) -> AlertDetail:
    return AlertDetail(
        id=record.id,
        adult_name=record.adult_name,
        category=record.category,
        status=record.status,
        created_at=record.created_at,
        location_available=_location_available(record),
        latitude=record.latitude,
        longitude=record.longitude,
    )


def _push_body(record: AlertRecord) -> str:
    if _location_available(record):
        return f"{record.adult_name} pidió ayuda. Ubicación disponible."
    return f"{record.adult_name} pidió ayuda. Ubicación no disponible."


def _push_data(record: AlertRecord) -> dict[str, str]:
    """data de la push: todos los valores son str (FCM solo acepta texto)."""
    return {
        "type": "alert",
        "alert_id": record.id,
        "adult_id": record.adult_uid,
        "adult_name": record.adult_name,
        "category": record.category.value if record.category is not None else "",
        "location_available": "true" if _location_available(record) else "false",
    }


def alert_recipients(
    record: AlertRecord,
    contacts: ContactRepository,
    users: UserRepository,
) -> list[str]:
    """Destinatarios de la push de una solicitud, sin duplicados y en orden estable.

    Cuidadores vinculados con receive_alerts (un vínculo sin permisos cuenta como
    False, D-R2-8) más los uids verificados de los contactos con help_alerts
    (D-R2-3, P9). El propio adulto nunca es destinatario de su solicitud.
    """
    caregiver_uids = [
        link.user_uid
        for link in users.caregivers_of(record.adult_uid)
        if link.permissions is not None and link.permissions.receive_alerts
    ]
    contact_uids = [
        push_service.linked_uid_for_contact(contact, users)
        for contact in contacts.list_for_adult(record.adult_uid)
        if contact.permissions.help_alerts
    ]
    return [
        uid
        for uid in dict.fromkeys([*caregiver_uids, *contact_uids])
        if uid is not None and uid != record.adult_uid
    ]


def create_alert(
    adult: AuthenticatedUser,
    payload: AlertCreateRequest | None,
    alerts: AlertRepository,
    contacts: ContactRepository,
    users: UserRepository,
    devices: DeviceRepository,
    now: datetime | None = None,
) -> AlertCreateResponse:
    """H1: crea la solicitud del adulto del token y envía las push (RF-13 a RF-15).

    payload None equivale a un cuerpo vacío (todo opcional).
    """
    if adult.role != Role.ADULTO_MAYOR:
        raise AlertPermissionDeniedError("Solo el adulto mayor puede pedir ayuda.")
    if payload is None:
        payload = AlertCreateRequest()
    if (payload.latitude is None) != (payload.longitude is None):
        raise AlertLocationIncompleteError(
            "Datos inválidos: latitude y longitude deben enviarse juntas"
        )

    created_at = now or _now()
    has_trusted_contacts = any(
        contact.permissions.help_alerts
        for contact in contacts.list_for_adult(adult.uid)
    )
    record = alerts.add(
        AlertRecord(
            id=str(uuid4()),
            adult_uid=adult.uid,
            adult_name=_adult_name(adult, users),
            category=payload.category,
            status=AlertStatus.PENDIENTE,
            created_at=created_at,
            latitude=payload.latitude,
            longitude=payload.longitude,
            has_trusted_contacts=has_trusted_contacts,
            last_notified_at=created_at,
            notification_round=0,
        )
    )

    # Siempre con el registro que devolvió add() (D-R2-17).
    body = _push_body(record)
    data = _push_data(record)
    recipients = alert_recipients(record, contacts, users)
    if recipients:
        push_service.notify_users(recipients, ALERT_PUSH_TITLE, body, data, devices)
    if not record.has_trusted_contacts or not push_service.reachable_uids(
        recipients, devices
    ):
        # RF-26 y regla derivada P9: sin contactos de confianza o sin ningún
        # destinatario alcanzable, también el familiar responsable.
        family_uid = users.responsible_family_of(record.adult_uid)
        if family_uid is not None and family_uid not in recipients:
            push_service.notify_users(
                [family_uid], ALERT_PUSH_TITLE, body, data, devices
            )

    return AlertCreateResponse(
        id=record.id,
        status=record.status,
        created_at=record.created_at,
        location_available=_location_available(record),
        has_trusted_contacts=record.has_trusted_contacts,
    )


def list_alerts(adult_uid: str, alerts: AlertRepository) -> list[AlertSummary]:
    """H2: solicitudes del adulto, de la más reciente a la más antigua.

    El alcance (adulto implícito y vínculo) lo resuelve el router con
    resolve_adult_scope antes de llamar aquí.
    """
    return [_to_summary(record) for record in alerts.list_for_adult(adult_uid)]


def _get_accessible(
    user: AuthenticatedUser,
    alert_id: str,
    alerts: AlertRepository,
    users: UserRepository,
) -> AlertRecord:
    """La solicitud si el usuario puede acceder a su adulto; si no, no existe (P18)."""
    record = alerts.get(alert_id)
    if record is None or not can_access_adult(user, record.adult_uid, users):
        raise AlertNotFoundError("La solicitud no existe.")
    return record


def _ensure_can_attend(
    user: AuthenticatedUser, adult_uid: str, users: UserRepository
) -> None:
    """H4 y H5: familiar vinculado o cuidador con receive_alerts (P18, D-R2-8)."""
    if user.role == Role.FAMILIAR:
        return
    if user.role == Role.CUIDADOR:
        link = users.link_of(user.uid, adult_uid)
        if (
            link is not None
            and link.permissions is not None
            and link.permissions.receive_alerts
        ):
            return
    raise AlertPermissionDeniedError("No puede atender esta solicitud.")


def get_alert(
    user: AuthenticatedUser,
    alert_id: str,
    alerts: AlertRepository,
    users: UserRepository,
) -> AlertDetail:
    """H3: una solicitud, con latitude y longitude si se compartieron."""
    return _to_detail(_get_accessible(user, alert_id, alerts, users))


def acknowledge_alert(
    user: AuthenticatedUser,
    alert_id: str,
    alerts: AlertRepository,
    users: UserRepository,
) -> AlertDetail:
    """H4: pendiente -> en_atencion; en_atencion es idempotente; atendida -> error."""
    with _LOCK:
        record = _get_accessible(user, alert_id, alerts, users)
        _ensure_can_attend(user, record.adult_uid, users)
        if record.status == AlertStatus.ATENDIDA:
            raise AlertAlreadyClosedError("La solicitud ya fue atendida.")
        if record.status == AlertStatus.EN_ATENCION:
            return _to_detail(record)
        stored = alerts.update_if_status(
            replace(record, status=AlertStatus.EN_ATENCION, acknowledged_by=user.uid),
            AlertStatus.PENDIENTE,
        )
        if stored is None:
            raise AlertNotFoundError("La solicitud no existe.")
        return _to_detail(stored)


def update_alert_status(
    user: AuthenticatedUser,
    alert_id: str,
    status: AlertStatus,
    alerts: AlertRepository,
    users: UserRepository,
) -> AlertDetail:
    """H5: cambia el estado a cualquier valor del enum (el contrato no fija transiciones, P25).

    Al pasar a en_atencion sin nadie registrado como quien la atiende, se registra
    al usuario (mismo efecto interno que H4).
    """
    with _LOCK:
        record = _get_accessible(user, alert_id, alerts, users)
        _ensure_can_attend(user, record.adult_uid, users)
        acknowledged_by = record.acknowledged_by
        if status == AlertStatus.EN_ATENCION and acknowledged_by is None:
            acknowledged_by = user.uid
        stored = alerts.update(
            replace(record, status=status, acknowledged_by=acknowledged_by)
        )
        if stored is None:
            raise AlertNotFoundError("La solicitud no existe.")
        return _to_detail(stored)


def escalate_pending_alerts(
    alerts: AlertRepository,
    contacts: ContactRepository,
    users: UserRepository,
    devices: DeviceRepository,
    escalation_seconds: int,
    now: datetime | None = None,
) -> int:
    """Reenvía las solicitudes pendientes sin confirmar (RF-24); devuelve cuántas.

    Una pendiente con now - last_notified_at >= escalation_seconds y menos de
    MAX_ESCALATION_ROUNDS reenvíos pasa a la ronda siguiente con una escritura
    condicionada a que siga pendiente (si alguien la atendió entre medias, no se
    envía nada) y recibe UNA push a sus destinatarios más el familiar responsable.
    """
    current = now or _now()
    interval = timedelta(seconds=escalation_seconds)
    to_notify: list[tuple[AlertRecord, list[str]]] = []

    with _LOCK:
        for record in alerts.list_pending():
            if record.notification_round >= MAX_ESCALATION_ROUNDS:
                continue
            if current - record.last_notified_at < interval:
                continue
            stored = alerts.update_if_status(
                replace(
                    record,
                    notification_round=record.notification_round + 1,
                    last_notified_at=current,
                ),
                AlertStatus.PENDIENTE,
            )
            if stored is None:
                continue
            uids = alert_recipients(stored, contacts, users)
            family_uid = users.responsible_family_of(stored.adult_uid)
            if family_uid is not None and family_uid not in uids:
                uids.append(family_uid)
            to_notify.append((stored, uids))

    # Fuera del lock: la push hace red.
    for stored, uids in to_notify:
        logger.info(
            "Solicitud de ayuda %s reenviada (ronda %d)",
            stored.id,
            stored.notification_round,
        )
        push_service.notify_users(
            uids, ALERT_PUSH_TITLE, _push_body(stored), _push_data(stored), devices
        )
    return len(to_notify)
