import threading
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from app.db import session as db_session
from app.db.mapping import (
    T_ADULTO_CONTACTO,
    T_CONTACTO,
    T_USUARIO,
    ensure_user_id,
    id_str,
    parse_id,
    utc_now,
)
from app.models.contacts import Contact, ContactPermissions
from app.repositories.errors import ContactOrderConflictError


class ContactRepository(Protocol):
    """Acceso a los contactos de cada adulto mayor (ADR-05)."""

    def get(self, adult_uid: str, contact_id: str) -> Contact | None: ...

    def list_for_adult(self, adult_uid: str) -> list[Contact]: ...

    def add(self, adult_uid: str, contact: Contact) -> Contact: ...

    def update(self, adult_uid: str, contact: Contact) -> Contact | None: ...

    def delete(self, adult_uid: str, contact_id: str) -> bool: ...

    def find(self, contact_id: str) -> tuple[str, Contact] | None: ...


class InMemoryContactRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias para que modificar un Contact recibido no altere lo guardado.
    Todos los métodos toman el mismo lock porque el planificador lee desde el threadpool;
    las iteraciones recorren copias.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contacts: dict[str, dict[str, Contact]] = {}

    def get(self, adult_uid: str, contact_id: str) -> Contact | None:
        with self._lock:
            contact = self._contacts.get(adult_uid, {}).get(contact_id)
            if contact is None:
                return None
            return contact.model_copy(deep=True)

    def list_for_adult(self, adult_uid: str) -> list[Contact]:
        with self._lock:
            return [
                contact.model_copy(deep=True)
                for contact in list(self._contacts.get(adult_uid, {}).values())
            ]

    def add(self, adult_uid: str, contact: Contact) -> Contact:
        stored = contact.model_copy(deep=True)
        with self._lock:
            self._contacts.setdefault(adult_uid, {})[stored.id] = stored
            return stored.model_copy(deep=True)

    def update(self, adult_uid: str, contact: Contact) -> Contact | None:
        """Reemplaza completo el contacto con el mismo id; None si no existe para ese adulto."""
        stored = contact.model_copy(deep=True)
        with self._lock:
            adult_contacts = self._contacts.get(adult_uid)
            if adult_contacts is None or stored.id not in adult_contacts:
                return None
            adult_contacts[stored.id] = stored
            return stored.model_copy(deep=True)

    def delete(self, adult_uid: str, contact_id: str) -> bool:
        with self._lock:
            adult_contacts = self._contacts.get(adult_uid)
            if adult_contacts is None or contact_id not in adult_contacts:
                return False
            del adult_contacts[contact_id]
            return True

    def find(self, contact_id: str) -> tuple[str, Contact] | None:
        """Busca el contacto en todos los adultos y devuelve (adult_uid, contacto)."""
        with self._lock:
            for adult_uid, adult_contacts in list(self._contacts.items()):
                contact = adult_contacts.get(contact_id)
                if contact is not None:
                    return adult_uid, contact.model_copy(deep=True)
        return None


# --- PostgreSQL (plan ronda 3, D-R3-7 con la adaptación V004 de D-R3-26(d)) ---

# Índice único parcial (adulto_mayor_id, orden_favorito) de los favoritos activos del
# esquema V003. V004 lo quitó: la traducción a ContactOrderConflictError se conserva como
# ruta muerta inofensiva (D-R3-26(d)).
FAVORITE_ORDER_CONSTRAINT = "uq_adulto_contacto_orden_favorito"

_ESTADO_ACTIVO = "ACTIVO"
_ESTADO_INACTIVO = "INACTIVO"

# photo_url vive en adulto_contacto.foto_url (por adulto, V004); contacto.foto_url queda NULL.
_CONTACT_COLUMNS = (
    "ac.id_adulto_contacto, ac.nombre, ac.apellido, ac.relacion, ac.telefono, "
    "ac.correo, ac.foto_url, ac.es_favorito, ac.orden_favorito, ac.permite_llamadas, "
    "ac.permite_videollamadas, ac.permite_mensajes, ac.es_confianza"
)
_CONTACT_FROM = (
    f"FROM {T_ADULTO_CONTACTO} ac "
    f"JOIN {T_USUARIO} u ON u.id_usuario = ac.adulto_mayor_id "
)

_SELECT_CONTACT = text(
    f"SELECT {_CONTACT_COLUMNS} {_CONTACT_FROM}"
    "WHERE ac.id_adulto_contacto = :id AND u.firebase_uid = :adult_uid "
    f"AND ac.estado = '{_ESTADO_ACTIVO}'"
)
_SELECT_CONTACTS_OF_ADULT = text(
    f"SELECT {_CONTACT_COLUMNS} {_CONTACT_FROM}"
    f"WHERE u.firebase_uid = :adult_uid AND ac.estado = '{_ESTADO_ACTIVO}' "
    "ORDER BY ac.id_adulto_contacto"
)
_SELECT_CONTACT_ANY_ADULT = text(
    f"SELECT u.firebase_uid AS adult_uid, {_CONTACT_COLUMNS} {_CONTACT_FROM}"
    f"WHERE ac.id_adulto_contacto = :id AND ac.estado = '{_ESTADO_ACTIVO}'"
)
_INSERT_CONTACTO = text(
    f"INSERT INTO {T_CONTACTO} (usuario_id, foto_url, creado_en, actualizado_en) "
    "VALUES (NULL, NULL, :now, :now) "
    "RETURNING id_contacto"
)
_INSERT_ADULTO_CONTACTO = text(
    f"INSERT INTO {T_ADULTO_CONTACTO} "
    "(adulto_mayor_id, contacto_id, nombre, apellido, relacion, telefono, correo, "
    "foto_url, es_confianza, permite_llamadas, permite_videollamadas, permite_mensajes, "
    "es_favorito, orden_favorito, estado, configurado_por_id, creado_en, actualizado_en) "
    "VALUES (:adulto_id, :contacto_id, :nombre, :apellido, :relacion, :telefono, :correo, "
    ":foto_url, :es_confianza, :permite_llamadas, :permite_videollamadas, "
    f":permite_mensajes, :es_favorito, :orden_favorito, '{_ESTADO_ACTIVO}', :adulto_id, "
    ":now, :now) "
    "RETURNING id_adulto_contacto"
)
_UPDATE_ADULTO_CONTACTO = text(
    f"UPDATE {T_ADULTO_CONTACTO} SET "
    "nombre = :nombre, apellido = :apellido, relacion = :relacion, "
    "telefono = :telefono, correo = :correo, foto_url = :foto_url, "
    "es_confianza = :es_confianza, permite_llamadas = :permite_llamadas, "
    "permite_videollamadas = :permite_videollamadas, "
    "permite_mensajes = :permite_mensajes, es_favorito = :es_favorito, "
    "orden_favorito = :orden_favorito, "
    "actualizado_en = GREATEST(clock_timestamp(), creado_en) "
    f"WHERE id_adulto_contacto = :id AND estado = '{_ESTADO_ACTIVO}' "
    f"AND adulto_mayor_id = (SELECT id_usuario FROM {T_USUARIO} "
    "WHERE firebase_uid = :adult_uid) "
    "RETURNING id_adulto_contacto"
)
_DELETE_ADULTO_CONTACTO = text(
    f"UPDATE {T_ADULTO_CONTACTO} SET "
    f"estado = '{_ESTADO_INACTIVO}', es_favorito = false, orden_favorito = NULL, "
    "actualizado_en = GREATEST(clock_timestamp(), creado_en) "
    f"WHERE id_adulto_contacto = :id AND estado = '{_ESTADO_ACTIVO}' "
    f"AND adulto_mayor_id = (SELECT id_usuario FROM {T_USUARIO} "
    "WHERE firebase_uid = :adult_uid) "
    "RETURNING id_adulto_contacto"
)


class SqlContactRepository:
    """Contactos en connects_senior.contacto + adulto_contacto (D-R3-7).

    Contact.id = adulto_contacto.id_adulto_contacto en texto; cada contacto tiene su propia
    fila en contacto (usuario_id NULL y foto_url NULL: el vínculo contacto -> usuario sigue
    siendo por correo verificado en los servicios, D-R2-3, y photo_url se guarda por adulto
    en adulto_contacto.foto_url, V004). C4 es una baja lógica (estado INACTIVO) y todas las
    lecturas ven solo los ACTIVO. Un id no numérico se trata como inexistente sin consultar.
    Con el índice único de favoritos de V003 (ya no está en V004) un favorite_order repetido
    lanzaría ContactOrderConflictError (la transacción se revierte sola al salir del bloque).
    """

    def __init__(self, db: db_session.Database) -> None:
        self._db = db

    def get(self, adult_uid: str, contact_id: str) -> Contact | None:
        row_id = parse_id(contact_id)
        if row_id is None or not adult_uid:
            return None
        with self._db.transaction() as conn:
            row = conn.execute(
                _SELECT_CONTACT, {"id": row_id, "adult_uid": adult_uid}
            ).mappings().one_or_none()
        return None if row is None else _contact_from_row(row)

    def list_for_adult(self, adult_uid: str) -> list[Contact]:
        if not adult_uid:
            return []
        with self._db.transaction() as conn:
            rows = conn.execute(
                _SELECT_CONTACTS_OF_ADULT, {"adult_uid": adult_uid}
            ).mappings().all()
        return [_contact_from_row(row) for row in rows]

    def add(self, adult_uid: str, contact: Contact) -> Contact:
        """Crea contacto + adulto_contacto; devuelve lo recibido con el id nuevo (D-R3-4)."""
        try:
            with self._db.transaction() as conn:
                new_id = _insert_contact(conn, adult_uid, contact)
        except IntegrityError as exc:
            _raise_order_conflict(exc)
            raise
        return contact.model_copy(update={"id": id_str(new_id)}, deep=True)

    def update(self, adult_uid: str, contact: Contact) -> Contact | None:
        """Reescribe el contacto activo del adulto; devuelve el registro releído o None."""
        row_id = parse_id(contact.id)
        if row_id is None or not adult_uid:
            return None
        try:
            with self._db.transaction() as conn:
                return _update_contact(conn, adult_uid, row_id, contact)
        except IntegrityError as exc:
            _raise_order_conflict(exc)
            raise

    def delete(self, adult_uid: str, contact_id: str) -> bool:
        """Baja lógica: estado INACTIVO y sin favorito; False si no existe o ya estaba de baja."""
        row_id = parse_id(contact_id)
        if row_id is None or not adult_uid:
            return False
        with self._db.transaction() as conn:
            deleted = conn.execute(
                _DELETE_ADULTO_CONTACTO, {"id": row_id, "adult_uid": adult_uid}
            ).scalar_one_or_none()
        return deleted is not None

    def find(self, contact_id: str) -> tuple[str, Contact] | None:
        """Contacto activo con ese id en cualquier adulto: (uid del adulto, contacto)."""
        row_id = parse_id(contact_id)
        if row_id is None:
            return None
        with self._db.transaction() as conn:
            row = conn.execute(
                _SELECT_CONTACT_ANY_ADULT, {"id": row_id}
            ).mappings().one_or_none()
        if row is None:
            return None
        return row["adult_uid"], _contact_from_row(row)


def _insert_contact(conn: Connection, adult_uid: str, contact: Contact) -> int:
    adult_id = ensure_user_id(conn, adult_uid)
    now = utc_now()
    contacto_id = conn.execute(_INSERT_CONTACTO, {"now": now}).scalar_one()
    new_id = conn.execute(
        _INSERT_ADULTO_CONTACTO,
        {
            **_contact_params(contact),
            "adulto_id": adult_id,
            "contacto_id": int(contacto_id),
            "now": now,
        },
    ).scalar_one()
    return int(new_id)


def _update_contact(
    conn: Connection, adult_uid: str, row_id: int, contact: Contact
) -> Contact | None:
    updated_id = conn.execute(
        _UPDATE_ADULTO_CONTACTO,
        {**_contact_params(contact), "id": row_id, "adult_uid": adult_uid},
    ).scalar_one_or_none()
    if updated_id is None:
        return None
    row = conn.execute(
        _SELECT_CONTACT, {"id": row_id, "adult_uid": adult_uid}
    ).mappings().one_or_none()
    return None if row is None else _contact_from_row(row)


def _contact_params(contact: Contact) -> dict[str, object]:
    """Columnas de adulto_contacto que salen del Contact (sin ids ni fechas)."""
    permissions = contact.permissions
    return {
        "nombre": contact.first_name,
        "apellido": contact.last_name,
        "relacion": contact.relationship,
        "telefono": _blank_to_none(contact.phone),
        "correo": _blank_to_none(contact.email),
        "foto_url": _blank_to_none(contact.photo_url),
        "es_confianza": permissions.help_alerts,
        "permite_llamadas": permissions.calls,
        "permite_videollamadas": permissions.video_calls,
        "permite_mensajes": permissions.messages,
        "es_favorito": contact.is_favorite,
        "orden_favorito": _stored_favorite_order(contact),
    }


def _stored_favorite_order(contact: Contact) -> int | None:
    """orden_favorito: solo un favorito con orden >= 1 lo guarda; el resto NULL.

    El CHECK de V004 también admite 0 en un no favorito; se guarda NULL para no depender de
    esa versión del esquema (D-R3-26(d)). Al leer, NULL -> 0.
    """
    if contact.is_favorite and contact.favorite_order >= 1:
        return contact.favorite_order
    return None


def _blank_to_none(value: str | None) -> str | None:
    """telefono y correo admiten NULL pero no texto vacío (CHECK del esquema); foto_url igual.

    Los Request ya convierten "" en None (blank_to_none): en la práctica llega None o una URL.
    """
    if value is None or not value.strip():
        return None
    return value


def _contact_from_row(row: RowMapping) -> Contact:
    return Contact(
        id=id_str(row["id_adulto_contacto"]),
        first_name=row["nombre"],
        last_name=row["apellido"],
        relationship=row["relacion"],
        phone=row["telefono"],
        email=row["correo"],
        photo_url=row["foto_url"],
        is_favorite=bool(row["es_favorito"]),
        favorite_order=row["orden_favorito"] or 0,
        permissions=ContactPermissions(
            calls=bool(row["permite_llamadas"]),
            video_calls=bool(row["permite_videollamadas"]),
            messages=bool(row["permite_mensajes"]),
            help_alerts=bool(row["es_confianza"]),
        ),
    )


def _raise_order_conflict(exc: IntegrityError) -> None:
    """Traduce la violación de uq_adulto_contacto_orden_favorito; con otras no hace nada.

    El índice era de V003; con V004 esta rama no se alcanza (D-R3-26(d)).

    Se llama FUERA del bloque de transacción: el SAVEPOINT o la transacción ya se
    revirtieron y la conexión sigue utilizable. Sin encadenar la causa: su texto trae la
    sentencia SQL y el DETAIL de Postgres.
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    if getattr(diag, "constraint_name", None) == FAVORITE_ORDER_CONSTRAINT:
        raise ContactOrderConflictError(
            "Otro contacto favorito del adulto ya usa ese orden."
        ) from None


_default_contact_repository = InMemoryContactRepository()
_sql_contact_repository = db_session.lazy_sql_repository(SqlContactRepository)


def get_contact_repository() -> ContactRepository:
    if db_session.sql_enabled():
        return _sql_contact_repository()
    return _default_contact_repository
