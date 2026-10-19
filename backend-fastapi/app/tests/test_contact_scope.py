import dataclasses

import pytest

from app.core.security import FORBIDDEN_DETAIL
from app.models.auth import AuthenticatedUser
from app.models.contacts import Contact, ContactPermissions
from app.models.enums import Role
from app.repositories.contact_repository import InMemoryContactRepository
from app.repositories.user_repository import InMemoryUserRepository
from app.services.contact_scope import (
    CONTACT_NOT_FOUND_DETAIL,
    ContactNotLinkedError,
    ContactScope,
    ContactScopeForbiddenError,
    ContactScopeNotFoundError,
    recipient_contacts_for_user,
    resolve_contact_scope,
)


ADULT_UID = "adult-uid"
OTHER_ADULT_UID = "other-adult-uid"
THIRD_ADULT_UID = "third-adult-uid"
FAMILY_UID = "family-uid"
CAREGIVER_UID = "caregiver-uid"
FAMILY_EMAIL = "hija@example.com"
CONTACT_ID = "contact-hija"
OTHER_CONTACT_ID = "contact-vecino"
FOREIGN_CONTACT_ID = "contact-of-other-adult"
THIRD_CONTACT_ID = "contact-hija-third-adult"


def _contact(contact_id: str, email: str | None, first_name: str = "Ana") -> Contact:
    return Contact(
        id=contact_id,
        first_name=first_name,
        last_name="Gómez",
        relationship="hija",
        email=email,
        permissions=ContactPermissions(
            calls=True, video_calls=True, messages=True, help_alerts=False
        ),
    )


def _user(
    role: Role | None,
    uid: str = FAMILY_UID,
    email: str | None = FAMILY_EMAIL,
    email_verified: bool | None = True,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=email,
        email_verified=email_verified,
        display_name="Ana Gómez",
        role=role,
    )


def _adult() -> AuthenticatedUser:
    return _user(Role.ADULTO_MAYOR, uid=ADULT_UID, email="rosa@example.com")


@pytest.fixture
def contacts() -> InMemoryContactRepository:
    repository = InMemoryContactRepository()
    # El email del contacto lleva mayúsculas y espacios: se compara normalizado.
    repository.add(ADULT_UID, _contact(CONTACT_ID, "  Hija@Example.COM "))
    repository.add(ADULT_UID, _contact(OTHER_CONTACT_ID, "vecino@example.com", "Luis"))
    repository.add(OTHER_ADULT_UID, _contact(FOREIGN_CONTACT_ID, FAMILY_EMAIL))
    repository.add(THIRD_ADULT_UID, _contact(THIRD_CONTACT_ID, FAMILY_EMAIL, "Ana M."))
    return repository


@pytest.fixture
def users() -> InMemoryUserRepository:
    repository = InMemoryUserRepository()
    repository.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    repository.link_adult(FAMILY_UID, THIRD_ADULT_UID, Role.FAMILIAR)
    repository.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)
    return repository


# --- resolve_contact_scope: adulto_mayor ---


def test_resolve_contact_scope_adult_own_contact(contacts, users):
    scope = resolve_contact_scope(_adult(), CONTACT_ID, contacts, users)

    assert scope == ContactScope(
        adult_uid=ADULT_UID,
        contact=contacts.get(ADULT_UID, CONTACT_ID),
        from_adult=True,
    )
    assert scope.contact.id == CONTACT_ID


def test_resolve_contact_scope_adult_does_not_need_verified_email(contacts, users):
    adult = _user(Role.ADULTO_MAYOR, uid=ADULT_UID, email=None, email_verified=False)

    scope = resolve_contact_scope(adult, OTHER_CONTACT_ID, contacts, users)

    assert scope.adult_uid == ADULT_UID
    assert scope.from_adult is True


def test_resolve_contact_scope_adult_foreign_contact_not_found(contacts, users):
    with pytest.raises(ContactScopeNotFoundError) as excinfo:
        resolve_contact_scope(_adult(), FOREIGN_CONTACT_ID, contacts, users)

    assert str(excinfo.value) == CONTACT_NOT_FOUND_DETAIL
    assert CONTACT_NOT_FOUND_DETAIL == "El contacto no existe."


def test_resolve_contact_scope_adult_missing_contact_not_found(contacts, users):
    with pytest.raises(ContactScopeNotFoundError):
        resolve_contact_scope(_adult(), "no-existe", contacts, users)


# --- resolve_contact_scope: familiar y cuidador ---


@pytest.mark.parametrize(
    ("role", "uid"),
    [(Role.FAMILIAR, FAMILY_UID), (Role.CUIDADOR, CAREGIVER_UID)],
)
def test_resolve_contact_scope_family_or_caregiver_linked_by_verified_email(
    contacts, users, role, uid
):
    scope = resolve_contact_scope(_user(role, uid=uid), CONTACT_ID, contacts, users)

    assert scope.adult_uid == ADULT_UID
    assert scope.contact.id == CONTACT_ID
    assert scope.from_adult is False


def test_resolve_contact_scope_family_email_compared_normalized(contacts, users):
    family = _user(Role.FAMILIAR, email="  HIJA@example.com")

    scope = resolve_contact_scope(family, CONTACT_ID, contacts, users)

    assert scope.contact.id == CONTACT_ID


@pytest.mark.parametrize("email_verified", [False, None])
def test_resolve_contact_scope_family_unverified_email_not_linked(
    contacts, users, email_verified
):
    family = _user(Role.FAMILIAR, email_verified=email_verified)

    with pytest.raises(ContactNotLinkedError) as excinfo:
        resolve_contact_scope(family, CONTACT_ID, contacts, users)

    assert str(excinfo.value) == FORBIDDEN_DETAIL


def test_resolve_contact_scope_family_different_email_not_linked(contacts, users):
    with pytest.raises(ContactNotLinkedError):
        resolve_contact_scope(
            _user(Role.FAMILIAR), OTHER_CONTACT_ID, contacts, users
        )


def test_resolve_contact_scope_family_without_token_email_not_linked(contacts, users):
    family = _user(Role.FAMILIAR, email=None)

    with pytest.raises(ContactNotLinkedError):
        resolve_contact_scope(family, CONTACT_ID, contacts, users)


@pytest.mark.parametrize("contact_email", [None, "", "   "])
def test_resolve_contact_scope_contact_without_email_not_linked(
    users, contact_email
):
    contacts = InMemoryContactRepository()
    contacts.add(ADULT_UID, _contact("sin-email", contact_email))

    # Ni siquiera un token con email vacío coincide con un contacto sin email.
    for family in (_user(Role.FAMILIAR), _user(Role.FAMILIAR, email="")):
        with pytest.raises(ContactNotLinkedError):
            resolve_contact_scope(family, "sin-email", contacts, users)


def test_resolve_contact_scope_family_unlinked_adult_not_found(contacts, users):
    # El email coincide, pero el adulto dueño del contacto no está vinculado al familiar.
    with pytest.raises(ContactScopeNotFoundError) as excinfo:
        resolve_contact_scope(
            _user(Role.FAMILIAR), FOREIGN_CONTACT_ID, contacts, users
        )

    assert not isinstance(excinfo.value, ContactScopeForbiddenError)


def test_resolve_contact_scope_caregiver_unlinked_adult_not_found(contacts, users):
    with pytest.raises(ContactScopeNotFoundError):
        resolve_contact_scope(
            _user(Role.CUIDADOR, uid=CAREGIVER_UID), THIRD_CONTACT_ID, contacts, users
        )


def test_resolve_contact_scope_family_missing_contact_not_found(contacts, users):
    with pytest.raises(ContactScopeNotFoundError):
        resolve_contact_scope(_user(Role.FAMILIAR), "no-existe", contacts, users)


def test_resolve_contact_scope_unlinked_adult_checked_before_email(contacts, users):
    # Sin vínculo con el adulto el resultado es 404 aunque el email no esté verificado:
    # nunca se revela que el contacto existe.
    family = _user(Role.FAMILIAR, email_verified=False)

    with pytest.raises(ContactScopeNotFoundError):
        resolve_contact_scope(family, FOREIGN_CONTACT_ID, contacts, users)


@pytest.mark.parametrize("role", [Role.ADMINISTRADOR, None])
def test_resolve_contact_scope_admin_and_no_role_forbidden(contacts, users, role):
    with pytest.raises(ContactScopeForbiddenError) as excinfo:
        resolve_contact_scope(_user(role), CONTACT_ID, contacts, users)

    assert not isinstance(excinfo.value, ContactNotLinkedError)
    assert str(excinfo.value) == FORBIDDEN_DETAIL


@pytest.mark.parametrize("role", [Role.ADMINISTRADOR, None])
def test_resolve_contact_scope_admin_forbidden_even_for_missing_contact(
    contacts, users, role
):
    with pytest.raises(ContactScopeForbiddenError):
        resolve_contact_scope(_user(role), "no-existe", contacts, users)


def test_contact_not_linked_error_is_forbidden_error():
    assert issubclass(ContactNotLinkedError, ContactScopeForbiddenError)
    assert not issubclass(ContactScopeNotFoundError, ContactScopeForbiddenError)


def test_contact_scope_is_frozen(contacts, users):
    scope = resolve_contact_scope(_adult(), CONTACT_ID, contacts, users)

    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.from_adult = False


# --- recipient_contacts_for_user ---


def test_recipient_contacts_for_user_verified_across_several_adults(contacts, users):
    result = recipient_contacts_for_user(_user(Role.FAMILIAR), contacts, users)

    # Solo adultos vinculados (OTHER_ADULT_UID no), en el orden de los vínculos.
    assert [(adult_uid, contact.id) for adult_uid, contact in result] == [
        (ADULT_UID, CONTACT_ID),
        (THIRD_ADULT_UID, THIRD_CONTACT_ID),
    ]


def test_recipient_contacts_for_user_caregiver_verified(contacts, users):
    caregiver = _user(Role.CUIDADOR, uid=CAREGIVER_UID)

    result = recipient_contacts_for_user(caregiver, contacts, users)

    assert [(adult_uid, contact.id) for adult_uid, contact in result] == [
        (ADULT_UID, CONTACT_ID)
    ]


@pytest.mark.parametrize("email_verified", [False, None])
def test_recipient_contacts_for_user_unverified_returns_empty(
    contacts, users, email_verified
):
    family = _user(Role.FAMILIAR, email_verified=email_verified)

    assert recipient_contacts_for_user(family, contacts, users) == []


def test_recipient_contacts_for_user_without_email_returns_empty(contacts, users):
    family = _user(Role.FAMILIAR, email=None)

    assert recipient_contacts_for_user(family, contacts, users) == []


def test_recipient_contacts_for_user_without_links_returns_empty(contacts):
    # Su email coincide con contactos, pero no tiene vínculos con ningún adulto.
    assert (
        recipient_contacts_for_user(
            _user(Role.FAMILIAR), contacts, InMemoryUserRepository()
        )
        == []
    )


def test_recipient_contacts_for_user_without_matching_contacts_returns_empty(
    contacts, users
):
    family = _user(Role.FAMILIAR, email="nadie@example.com")

    assert recipient_contacts_for_user(family, contacts, users) == []


def test_recipient_contacts_adult_returns_empty(contacts, users):
    assert recipient_contacts_for_user(_adult(), contacts, users) == []


def test_recipient_contacts_admin_raises_forbidden(contacts, users):
    with pytest.raises(ContactScopeForbiddenError) as excinfo:
        recipient_contacts_for_user(_user(Role.ADMINISTRADOR), contacts, users)

    assert str(excinfo.value) == FORBIDDEN_DETAIL


def test_recipient_contacts_no_role_raises_forbidden(contacts, users):
    with pytest.raises(ContactScopeForbiddenError):
        recipient_contacts_for_user(_user(None), contacts, users)
