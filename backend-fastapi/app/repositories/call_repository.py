import json
from dataclasses import replace
from datetime import timedelta
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from app.core.config import get_settings
from app.db import mapping
from app.db import session as db_session
from app.db.mapping import (
    T_ADULTO_CONTACTO,
    T_COMUNICACION,
    T_COMUNICACION_HISTORIAL_ESTADO,
    T_USUARIO,
    TIPO_LLAMADA_AUDIO,
    TIPO_VIDEOLLAMADA,
)
from app.models.calls import CallRecord
from app.models.enums import CallStatus


class CallRepository(Protocol):
    """Acceso a las llamadas de cada adulto mayor (ADR-05)."""

    def add(self, record: CallRecord) -> CallRecord: ...

    def get(self, adult_uid: str, call_id: str) -> CallRecord | None: ...

    def update_status(
        self, adult_uid: str, call_id: str, status: CallStatus
    ) -> CallRecord | None: ...

    def list_for_adult(self, adult_uid: str) -> list[CallRecord]: ...


class InMemoryCallRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias para que modificar un CallRecord recibido no altere lo guardado.
    """

    def __init__(self) -> None:
        self._calls: dict[str, dict[str, CallRecord]] = {}

    def add(self, record: CallRecord) -> CallRecord:
        stored = replace(record)
        self._calls.setdefault(stored.adult_uid, {})[stored.id] = stored
        return replace(stored)

    def get(self, adult_uid: str, call_id: str) -> CallRecord | None:
        record = self._calls.get(adult_uid, {}).get(call_id)
        if record is None:
            return None
        return replace(record)

    def update_status(
        self, adult_uid: str, call_id: str, status: CallStatus
    ) -> CallRecord | None:
        adult_calls = self._calls.get(adult_uid, {})
        record = adult_calls.get(call_id)
        if record is None:
            return None
        updated = replace(record, status=status)
        adult_calls[call_id] = updated
        return replace(updated)

    def list_for_adult(self, adult_uid: str) -> list[CallRecord]:
        # sorted es estable: con el mismo started_at se conserva el orden de inserción.
        records = sorted(
            self._calls.get(adult_uid, {}).values(),
            key=lambda record: record.started_at,
            reverse=True,
        )
        return [replace(record) for record in records]


# --- PostgreSQL (plan ronda 3, D-R3-9) ---

# Estado con el que se crea toda llamada; al leerlo es no_contestada (mapping).
_INITIAL_STATE = "INICIADA"
# Duración de la sala de Daily (D-T4-1): en las lecturas room_expires_at = inicio + 1 h.
_ROOM_LIFETIME = timedelta(hours=1)
_CALL_TYPES = f"('{TIPO_LLAMADA_AUDIO}', '{TIPO_VIDEOLLAMADA}')"

_CALL_COLUMNS = (
    "c.id_comunicacion, c.adulto_contacto_id, c.fecha_inicio, c.estado, "
    "c.tipo_comunicacion, c.daily_sala_id"
)

# id_usuario del adulto si el adulto_contacto es suyo (en cualquier estado del contacto).
_SELECT_OWNER = text(
    "SELECT ac.adulto_mayor_id "
    f"FROM {T_ADULTO_CONTACTO} ac "
    f"JOIN {T_USUARIO} u ON u.id_usuario = ac.adulto_mayor_id "
    "WHERE ac.id_adulto_contacto = :adulto_contacto_id AND u.firebase_uid = :uid"
)
_INSERT_CALL = text(
    f"INSERT INTO {T_COMUNICACION} "
    "(adulto_contacto_id, iniciado_por_id, id_idempotencia, tipo_comunicacion, estado, "
    "fecha_inicio, fecha_fin, daily_sala_id, creado_en, actualizado_en) "
    "VALUES (:adulto_contacto_id, :iniciado_por_id, :id_idempotencia, :tipo, "
    f"'{_INITIAL_STATE}', :fecha_inicio, NULL, :daily_sala_id, :fecha_inicio, :fecha_inicio) "
    "RETURNING id_comunicacion"
)
_INSERT_HISTORY = text(
    f"INSERT INTO {T_COMUNICACION_HISTORIAL_ESTADO} "
    "(comunicacion_id, cambiado_por_id, estado_nuevo, cambiado_en) "
    "VALUES (:comunicacion_id, :cambiado_por_id, :estado_nuevo, :cambiado_en)"
)
_SELECT_CALL = text(
    f"SELECT {_CALL_COLUMNS} "
    f"FROM {T_COMUNICACION} c "
    f"JOIN {T_ADULTO_CONTACTO} ac ON ac.id_adulto_contacto = c.adulto_contacto_id "
    f"JOIN {T_USUARIO} u ON u.id_usuario = ac.adulto_mayor_id "
    "WHERE c.id_comunicacion = :id_comunicacion "
    f"AND c.tipo_comunicacion IN {_CALL_TYPES} "
    "AND u.firebase_uid = :uid"
)
_LIST_CALLS = text(
    f"SELECT {_CALL_COLUMNS} "
    f"FROM {T_COMUNICACION} c "
    f"JOIN {T_ADULTO_CONTACTO} ac ON ac.id_adulto_contacto = c.adulto_contacto_id "
    f"JOIN {T_USUARIO} u ON u.id_usuario = ac.adulto_mayor_id "
    "WHERE u.firebase_uid = :uid "
    f"AND c.tipo_comunicacion IN {_CALL_TYPES} "
    "ORDER BY c.fecha_inicio DESC, c.id_comunicacion ASC"
)
_UPDATE_STATUS = text(
    f"UPDATE {T_COMUNICACION} c "
    "SET estado = :estado, "
    "fecha_fin = GREATEST(clock_timestamp(), c.fecha_inicio), "
    "actualizado_en = GREATEST(clock_timestamp(), c.creado_en) "
    "WHERE c.id_comunicacion = :id_comunicacion "
    f"AND c.tipo_comunicacion IN {_CALL_TYPES} "
    "AND c.adulto_contacto_id IN ("
    f"SELECT id_adulto_contacto FROM {T_ADULTO_CONTACTO} WHERE adulto_mayor_id = :adulto"
    ") "
    f"RETURNING {_CALL_COLUMNS}, c.fecha_fin"
)


class SqlCallRepository:
    """Llamadas y videollamadas en connects_senior.comunicacion (D-R3-9).

    Cada llamada es una fila de comunicacion (LLAMADA_AUDIO o VIDEOLLAMADA) precedida de
    su fila de idempotencia_operacion y con historial en comunicacion_historial_estado.
    Se guarda siempre en estado INICIADA (se lee no_contestada); L2 escribe FINALIZADA,
    NO_ATENDIDA o CANCELADA y fija fecha_fin. Solo se guarda el nombre de la sala de
    Daily: en las lecturas room_url se reconstruye con ``daily_domain`` y
    room_expires_at = started_at + 1 h. Los ids de la API son el id_comunicacion en texto.
    """

    def __init__(self, db: db_session.Database, daily_domain: str) -> None:
        self._db = db
        self._daily_domain = daily_domain or ""

    def add(self, record: CallRecord) -> CallRecord:
        """Guarda la llamada y devuelve el registro recibido con el id de la base.

        room_url y room_expires_at vuelven tal como llegaron de Daily (el servicio los usa
        para la respuesta de L1 y la push). Un contact_id que no es de ese adulto es un
        error de programación del servicio: ValueError y no se escribe nada.
        """
        contact_pk = mapping.parse_id(record.contact_id)
        tipo = mapping.tipo_for_audio_only(record.audio_only)
        with self._db.transaction() as conn:
            adult_id = _owner_id(conn, record.adult_uid, contact_pk)
            if adult_id is None:
                raise ValueError("El contacto de la llamada no pertenece al adulto.")
            idempotency_id = mapping.new_idempotency(
                conn,
                adult_id,
                mapping.OP_CREAR_COMUNICACION,
                _hash_source(record, tipo),
            )
            call_pk = int(
                conn.execute(
                    _INSERT_CALL,
                    {
                        "adulto_contacto_id": contact_pk,
                        "iniciado_por_id": adult_id,
                        "id_idempotencia": idempotency_id,
                        "tipo": tipo,
                        "fecha_inicio": record.started_at,
                        "daily_sala_id": record.room_name or None,
                    },
                ).scalar_one()
            )
            conn.execute(
                _INSERT_HISTORY,
                {
                    "comunicacion_id": call_pk,
                    "cambiado_por_id": adult_id,
                    "estado_nuevo": _INITIAL_STATE,
                    "cambiado_en": record.started_at,
                },
            )
        return replace(record, id=mapping.id_str(call_pk))

    def get(self, adult_uid: str, call_id: str) -> CallRecord | None:
        call_pk = mapping.parse_id(call_id)
        if call_pk is None or not adult_uid:
            return None
        with self._db.transaction() as conn:
            row = conn.execute(
                _SELECT_CALL, {"id_comunicacion": call_pk, "uid": adult_uid}
            ).one_or_none()
        return None if row is None else self._to_record(adult_uid, row)

    def update_status(
        self, adult_uid: str, call_id: str, status: CallStatus
    ) -> CallRecord | None:
        """L2: nuevo estado, fecha_fin e historial; devuelve la fila releída.

        None si la llamada no existe, es de otro adulto o es un mensaje de voz.
        """
        call_pk = mapping.parse_id(call_id)
        if call_pk is None or not adult_uid:
            return None
        db_state = mapping.CALL_STATUS_TO_DB[CallStatus(status)]
        with self._db.transaction() as conn:
            adult_id = mapping.user_id_for_uid(conn, adult_uid)
            if adult_id is None:
                return None
            row = conn.execute(
                _UPDATE_STATUS,
                {"estado": db_state, "id_comunicacion": call_pk, "adulto": adult_id},
            ).one_or_none()
            if row is None:
                return None
            conn.execute(
                _INSERT_HISTORY,
                {
                    "comunicacion_id": call_pk,
                    "cambiado_por_id": adult_id,
                    "estado_nuevo": db_state,
                    "cambiado_en": row.fecha_fin,
                },
            )
        return self._to_record(adult_uid, row)

    def list_for_adult(self, adult_uid: str) -> list[CallRecord]:
        """Historial del adulto, incluidas las llamadas de contactos dados de baja."""
        if not adult_uid:
            return []
        with self._db.transaction() as conn:
            rows = conn.execute(_LIST_CALLS, {"uid": adult_uid}).all()
        return [self._to_record(adult_uid, row) for row in rows]

    def _to_record(self, adult_uid: str, row: Row) -> CallRecord:
        started_at = mapping.as_utc(row.fecha_inicio)
        room_name = row.daily_sala_id or ""
        return CallRecord(
            id=mapping.id_str(row.id_comunicacion),
            adult_uid=adult_uid,
            contact_id=mapping.id_str(row.adulto_contacto_id),
            started_at=started_at,
            status=mapping.call_status_from_db(row.estado),
            audio_only=mapping.audio_only_from_tipo(row.tipo_comunicacion),
            room_name=room_name,
            room_url=mapping.daily_room_url(self._daily_domain, room_name),
            room_expires_at=started_at + _ROOM_LIFETIME,
        )


def _owner_id(conn: Connection, adult_uid: str, contact_pk: int | None) -> int | None:
    """id_usuario del adulto si el adulto_contacto le pertenece; None en otro caso."""
    if contact_pk is None or not adult_uid:
        return None
    value = conn.execute(
        _SELECT_OWNER, {"adulto_contacto_id": contact_pk, "uid": adult_uid}
    ).scalar_one_or_none()
    return None if value is None else int(value)


def _hash_source(record: CallRecord, tipo: str) -> str:
    """Texto del que sale hash_solicitud de idempotencia_operacion (D-R3-14)."""
    return json.dumps(
        {
            "operacion": mapping.OP_CREAR_COMUNICACION,
            "adult_uid": record.adult_uid,
            "contact_id": record.contact_id,
            "tipo": tipo,
            "room_name": record.room_name,
            "started_at": record.started_at.isoformat(),
        },
        sort_keys=True,
    )


_default_call_repository = InMemoryCallRepository()
_sql_call_repository = db_session.lazy_sql_repository(
    lambda db: SqlCallRepository(db, get_settings().daily_domain)
)


def get_call_repository() -> CallRepository:
    if db_session.sql_enabled():
        return _sql_call_repository()
    return _default_call_repository
