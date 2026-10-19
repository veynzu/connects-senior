"""Repositorio de recordatorios (ADR-05, D-R2-11, D-R2-23, ronda 3 D-R3-11 y D-R3-26(c)).

InMemoryReminderRepository se usa en los tests y con DATABASE_URL de sqlite;
SqlReminderRepository guarda en connects_senior.recordatorio y
connects_senior.recordatorio_historial_estado cuando DATABASE_URL es PostgreSQL.
"""
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import session as db_session
from app.db.mapping import (
    T_RECORDATORIO,
    T_RECORDATORIO_HISTORIAL_ESTADO,
    T_USUARIO,
    as_utc,
    ensure_user_id,
    id_str,
    offset_text,
    parse_id,
    tzinfo_from_text,
)
from app.models.enums import ReminderKind, ReminderRepeat, ReminderStatus
from app.models.reminders import ReminderRecord


class ReminderRepository(Protocol):
    """Acceso a los recordatorios de todos los adultos mayores."""

    def add(self, record: ReminderRecord) -> ReminderRecord: ...

    def get(self, reminder_id: str) -> ReminderRecord | None: ...

    def update(self, record: ReminderRecord) -> ReminderRecord | None: ...

    def update_if_status(
        self, record: ReminderRecord, expected_status: ReminderStatus
    ) -> ReminderRecord | None: ...

    def delete(self, reminder_id: str) -> bool: ...

    def list_for_adult(self, adult_uid: str) -> list[ReminderRecord]: ...

    def list_pending(self) -> list[ReminderRecord]: ...


class InMemoryReminderRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias. Todos los métodos toman el mismo lock porque el
    planificador (threadpool) lee y escribe mientras las rutas `def` escriben; las
    iteraciones recorren copias. Un contador de inserción desempata los listados.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # id -> (orden de inserción, registro)
        self._records: dict[str, tuple[int, ReminderRecord]] = {}
        self._next_sequence = 0

    def add(self, record: ReminderRecord) -> ReminderRecord:
        stored = replace(record)
        with self._lock:
            current = self._records.get(stored.id)
            if current is None:
                sequence = self._next_sequence
                self._next_sequence += 1
            else:
                sequence = current[0]
            self._records[stored.id] = (sequence, stored)
        return replace(stored)

    def get(self, reminder_id: str) -> ReminderRecord | None:
        with self._lock:
            current = self._records.get(reminder_id)
            return None if current is None else replace(current[1])

    def update(self, record: ReminderRecord) -> ReminderRecord | None:
        """Reemplaza el registro con el mismo id; None si no existe."""
        stored = replace(record)
        with self._lock:
            current = self._records.get(stored.id)
            if current is None:
                return None
            self._records[stored.id] = (current[0], stored)
        return replace(stored)

    def update_if_status(
        self, record: ReminderRecord, expected_status: ReminderStatus
    ) -> ReminderRecord | None:
        """Reemplaza solo si el registro guardado tiene expected_status (atómico).

        Si no existe o su estado cambió, devuelve None sin tocar nada.
        """
        stored = replace(record)
        with self._lock:
            current = self._records.get(stored.id)
            if current is None or current[1].status != expected_status:
                return None
            self._records[stored.id] = (current[0], stored)
        return replace(stored)

    def delete(self, reminder_id: str) -> bool:
        with self._lock:
            return self._records.pop(reminder_id, None) is not None

    def list_for_adult(self, adult_uid: str) -> list[ReminderRecord]:
        """scheduled_at ascendente; en empate, orden de inserción."""
        with self._lock:
            entries = list(self._records.values())
        selected = [entry for entry in entries if entry[1].adult_uid == adult_uid]
        selected.sort(key=lambda entry: (entry[1].scheduled_at, entry[0]))
        return [replace(record) for _, record in selected]

    def list_pending(self) -> list[ReminderRecord]:
        """Recordatorios pendientes de todos los adultos, scheduled_at ascendente."""
        with self._lock:
            entries = list(self._records.values())
        selected = [
            entry for entry in entries if entry[1].status == ReminderStatus.PENDIENTE
        ]
        selected.sort(key=lambda entry: (entry[1].scheduled_at, entry[0]))
        return [replace(record) for _, record in selected]


# --- PostgreSQL (plan ronda 3, D-R3-11 y D-R3-26(c)) ---

_EVENT_CREATED = "CREADO"
_EVENT_NOTIFIED = "NOTIFICADO"
_EVENT_CONFIRMED = "CONFIRMADO"
_EVENT_MISSED = "OMITIDO"
_EVENT_RESCHEDULED = "REPROGRAMADO"

# CHECK de desfase_utc en V004: de -14:00 a +14:00 con minutos enteros.
_MAX_DESFASE_MINUTES = 14 * 60

_RECORD_COLUMNS = (
    "r.id_recordatorio, u.firebase_uid, r.tipo, r.descripcion, r.programado_para, "
    "r.desfase_utc, r.zona_horaria, r.repeticion, r.estado, r.creado_en, "
    "r.notificado_en, r.siguiente_ocurrencia_creada"
)
_RECORD_FROM = (
    f"FROM {T_RECORDATORIO} r JOIN {T_USUARIO} u ON u.id_usuario = r.adulto_mayor_id"
)
# Baja lógica (D-R3-26(c)): un recordatorio con eliminado_en no existe para la API.
_ACTIVE = "r.eliminado_en IS NULL"
_ORDER = "ORDER BY r.programado_para ASC, r.id_recordatorio ASC"

_SELECT_BY_ID = text(
    f"SELECT {_RECORD_COLUMNS} {_RECORD_FROM} "
    f"WHERE r.id_recordatorio = :id AND {_ACTIVE}"
)
_SELECT_FOR_ADULT = text(
    f"SELECT {_RECORD_COLUMNS} {_RECORD_FROM} "
    f"WHERE u.firebase_uid = :uid AND {_ACTIVE} {_ORDER}"
)
_SELECT_BY_STATUS = text(
    f"SELECT {_RECORD_COLUMNS} {_RECORD_FROM} "
    f"WHERE r.estado = :estado AND {_ACTIVE} {_ORDER}"
)
_LOCK_CURRENT = text(
    "SELECT adulto_mayor_id, programado_para, desfase_utc, zona_horaria, estado, "
    f"notificado_en, creado_en FROM {T_RECORDATORIO} "
    "WHERE id_recordatorio = :id AND eliminado_en IS NULL FOR UPDATE"
)
# zona_horaria no se escribe: queda con su default 'America/Bogota' (D-R3-26(c)).
_INSERT = text(
    f"INSERT INTO {T_RECORDATORIO} "
    "(adulto_mayor_id, creado_por_id, tipo, descripcion, programado_para, desfase_utc, "
    "repeticion, estado, notificado_en, siguiente_ocurrencia_creada, "
    "recordatorio_origen_id, creado_en, actualizado_en) "
    "VALUES (:adulto, :adulto, :tipo, :descripcion, :programado_para, :desfase_utc, "
    ":repeticion, :estado, :notificado_en, :siguiente, NULL, :creado_en, :creado_en) "
    "RETURNING id_recordatorio"
)
_UPDATE_SET = (
    f"UPDATE {T_RECORDATORIO} SET "
    "tipo = :tipo, descripcion = :descripcion, programado_para = :programado_para, "
    "desfase_utc = :desfase_utc, repeticion = :repeticion, estado = :estado, "
    "notificado_en = :notificado_en, siguiente_ocurrencia_creada = :siguiente, "
    "actualizado_en = GREATEST(clock_timestamp(), creado_en) "
    "WHERE id_recordatorio = :id AND eliminado_en IS NULL"
)
_UPDATE = text(f"{_UPDATE_SET} RETURNING id_recordatorio")
_UPDATE_IF_STATUS = text(f"{_UPDATE_SET} AND estado = :esperado RETURNING id_recordatorio")
_SOFT_DELETE = text(
    f"UPDATE {T_RECORDATORIO} SET "
    "eliminado_en = GREATEST(clock_timestamp(), creado_en), "
    "actualizado_en = GREATEST(clock_timestamp(), creado_en) "
    "WHERE id_recordatorio = :id AND eliminado_en IS NULL "
    "RETURNING id_recordatorio"
)
_INSERT_CREATED_EVENT = text(
    f"INSERT INTO {T_RECORDATORIO_HISTORIAL_ESTADO} "
    "(recordatorio_id, cambiado_por_id, evento, estado_nuevo, registrado_en, "
    "programado_para_nuevo) "
    "VALUES (:recordatorio_id, :cambiado_por_id, :evento, :estado_nuevo, "
    ":registrado_en, :programado_para_nuevo)"
)
_INSERT_CHANGE_EVENT = text(
    f"INSERT INTO {T_RECORDATORIO_HISTORIAL_ESTADO} "
    "(recordatorio_id, cambiado_por_id, evento, estado_nuevo, registrado_en, "
    "programado_para_nuevo) "
    "VALUES (:recordatorio_id, :cambiado_por_id, :evento, :estado_nuevo, "
    "GREATEST(clock_timestamp(), :creado_en), :programado_para_nuevo)"
)


@dataclass(frozen=True)
class _HistoryEvent:
    evento: str
    estado_nuevo: str
    cambiado_por_id: int | None
    programado_para_nuevo: datetime | None


def _utc(value: datetime) -> datetime:
    """Instante en UTC de una fecha con zona (programado_para se guarda en UTC)."""
    if value.utcoffset() is None:
        raise ValueError("La fecha no tiene zona horaria.")
    return value.astimezone(timezone.utc)


def _desfase_utc(value: datetime) -> str:
    """Offset de scheduled_at para desfase_utc, "+HH:MM" o "-HH:MM" (D-R3-26(c)).

    Es offset_text(value) cuando el offset tiene minutos enteros y cabe en el CHECK de
    V004 (de -14:00 a +14:00), que es el caso normal. Un offset con segundos se
    redondea al minuto más cercano y uno fuera de rango (Python admite hasta ±23:59) se
    guarda como "+00:00"; el instante (programado_para) no cambia, solo el offset con
    el que se devuelve al leer.
    """
    offset = value.utcoffset()
    if offset is None:
        raise ValueError("La fecha no tiene zona horaria.")
    total_seconds = offset.total_seconds()
    minutes = int((abs(total_seconds) + 30) // 60)
    if minutes > _MAX_DESFASE_MINUTES:
        return "+00:00"
    rounded = timedelta(minutes=-minutes if total_seconds < 0 else minutes)
    return offset_text(value.astimezone(timezone(rounded)))


def _zone(desfase_utc: str | None, zona_horaria: str | None) -> tzinfo:
    """Zona con la que se devuelve scheduled_at (D-R3-26(c)).

    desfase_utc ("-05:00") si no es NULL; si lo es, zona_horaria (nombre IANA como
    el default 'America/Bogota' -> ZoneInfo), vía mapping.tzinfo_from_text.
    """
    return tzinfo_from_text(desfase_utc if desfase_utc is not None else zona_horaria)


def _scheduled_at(
    programado_para: datetime, desfase_utc: str | None, zona_horaria: str | None
) -> datetime:
    """scheduled_at del contrato: el instante guardado con el offset original (D-R3-11)."""
    return as_utc(programado_para).astimezone(_zone(desfase_utc, zona_horaria))


def _to_record(row: Any) -> ReminderRecord:
    return ReminderRecord(
        id=id_str(row.id_recordatorio),
        adult_uid=row.firebase_uid,
        kind=ReminderKind(row.tipo),
        description=row.descripcion,
        scheduled_at=_scheduled_at(row.programado_para, row.desfase_utc, row.zona_horaria),
        repeat=ReminderRepeat(row.repeticion),
        status=ReminderStatus(row.estado),
        created_at=as_utc(row.creado_en),
        notified_at=as_utc(row.notificado_en),
        next_created=bool(row.siguiente_ocurrencia_creada),
    )


def _write_params(record: ReminderRecord) -> dict[str, Any]:
    """Columnas que escriben add, update y update_if_status (sin desfase ni ids)."""
    return {
        "tipo": ReminderKind(record.kind).value,
        "descripcion": record.description,
        "programado_para": _utc(record.scheduled_at),
        "repeticion": ReminderRepeat(record.repeat).value,
        "estado": ReminderStatus(record.status).value,
        "notificado_en": record.notified_at,
        "siguiente": bool(record.next_created),
    }


def _desfase_for_update(record: ReminderRecord, current: Any) -> str | None:
    """desfase_utc tras una actualización (D-R3-11, D-R3-26(c)).

    Se conserva el guardado (incluido NULL, que se lee con zona_horaria, por ejemplo
    'America/Bogota') mientras scheduled_at sea el mismo instante con el mismo offset;
    si cambia el instante (R3, R6) o el offset con el que llega, se reescribe con el
    offset de scheduled_at para devolver la fecha con el offset que se envió, igual que
    InMemory. zona_horaria nunca se toca.
    """
    stored = _scheduled_at(
        current.programado_para, current.desfase_utc, current.zona_horaria
    )
    if (
        record.scheduled_at == stored
        and record.scheduled_at.utcoffset() == stored.utcoffset()
    ):
        return current.desfase_utc
    return _desfase_utc(record.scheduled_at)


def _change_events(record: ReminderRecord, current: Any) -> list[_HistoryEvent]:
    """Eventos de recordatorio_historial_estado que produce una actualización.

    REPROGRAMADO: cambia programado_para y el recordatorio queda pendiente (R3, R6).
    NOTIFICADO: notificado_en pasa de NULL a una fecha (planificador, RF-28).
    CONFIRMADO / OMITIDO: el estado pasa a confirmado (R5) u omitido (planificador).
    programado_para_nuevo lleva la nueva hora cuando la actualización la cambió.
    cambiado_por_id: el adulto en CONFIRMADO (R5 solo lo usa el adulto mayor, RF-29);
    NULL en el resto (el planificador es automático y R3 puede venir de un familiar o
    cuidador que el repositorio no conoce).
    """
    new_status = ReminderStatus(record.status)
    new_programado = _utc(record.scheduled_at)
    moved = new_programado != as_utc(current.programado_para)
    programado_para_nuevo = new_programado if moved else None
    events: list[_HistoryEvent] = []

    if moved and new_status == ReminderStatus.PENDIENTE:
        events.append(
            _HistoryEvent(_EVENT_RESCHEDULED, new_status.value, None, programado_para_nuevo)
        )
    if current.notificado_en is None and record.notified_at is not None:
        events.append(
            _HistoryEvent(_EVENT_NOTIFIED, new_status.value, None, programado_para_nuevo)
        )
    if new_status != ReminderStatus(current.estado):
        if new_status == ReminderStatus.CONFIRMADO:
            events.append(
                _HistoryEvent(
                    _EVENT_CONFIRMED,
                    new_status.value,
                    int(current.adulto_mayor_id),
                    programado_para_nuevo,
                )
            )
        elif new_status == ReminderStatus.OMITIDO:
            events.append(
                _HistoryEvent(_EVENT_MISSED, new_status.value, None, programado_para_nuevo)
            )
    return events


class SqlReminderRepository:
    """Recordatorios en PostgreSQL (tablas recordatorio y recordatorio_historial_estado).

    Cada método público abre una sola transacción (D-R3-3). scheduled_at se guarda como
    programado_para en UTC más desfase_utc con el offset original ("-05:00"); zona_horaria
    queda en su default y solo se usa al leer filas con desfase_utc NULL (D-R3-11,
    D-R3-26(c)). delete es una baja lógica (eliminado_en) y toda lectura o actualización
    ignora los recordatorios dados de baja. Los ids son id_recordatorio en texto; un id
    no numérico se trata como inexistente sin consultar. add devuelve el registro
    recibido con el id nuevo; update y update_if_status devuelven el registro releído.
    """

    def __init__(self, db: db_session.Database) -> None:
        self._db = db

    def add(self, record: ReminderRecord) -> ReminderRecord:
        with self._db.transaction() as conn:
            adult_id = ensure_user_id(conn, record.adult_uid)
            new_id = conn.execute(
                _INSERT,
                {
                    **_write_params(record),
                    "adulto": adult_id,
                    "desfase_utc": _desfase_utc(record.scheduled_at),
                    "creado_en": record.created_at,
                },
            ).scalar_one()
            conn.execute(
                _INSERT_CREATED_EVENT,
                {
                    "recordatorio_id": int(new_id),
                    "cambiado_por_id": adult_id,
                    "evento": _EVENT_CREATED,
                    "estado_nuevo": ReminderStatus(record.status).value,
                    "registrado_en": record.created_at,
                    "programado_para_nuevo": _utc(record.scheduled_at),
                },
            )
        return replace(record, id=id_str(new_id))

    def get(self, reminder_id: str) -> ReminderRecord | None:
        numeric_id = parse_id(reminder_id)
        if numeric_id is None:
            return None
        with self._db.transaction() as conn:
            return self._read(conn, numeric_id)

    def update(self, record: ReminderRecord) -> ReminderRecord | None:
        """Reemplaza los campos del recordatorio con el mismo id; None si no existe.

        No cambia el adulto ni creado_en: el registro devuelto es el releído. Un
        recordatorio dado de baja cuenta como inexistente.
        """
        numeric_id = parse_id(record.id)
        if numeric_id is None:
            return None
        with self._db.transaction() as conn:
            return self._update(conn, numeric_id, record, None)

    def update_if_status(
        self, record: ReminderRecord, expected_status: ReminderStatus
    ) -> ReminderRecord | None:
        """Reemplaza solo si el estado guardado es expected_status (atómico).

        Si no existe, está dado de baja o su estado cambió, devuelve None sin escribir
        nada.
        """
        numeric_id = parse_id(record.id)
        if numeric_id is None:
            return None
        with self._db.transaction() as conn:
            return self._update(conn, numeric_id, record, ReminderStatus(expected_status))

    def delete(self, reminder_id: str) -> bool:
        """Baja lógica (D-R3-26(c)): fija eliminado_en y actualizado_en.

        El historial y el recordatorio_origen_id de otros recordatorios no se tocan
        (las FK siguen ON DELETE RESTRICT). False si no existe o ya estaba dado de baja.
        """
        numeric_id = parse_id(reminder_id)
        if numeric_id is None:
            return False
        with self._db.transaction() as conn:
            return conn.execute(_SOFT_DELETE, {"id": numeric_id}).first() is not None

    def list_for_adult(self, adult_uid: str) -> list[ReminderRecord]:
        """scheduled_at ascendente (por instante); en empate, id ascendente."""
        if not adult_uid:
            return []
        with self._db.transaction() as conn:
            rows = conn.execute(_SELECT_FOR_ADULT, {"uid": adult_uid}).all()
        return [_to_record(row) for row in rows]

    def list_pending(self) -> list[ReminderRecord]:
        """Recordatorios pendientes de todos los adultos, scheduled_at ascendente."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                _SELECT_BY_STATUS, {"estado": ReminderStatus.PENDIENTE.value}
            ).all()
        return [_to_record(row) for row in rows]

    # --- helpers privados: reciben la conexión de la transacción en curso ---

    @staticmethod
    def _read(conn: Connection, numeric_id: int) -> ReminderRecord | None:
        row = conn.execute(_SELECT_BY_ID, {"id": numeric_id}).one_or_none()
        return None if row is None else _to_record(row)

    def _update(
        self,
        conn: Connection,
        numeric_id: int,
        record: ReminderRecord,
        expected_status: ReminderStatus | None,
    ) -> ReminderRecord | None:
        current = conn.execute(_LOCK_CURRENT, {"id": numeric_id}).one_or_none()
        if current is None:
            return None

        params = {
            **_write_params(record),
            "id": numeric_id,
            "desfase_utc": _desfase_for_update(record, current),
        }
        if expected_status is None:
            updated = conn.execute(_UPDATE, params).first()
        else:
            updated = conn.execute(
                _UPDATE_IF_STATUS, {**params, "esperado": expected_status.value}
            ).first()
        if updated is None:
            return None

        for event in _change_events(record, current):
            conn.execute(
                _INSERT_CHANGE_EVENT,
                {
                    "recordatorio_id": numeric_id,
                    "cambiado_por_id": event.cambiado_por_id,
                    "evento": event.evento,
                    "estado_nuevo": event.estado_nuevo,
                    "creado_en": current.creado_en,
                    "programado_para_nuevo": event.programado_para_nuevo,
                },
            )
        return self._read(conn, numeric_id)


_default_reminder_repository = InMemoryReminderRepository()
_sql_reminder_repository = db_session.lazy_sql_repository(SqlReminderRepository)


def get_reminder_repository() -> ReminderRepository:
    if db_session.sql_enabled():
        return _sql_reminder_repository()
    return _default_reminder_repository
