"""Servicio de contactos (C1-C4; RF-07, RF-08, RF-09, RF-22).

No conoce FastAPI: lanza excepciones de dominio que el router traduce.
Permisos (D-R2-7, D-R2-8):
- adulto_mayor: sus propios contactos (adulto deducido del token, P6).
- familiar: contactos de sus adultos vinculados.
- cuidador: lee los de sus adultos vinculados y escribe (C2-C4) solo con manage_contacts.
- administrador o sin rol: nunca (el router ya responde 403; aquí es defensa en profundidad).
En C3 y C4 un contacto inexistente o de un adulto no vinculado da el mismo error
(404 sin revelar existencia, P18).
Con PostgreSQL, un favorite_order ya usado por otro favorito activo del mismo adulto
lanza ContactOrderInUseError (índice único de la base, plan ronda 3, D-R3-7).
"""
import unicodedata
from uuid import uuid4

from app.core.security import can_access_adult
from app.models.auth import AuthenticatedUser
from app.models.contacts import Contact, ContactCreateRequest, ContactUpdateRequest
from app.models.enums import Role
from app.repositories.contact_repository import ContactRepository
from app.repositories.errors import ContactOrderConflictError
from app.repositories.user_repository import UserRepository


class ContactNotFoundError(Exception):
    """El contacto no existe o pertenece a un adulto al que el usuario no tiene acceso."""


class ContactPermissionDeniedError(Exception):
    """El usuario no puede gestionar los contactos de ese adulto."""


class ContactOrderInUseError(Exception):
    """Otro contacto favorito del mismo adulto ya usa ese favorite_order (D-R3-7)."""


def list_contacts(adult_uid: str, contacts: ContactRepository) -> list[Contact]:
    """C1: contactos del adulto ya resuelto por resolve_adult_scope, en el orden del contrato."""
    return sort_contacts(contacts.list_for_adult(adult_uid))


def create_contact(
    user: AuthenticatedUser,
    adult_uid: str,
    payload: ContactCreateRequest,
    contacts: ContactRepository,
    users: UserRepository,
) -> Contact:
    """C2: crea el contacto del adulto; nace sin marcar como favorito (orden 0)."""
    if not can_access_adult(user, adult_uid, users):
        raise ContactPermissionDeniedError("No tienes permiso para esta acción.")
    _ensure_can_manage_contacts(user, adult_uid, users)

    contact = Contact(
        id=str(uuid4()),
        first_name=payload.first_name,
        last_name=payload.last_name,
        relationship=payload.relationship,
        phone=payload.phone,
        email=payload.email,
        photo_url=payload.photo_url,
        is_favorite=False,
        favorite_order=0,
        permissions=payload.permissions.model_copy(),
    )
    # Siempre el registro que devuelve el repositorio (D-R2-17).
    try:
        return contacts.add(adult_uid, contact)
    except ContactOrderConflictError as error:
        raise ContactOrderInUseError("El orden de favorito ya está en uso.") from error


def update_contact(
    user: AuthenticatedUser,
    contact_id: str,
    payload: ContactUpdateRequest,
    contacts: ContactRepository,
    users: UserRepository,
) -> Contact:
    """C3: aplica solo los campos enviados; null en phone, email o photo_url los borra."""
    adult_uid, current = _find_accessible_contact(user, contact_id, contacts, users)
    _ensure_can_manage_contacts(user, adult_uid, users)

    changes = payload.model_dump(exclude_unset=True)
    # model_validate (nunca model_copy(update=), que no valida) sobre el contacto completo.
    updated = Contact.model_validate({**current.model_dump(), **changes})
    try:
        stored = contacts.update(adult_uid, updated)
    except ContactOrderConflictError as error:
        raise ContactOrderInUseError("El orden de favorito ya está en uso.") from error
    if stored is None:
        # Se borró entre la lectura y la escritura.
        raise ContactNotFoundError("El contacto no existe.")
    return stored


def delete_contact(
    user: AuthenticatedUser,
    contact_id: str,
    contacts: ContactRepository,
    users: UserRepository,
) -> None:
    """C4: borra el contacto."""
    adult_uid, _ = _find_accessible_contact(user, contact_id, contacts, users)
    _ensure_can_manage_contacts(user, adult_uid, users)

    if not contacts.delete(adult_uid, contact_id):
        raise ContactNotFoundError("El contacto no existe.")


def sort_contacts(contacts: list[Contact]) -> list[Contact]:
    """Favoritos primero por favorite_order y luego por nombre; después el resto por nombre.

    Sin tildes ni mayúsculas en la comparación de nombres (D-R2-9); orden estable.
    """
    return sorted(contacts, key=_sort_key)


def _sort_key(contact: Contact) -> tuple[bool, int, str, str]:
    return (
        not contact.is_favorite,
        contact.favorite_order if contact.is_favorite else 0,
        _name_key(contact.first_name),
        _name_key(contact.last_name),
    )


def _name_key(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    ).casefold()


def _find_accessible_contact(
    user: AuthenticatedUser,
    contact_id: str,
    contacts: ContactRepository,
    users: UserRepository,
) -> tuple[str, Contact]:
    found = contacts.find(contact_id)
    if found is None:
        raise ContactNotFoundError("El contacto no existe.")
    adult_uid, contact = found
    if not can_access_adult(user, adult_uid, users):
        raise ContactNotFoundError("El contacto no existe.")
    return adult_uid, contact


def _ensure_can_manage_contacts(
    user: AuthenticatedUser,
    adult_uid: str,
    users: UserRepository,
) -> None:
    """El cuidador escribe contactos solo con manage_contacts en su vínculo (D-R2-8)."""
    if user.role != Role.CUIDADOR:
        return
    link = users.link_of(user.uid, adult_uid)
    if link is None or link.permissions is None or not link.permissions.manage_contacts:
        raise ContactPermissionDeniedError("No tienes permiso para esta acción.")
