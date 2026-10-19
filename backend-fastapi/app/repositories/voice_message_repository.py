import json
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from app.db import mapping
from app.db import session as db_session
from app.db.mapping import (
    T_ADULTO_CONTACTO,
    T_COMUNICACION,
    T_MENSAJE_VOZ,
    T_USUARIO,
    TIPO_MENSAJE_VOZ,
)
from app.models.voice_messages import VoiceMessageRecord
from app.repositories.errors import RecipientNotLinkedError


class VoiceMessageRepository(Protocol):
    """Metadatos de los mensajes de voz (ADR-05, ADR-08): el audio vive en disco."""

    def add(self, record: VoiceMessageRecord) -> VoiceMessageRecord: ...

    def get(self, voice_message_id: str) -> VoiceMessageRecord | None: ...

    def list_received_by_adult(self, adult_uid: str) -> list[VoiceMessageRecord]: ...

    def list_sent_to_contact(
        self, adult_uid: str, contact_id: str
    ) -> list[VoiceMessageRecord]: ...


class InMemoryVoiceMessageRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias. Las listas van del más reciente al más antiguo; con el
    mismo created_at va primero el último insertado (ascendente estable por created_at
    y después invertido), para lo que se guarda un contador de inserción.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        # voice_message_id -> (orden de inserción, registro)
        self._records: dict[str, tuple[int, VoiceMessageRecord]] = {}

    def add(self, record: VoiceMessageRecord) -> VoiceMessageRecord:
        stored = replace(record)
        with self._lock:
            self._sequence += 1
            self._records[stored.id] = (self._sequence, stored)
        return replace(stored)

    def get(self, voice_message_id: str) -> VoiceMessageRecord | None:
        with self._lock:
            entry = self._records.get(voice_message_id)
        return None if entry is None else replace(entry[1])

    def list_received_by_adult(self, adult_uid: str) -> list[VoiceMessageRecord]:
        """Mensajes que los contactos enviaron a ese adulto (from_adult False)."""
        return self._newest_first(
            lambda record: record.adult_uid == adult_uid and not record.from_adult
        )

    def list_sent_to_contact(
        self, adult_uid: str, contact_id: str
    ) -> list[VoiceMessageRecord]:
        """Mensajes que ese adulto envió a ese contacto (from_adult True)."""
        return self._newest_first(
            lambda record: record.adult_uid == adult_uid
            and record.contact_id == contact_id
            and record.from_adult
        )

    def _newest_first(
        self, predicate: Callable[[VoiceMessageRecord], bool]
    ) -> list[VoiceMessageRecord]:
        with self._lock:
            entries = list(self._records.values())
        selected = [entry for entry in entries if predicate(entry[1])]
        selected.sort(key=lambda entry: (entry[1].created_at, entry[0]), reverse=True)
        return [replace(record) for _, record in selected]


# --- PostgreSQL (plan ronda 3, D-R3-13) ---

# Un mensaje de voz ya enviado es una comunicación terminada.
_VOICE_STATE = "FINALIZADA"

_RECIPIENT_NOT_LINKED_MESSAGE = (
    "El contacto no corresponde a un usuario de la plataforma distinto del adulto."
)

_VOICE_COLUMNS = (
    "c.id_comunicacion, c.adulto_contacto_id, c.iniciado_por_id, c.fecha_inicio, "
    "ac.adulto_mayor_id, ac.nombre AS contacto_nombre, ac.apellido AS contacto_apellido, "
    "a.firebase_uid AS adulto_uid, "
    "mv.referencia_archivo, mv.tipo_mime, mv.tamano_bytes"
)
_VOICE_FROM = (
    f"FROM {T_COMUNICACION} c "
    f"JOIN {T_MENSAJE_VOZ} mv ON mv.comunicacion_id = c.id_comunicacion "
    f"JOIN {T_ADULTO_CONTACTO} ac ON ac.id_adulto_contacto = c.adulto_contacto_id "
    f"JOIN {T_USUARIO} a ON a.id_usuario = ac.adulto_mayor_id "
    f"WHERE c.tipo_comunicacion = '{TIPO_MENSAJE_VOZ}' "
)
_NEWEST_FIRST = "ORDER BY c.fecha_inicio DESC, c.id_comunicacion DESC"

# id_usuario del adulto si el adulto_contacto es suyo (en cualquier estado del contacto).
_SELECT_OWNER = text(
    "SELECT ac.adulto_mayor_id "
    f"FROM {T_ADULTO_CONTACTO} ac "
    f"JOIN {T_USUARIO} u ON u.id_usuario = ac.adulto_mayor_id "
    "WHERE ac.id_adulto_contacto = :adulto_contacto_id AND u.firebase_uid = :uid"
)
_INSERT_COMMUNICATION = text(
    f"INSERT INTO {T_COMUNICACION} "
    "(adulto_contacto_id, iniciado_por_id, id_idempotencia, tipo_comunicacion, estado, "
    "fecha_inicio, fecha_fin, daily_sala_id, creado_en, actualizado_en) "
    "VALUES (:adulto_contacto_id, :iniciado_por_id, :id_idempotencia, "
    f"'{TIPO_MENSAJE_VOZ}', '{_VOICE_STATE}', "
    ":created_at, :created_at, NULL, :created_at, :created_at) "
    "RETURNING id_comunicacion"
)
_INSERT_VOICE = text(
    f"INSERT INTO {T_MENSAJE_VOZ} "
    "(comunicacion_id, referencia_archivo, duracion_segundos, tipo_mime, tamano_bytes, "
    "creado_en) "
    "VALUES (:comunicacion_id, :referencia_archivo, NULL, :tipo_mime, :tamano_bytes, "
    ":created_at)"
)
_SELECT_VOICE = text(
    f"SELECT {_VOICE_COLUMNS} {_VOICE_FROM}AND c.id_comunicacion = :id_comunicacion"
)
_LIST_RECEIVED = text(
    f"SELECT {_VOICE_COLUMNS} {_VOICE_FROM}"
    "AND a.firebase_uid = :uid AND c.iniciado_por_id <> ac.adulto_mayor_id "
    f"{_NEWEST_FIRST}"
)
_LIST_SENT = text(
    f"SELECT {_VOICE_COLUMNS} {_VOICE_FROM}"
    "AND a.firebase_uid = :uid AND ac.id_adulto_contacto = :adulto_contacto_id "
    "AND c.iniciado_por_id = ac.adulto_mayor_id "
    f"{_NEWEST_FIRST}"
)


class SqlVoiceMessageRepository:
    """Metadatos de los mensajes de voz en comunicacion + mensaje_voz (D-R3-13).

    Cada mensaje es una fila de comunicacion (MENSAJE_VOZ, FINALIZADA, fecha_inicio =
    fecha_fin = created_at) precedida de su fila de idempotencia_operacion, más su fila de
    mensaje_voz con la referencia al archivo (ADR-08: el audio sigue en disco). El id de
    la API es el id_comunicacion en texto. El emisor (iniciado_por_id) es el adulto si
    from_adult; si no, el usuario cuyo correo coincide con el del contacto
    (mapping.contact_user_id, solo para almacenamiento). En las lecturas from_adult =
    (iniciado_por_id = adulto) y from_name se deriva: el nombre actual del adulto o el
    nombre y apellido del contacto tal como lo guardó el adulto.
    """

    def __init__(self, db: db_session.Database) -> None:
        self._db = db

    def add(self, record: VoiceMessageRecord) -> VoiceMessageRecord:
        """Guarda el mensaje y devuelve el registro recibido con el id de la base.

        RecipientNotLinkedError (sin escribir nada) si lo envía el contacto y su correo no
        corresponde a ningún usuario o corresponde al propio adulto. Un contact_id que no
        es de ese adulto es un error de programación del servicio: ValueError.
        """
        contact_pk = mapping.parse_id(record.contact_id)
        saved_pk: int | None = None
        with self._db.transaction() as conn:
            adult_id = _owner_id(conn, record.adult_uid, contact_pk)
            if adult_id is None:
                raise ValueError("El contacto del mensaje de voz no pertenece al adulto.")
            # El emisor se resuelve antes de escribir: si falta, no se inserta nada.
            sender_id = _sender_id(conn, record.from_adult, adult_id, contact_pk)
            if sender_id is not None:
                saved_pk = _insert_voice_message(conn, record, contact_pk, sender_id)
        if saved_pk is None:
            raise RecipientNotLinkedError(_RECIPIENT_NOT_LINKED_MESSAGE)
        return replace(record, id=mapping.id_str(saved_pk))

    def get(self, voice_message_id: str) -> VoiceMessageRecord | None:
        """Mensaje de voz por id; None si no existe o el id es de una llamada."""
        voice_pk = mapping.parse_id(voice_message_id)
        if voice_pk is None:
            return None
        with self._db.transaction() as conn:
            row = conn.execute(_SELECT_VOICE, {"id_comunicacion": voice_pk}).one_or_none()
            if row is None:
                return None
            return _to_records(conn, [row])[0]

    def list_received_by_adult(self, adult_uid: str) -> list[VoiceMessageRecord]:
        """Mensajes que los contactos del adulto le enviaron (incluye contactos de baja)."""
        if not adult_uid:
            return []
        with self._db.transaction() as conn:
            rows = conn.execute(_LIST_RECEIVED, {"uid": adult_uid}).all()
            return _to_records(conn, rows)

    def list_sent_to_contact(
        self, adult_uid: str, contact_id: str
    ) -> list[VoiceMessageRecord]:
        """Mensajes que ese adulto envió a ese contacto."""
        contact_pk = mapping.parse_id(contact_id)
        if contact_pk is None or not adult_uid:
            return []
        with self._db.transaction() as conn:
            rows = conn.execute(
                _LIST_SENT, {"uid": adult_uid, "adulto_contacto_id": contact_pk}
            ).all()
            return _to_records(conn, rows)


def _owner_id(conn: Connection, adult_uid: str, contact_pk: int | None) -> int | None:
    """id_usuario del adulto si el adulto_contacto le pertenece; None en otro caso."""
    if contact_pk is None or not adult_uid:
        return None
    value = conn.execute(
        _SELECT_OWNER, {"adulto_contacto_id": contact_pk, "uid": adult_uid}
    ).scalar_one_or_none()
    return None if value is None else int(value)


def _sender_id(
    conn: Connection, from_adult: bool, adult_id: int, contact_pk: int
) -> int | None:
    """iniciado_por_id: el adulto, o el usuario del contacto si es otro usuario."""
    if from_adult:
        return adult_id
    contact_user = mapping.contact_user_id(conn, contact_pk)
    if contact_user is None or contact_user == adult_id:
        return None
    return contact_user


def _insert_voice_message(
    conn: Connection, record: VoiceMessageRecord, contact_pk: int, sender_id: int
) -> int:
    """idempotencia_operacion + comunicacion + mensaje_voz; devuelve id_comunicacion."""
    idempotency_id = mapping.new_idempotency(
        conn, sender_id, mapping.OP_CREAR_COMUNICACION, _hash_source(record)
    )
    voice_pk = int(
        conn.execute(
            _INSERT_COMMUNICATION,
            {
                "adulto_contacto_id": contact_pk,
                "iniciado_por_id": sender_id,
                "id_idempotencia": idempotency_id,
                "created_at": record.created_at,
            },
        ).scalar_one()
    )
    conn.execute(
        _INSERT_VOICE,
        {
            "comunicacion_id": voice_pk,
            "referencia_archivo": record.file_name,
            "tipo_mime": record.content_type or None,
            "tamano_bytes": int(record.size_bytes),
            "created_at": record.created_at,
        },
    )
    return voice_pk


def _to_records(conn: Connection, rows: list[Row]) -> list[VoiceMessageRecord]:
    """Filas -> registros; el nombre del adulto se lee una sola vez por adulto."""
    adult_names: dict[int, str] = {}
    records = []
    for row in rows:
        adult_id = int(row.adulto_mayor_id)
        from_adult = int(row.iniciado_por_id) == adult_id
        if from_adult:
            if adult_id not in adult_names:
                adult_names[adult_id] = mapping.user_full_name(conn, adult_id)
            from_name = adult_names[adult_id]
        else:
            from_name = _contact_name(row.contacto_nombre, row.contacto_apellido)
        records.append(
            VoiceMessageRecord(
                id=mapping.id_str(row.id_comunicacion),
                adult_uid=row.adulto_uid,
                contact_id=mapping.id_str(row.adulto_contacto_id),
                from_adult=from_adult,
                from_name=from_name,
                file_name=row.referencia_archivo,
                content_type=row.tipo_mime or "",
                size_bytes=int(row.tamano_bytes or 0),
                created_at=mapping.as_utc(row.fecha_inicio),
            )
        )
    return records


def _contact_name(first_name: str | None, last_name: str | None) -> str:
    """Nombre y apellido del contacto tal como los guardó el adulto."""
    return " ".join(part.strip() for part in (first_name, last_name) if part and part.strip())


def _hash_source(record: VoiceMessageRecord) -> str:
    """Texto del que sale hash_solicitud de idempotencia_operacion (D-R3-14)."""
    return json.dumps(
        {
            "operacion": mapping.OP_CREAR_COMUNICACION,
            "tipo": TIPO_MENSAJE_VOZ,
            "adult_uid": record.adult_uid,
            "contact_id": record.contact_id,
            "from_adult": record.from_adult,
            "file_name": record.file_name,
            "size_bytes": record.size_bytes,
            "created_at": record.created_at.isoformat(),
        },
        sort_keys=True,
    )


_default_voice_message_repository = InMemoryVoiceMessageRepository()
_sql_voice_message_repository = db_session.lazy_sql_repository(SqlVoiceMessageRepository)


def get_voice_message_repository() -> VoiceMessageRepository:
    if db_session.sql_enabled():
        return _sql_voice_message_repository()
    return _default_voice_message_repository
