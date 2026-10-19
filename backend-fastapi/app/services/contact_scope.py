"""Alcance de un contacto para mensajes de texto y de voz (M1, M2, V1, V2; P19, D-R2-12).

Resuelve, a partir del usuario del token y de un contact_id, a qué adulto pertenece
el contacto y en qué sentido habla el llamante:

- adulto_mayor: solo sus propios contactos (ajeno o inexistente -> no existe);
  habla como el adulto (from_adult = True).
- familiar o cuidador: un contacto de un adulto vinculado (si no -> no existe) cuyo
  email coincide con el email verificado del token (si no -> ContactNotLinkedError);
  habla como el contacto (from_adult = False).
- administrador o usuario sin rol: sin permiso (defensa en profundidad, P7 y D-02.4).

No conoce FastAPI: lanza excepciones de dominio que los routers traducen
(ContactScopeNotFoundError -> 404 "El contacto no existe."; ContactScopeForbiddenError
-> 403 FORBIDDEN_DETAIL; ContactNotLinkedError -> 403 en M1 y V1, lista vacía en M2 y V2).
"""
from dataclasses import dataclass

from app.core.security import FORBIDDEN_DETAIL, can_access_adult
from app.models.auth import AuthenticatedUser
from app.models.contacts import Contact
from app.models.enums import Role
from app.models.users import normalize_email
from app.repositories.contact_repository import ContactRepository
from app.repositories.user_repository import UserRepository


CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."


@dataclass(frozen=True)
class ContactScope:
    """Contacto resuelto dentro del ámbito del llamante.

    from_adult es True cuando el llamante es el adulto dueño del contacto.
    """

    adult_uid: str
    contact: Contact
    from_adult: bool


class ContactScopeNotFoundError(Exception):
    """El contacto no existe o pertenece a un adulto fuera del alcance del llamante."""


class ContactScopeForbiddenError(Exception):
    """El llamante no tiene un rol que pueda usar contactos (administrador o sin rol)."""


class ContactNotLinkedError(ContactScopeForbiddenError):
    """Familiar o cuidador cuyo email verificado no está vinculado al contacto."""


def _contact_matches_user(contact: Contact, user: AuthenticatedUser) -> bool:
    """El email del contacto es el email verificado del token (comparación normalizada).

    Un email vacío nunca coincide, ni siquiera con otro vacío.
    """
    if user.email_verified is not True or not user.email or not contact.email:
        return False
    contact_email = normalize_email(contact.email)
    if not contact_email:
        return False
    return contact_email == normalize_email(user.email)


def resolve_contact_scope(
    user: AuthenticatedUser,
    contact_id: str,
    contacts: ContactRepository,
    users: UserRepository,
) -> ContactScope:
    """Resuelve el contacto contact_id para el llamante (ver docstring del módulo).

    Lanza ContactScopeForbiddenError (administrador o sin rol, antes de buscar nada),
    ContactScopeNotFoundError (inexistente o fuera del alcance, sin revelar existencia)
    o ContactNotLinkedError (familiar o cuidador no vinculado por email al contacto).
    """
    if user.role == Role.ADULTO_MAYOR:
        contact = contacts.get(user.uid, contact_id)
        if contact is None:
            raise ContactScopeNotFoundError(CONTACT_NOT_FOUND_DETAIL)
        return ContactScope(adult_uid=user.uid, contact=contact, from_adult=True)

    if user.role in (Role.FAMILIAR, Role.CUIDADOR):
        found = contacts.find(contact_id)
        if found is None:
            raise ContactScopeNotFoundError(CONTACT_NOT_FOUND_DETAIL)
        adult_uid, contact = found
        if not can_access_adult(user, adult_uid, users):
            raise ContactScopeNotFoundError(CONTACT_NOT_FOUND_DETAIL)
        if not _contact_matches_user(contact, user):
            raise ContactNotLinkedError(FORBIDDEN_DETAIL)
        return ContactScope(adult_uid=adult_uid, contact=contact, from_adult=False)

    raise ContactScopeForbiddenError(FORBIDDEN_DETAIL)


def recipient_contacts_for_user(
    user: AuthenticatedUser,
    contacts: ContactRepository,
    users: UserRepository,
) -> list[tuple[str, Contact]]:
    """Contactos, de los adultos vinculados al llamante, que corresponden a su email verificado.

    Devuelve pares (adult_uid, contacto) en el orden de los vínculos y de los contactos.
    adulto_mayor: lista vacía (no es contacto de nadie en este modelo). Familiar o
    cuidador sin email verificado: lista vacía. Administrador o sin rol:
    ContactScopeForbiddenError.
    """
    if user.role == Role.ADULTO_MAYOR:
        return []

    if user.role in (Role.FAMILIAR, Role.CUIDADOR):
        if user.email_verified is not True or not user.email:
            return []
        return [
            (adult_uid, contact)
            for adult_uid in users.adults_of(user.uid)
            for contact in contacts.list_for_adult(adult_uid)
            if _contact_matches_user(contact, user)
        ]

    raise ContactScopeForbiddenError(FORBIDDEN_DETAIL)
