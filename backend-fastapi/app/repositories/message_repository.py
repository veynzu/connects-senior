import threading
from dataclasses import replace
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import session as db_session
from app.db.mapping import (
    T_ADULTO_CONTACTO,
    T_MENSAJE_TEXTO,
    T_USUARIO,
    as_utc,
    contact_user_id,
    ensure_user_id,
    id_str,
    parse_id,
)
from app.models.messages import MessageRecord
from app.repositories.errors import RecipientNotLinkedError


class MessageRepository(Protocol):
    """Acceso a los mensajes de texto entre cada adulto mayor y sus contactos (ADR-05)."""

    def add(self, record: MessageRecord) -> MessageRecord: ...

    def list_conversation(
        self, adult_uid: str, contact_id: str
    ) -> list[MessageRecord]: ...


class InMemoryMessageRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias para que modificar un MessageRecord recibido no altere lo
    guardado. Todos los métodos toman el mismo lock porque las rutas def corren en el
    threadpool; las lecturas recorren copias.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (adult_uid, contact_id) -> mensajes en orden de inserción.
        self._conversations: dict[tuple[str, str], list[MessageRecord]] = {}

    def add(self, record: MessageRecord) -> MessageRecord:
        stored = replace(record)
        with self._lock:
            self._conversations.setdefault(
                (stored.adult_uid, stored.contact_id), []
            ).append(stored)
        return replace(stored)

    def list_conversation(
        self, adult_uid: str, contact_id: str
    ) -> list[MessageRecord]:
        """Mensajes de la conversación por created_at ascendente.

        sorted es estable: con el mismo created_at se conserva el orden de inserción.
        """
        with self._lock:
            records = list(self._conversations.get((adult_uid, contact_id), []))
        ordered = sorted(records, key=lambda record: record.created_at)
        return [replace(record) for record in ordered]


_RECIPIENT_NOT_LINKED_DETAIL = (
    "El contacto no corresponde a un usuario de la plataforma distinto del adulto."
)
_CONTACT_NOT_OF_ADULT_DETAIL = "El contacto no pertenece a ese adulto."

# Dueño del contacto: adulto_contacto.adulto_mayor_id (sin filtrar estado, igual que la
# lectura de la conversación).
_SELECT_CONTACT_OWNER = text(
    f"SELECT adulto_mayor_id FROM {T_ADULTO_CONTACTO} "
    "WHERE id_adulto_contacto = :adulto_contacto_id"
)
_INSERT_MESSAGE = text(
    f"INSERT INTO {T_MENSAJE_TEXTO} "
    "(adulto_contacto_id, emisor_usuario_id, destinatario_usuario_id, contenido, "
    "enviado_en) "
    "VALUES (:adulto_contacto_id, :emisor, :destinatario, :contenido, :enviado_en) "
    "RETURNING id_mensaje_texto"
)
_SELECT_CONVERSATION = text(
    "SELECT mt.id_mensaje_texto, mt.adulto_contacto_id, mt.contenido, mt.enviado_en, "
    "mt.emisor_usuario_id = ac.adulto_mayor_id AS from_adult "
    f"FROM {T_MENSAJE_TEXTO} mt "
    f"JOIN {T_ADULTO_CONTACTO} ac ON ac.id_adulto_contacto = mt.adulto_contacto_id "
    f"JOIN {T_USUARIO} u ON u.id_usuario = ac.adulto_mayor_id "
    "WHERE mt.adulto_contacto_id = :adulto_contacto_id AND u.firebase_uid = :adult_uid "
    "ORDER BY mt.enviado_en ASC, mt.id_mensaje_texto ASC"
)


class SqlMessageRepository:
    """Mensajes de texto en connects_senior.mensaje_texto (ronda 3, D-R3-12, D-R3-26a).

    contact_id es adulto_contacto.id_adulto_contacto en texto; MessageRecord.id es
    mensaje_texto.id_mensaje_texto en texto. El emisor es siempre un usuario; desde
    V004 el destinatario admite NULL (mensaje del adulto a un contacto sin cuenta) y el
    CHECK emisor distinto del destinatario solo aplica cuando no es NULL. El usuario
    del contacto se resuelve por correo con mapping.contact_user_id (mapeo de
    almacenamiento, nunca de autorización ni de push). Cada método público corre en
    una única transacción; nunca se registra el texto de un mensaje.
    """

    def __init__(self, db: db_session.Database) -> None:
        self._db = db

    def add(self, record: MessageRecord) -> MessageRecord:
        """Guarda el mensaje y devuelve el registro recibido con el id de la base.

        from_adult True: emisor = adulto, destinatario = usuario del contacto, o NULL
        si el contacto no tiene usuario de la plataforma o ese usuario es el propio
        adulto (D-R3-26a: el adulto escribe a cualquier contacto, igual que en memoria).
        from_adult False: emisor = usuario del contacto, destinatario = adulto.
        enviado_en = record.created_at (nunca el default de la base). Lanza
        RecipientNotLinkedError, sin dejar nada escrito, si contact_id no es un id
        válido o el contacto no es de ese adulto y, con from_adult False, si el usuario
        del contacto no se resuelve o es el propio adulto (emisor_usuario_id es
        obligatorio).
        """
        adulto_contacto_id = parse_id(record.contact_id)
        if adulto_contacto_id is None:
            raise RecipientNotLinkedError(_CONTACT_NOT_OF_ADULT_DETAIL)

        with self._db.transaction() as conn:
            adult_id = ensure_user_id(conn, record.adult_uid)
            contact_user = self._contact_user(conn, adulto_contacto_id, adult_id)
            if record.from_adult:
                sender_id, recipient_id = adult_id, contact_user
            elif contact_user is None:
                raise RecipientNotLinkedError(_RECIPIENT_NOT_LINKED_DETAIL)
            else:
                sender_id, recipient_id = contact_user, adult_id
            new_id = conn.execute(
                _INSERT_MESSAGE,
                {
                    "adulto_contacto_id": adulto_contacto_id,
                    "emisor": sender_id,
                    "destinatario": recipient_id,
                    "contenido": record.text,
                    "enviado_en": record.created_at,
                },
            ).scalar_one()
        return replace(record, id=id_str(new_id))

    def list_conversation(
        self, adult_uid: str, contact_id: str
    ) -> list[MessageRecord]:
        """Mensajes de ese contacto del adulto, por enviado_en ascendente y luego por id.

        from_adult = el emisor es el adulto. Contacto de otro adulto, inexistente o id no
        numérico: lista vacía.
        """
        adulto_contacto_id = parse_id(contact_id)
        if adulto_contacto_id is None or not adult_uid:
            return []

        with self._db.transaction() as conn:
            rows = conn.execute(
                _SELECT_CONVERSATION,
                {"adulto_contacto_id": adulto_contacto_id, "adult_uid": adult_uid},
            ).all()
        return [
            MessageRecord(
                id=id_str(row.id_mensaje_texto),
                adult_uid=adult_uid,
                contact_id=id_str(row.adulto_contacto_id),
                text=row.contenido,
                created_at=as_utc(row.enviado_en),
                from_adult=bool(row.from_adult),
            )
            for row in rows
        ]

    @staticmethod
    def _contact_user(
        conn: Connection, adulto_contacto_id: int, adult_id: int
    ) -> int | None:
        """id_usuario del contacto del adulto, o None si no hay uno distinto del adulto.

        Lee adulto_contacto y usuario. Lanza RecipientNotLinkedError si el contacto no
        existe o es de otro adulto. Devuelve None si ningún usuario tiene su correo o
        si ese usuario es el propio adulto (CHECK emisor distinto del destinatario).
        """
        owner_id = conn.execute(
            _SELECT_CONTACT_OWNER, {"adulto_contacto_id": adulto_contacto_id}
        ).scalar_one_or_none()
        if owner_id is None or int(owner_id) != adult_id:
            raise RecipientNotLinkedError(_CONTACT_NOT_OF_ADULT_DETAIL)
        user_id = contact_user_id(conn, adulto_contacto_id)
        if user_id is None or user_id == adult_id:
            return None
        return user_id


_default_message_repository = InMemoryMessageRepository()
_sql_message_repository = db_session.lazy_sql_repository(SqlMessageRepository)


def get_message_repository() -> MessageRepository:
    if db_session.sql_enabled():
        return _sql_message_repository()
    return _default_message_repository
