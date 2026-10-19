"""Tests de contactos C1-C4 (tarea 2): rutas y servicio con repositorios InMemory reales."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.security import (
    ADULT_ID_REQUIRED_DETAIL,
    FORBIDDEN_DETAIL,
    get_current_user,
)
from app.main import app
from app.models.auth import AuthenticatedUser
from app.models.contacts import (
    Contact,
    ContactCreateRequest,
    ContactPermissions,
    ContactUpdateRequest,
)
from app.models.enums import Role
from app.models.users import CaregiverPermissions
from app.repositories.contact_repository import (
    InMemoryContactRepository,
    get_contact_repository,
)
from app.repositories.contact_repository import (
    FAVORITE_ORDER_CONSTRAINT,
    SqlContactRepository,
)
from app.repositories.errors import ContactOrderConflictError
from app.repositories.user_repository import (
    InMemoryUserRepository,
    get_user_repository,
)
from app.services.contact_service import (
    ContactNotFoundError,
    ContactOrderInUseError,
    ContactPermissionDeniedError,
    create_contact,
    delete_contact,
    list_contacts,
    sort_contacts,
    update_contact,
)


CONTACTS_PATH = "/api/contacts"
CONTACT_PATH_TEMPLATE = "/api/contacts/{contact_id}"
ADULT_UID = "adult-uid"
OTHER_ADULT_UID = "other-adult-uid"
FAMILY_UID = "family-uid"
CAREGIVER_UID = "caregiver-uid"
ADMIN_UID = "admin-uid"
NO_ROLE_UID = "no-role-uid"
CONTACT_ID = "contact-1"
FOREIGN_CONTACT_ID = "contact-of-other-adult"
CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."
BEARER_DETAIL = "Debes enviar un token Bearer."
CONTACT_KEYS = {
    "id",
    "first_name",
    "last_name",
    "relationship",
    "phone",
    "email",
    "photo_url",
    "is_favorite",
    "favorite_order",
    "permissions",
}
ALL_PERMISSIONS = {
    "calls": True,
    "video_calls": True,
    "messages": True,
    "help_alerts": True,
}
MIXED_PERMISSIONS = {
    "calls": True,
    "video_calls": False,
    "messages": True,
    "help_alerts": False,
}


@pytest.fixture(autouse=True)
def state():
    shared = SimpleNamespace(
        contacts=InMemoryContactRepository(),
        users=InMemoryUserRepository(),
    )
    app.dependency_overrides[get_contact_repository] = lambda: shared.contacts
    app.dependency_overrides[get_user_repository] = lambda: shared.users
    yield shared
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _user(role: Role | None, uid: str = ADULT_UID) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=f"{uid}@example.com",
        email_verified=True,
        display_name="Usuario de prueba",
        role=role,
    )


def _as_user(role: Role | None, uid: str = ADULT_UID) -> AuthenticatedUser:
    user = _user(role, uid)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _contact_path(contact_id: str = CONTACT_ID) -> str:
    return CONTACT_PATH_TEMPLATE.format(contact_id=contact_id)


def _contact(
    contact_id: str = CONTACT_ID,
    first_name: str = "Ana",
    last_name: str = "Pérez",
    *,
    is_favorite: bool = False,
    favorite_order: int = 0,
    phone: str | None = "+573001234567",
    email: str | None = "ana@example.com",
    photo_url: str | None = "https://example.com/ana.png",
) -> Contact:
    return Contact(
        id=contact_id,
        first_name=first_name,
        last_name=last_name,
        relationship="hija",
        phone=phone,
        email=email,
        photo_url=photo_url,
        is_favorite=is_favorite,
        favorite_order=favorite_order,
        permissions=ContactPermissions(**ALL_PERMISSIONS),
    )


def _create_body(**overrides) -> dict:
    body = {
        "first_name": "Ana",
        "last_name": "Pérez",
        "relationship": "hija",
        "phone": "+573001234567",
        "email": "Ana.Perez@Example.com",
        "photo_url": "https://example.com/ana.png",
        "permissions": dict(MIXED_PERMISSIONS),
    }
    body.update(overrides)
    return body


def _link_caregiver(state, *, manage_contacts: bool, adult_uid: str = ADULT_UID) -> None:
    state.users.link_adult(
        CAREGIVER_UID,
        adult_uid,
        Role.CUIDADOR,
        CaregiverPermissions(
            manage_contacts=manage_contacts,
            manage_reminders=True,
            receive_alerts=True,
        ),
    )


def _seed_contact(state, adult_uid: str = ADULT_UID, **kwargs) -> Contact:
    return state.contacts.add(adult_uid, _contact(**kwargs))


# --- Servicio: sort_contacts ---


def test_sort_contacts_service_orders_favorites_then_names():
    contacts = [
        _contact("plain-zoe", "Zoe"),
        _contact("fav-2", "Beto", is_favorite=True, favorite_order=2),
        _contact("plain-angela", "Ángela"),
        _contact("fav-1", "Zoe", is_favorite=True, favorite_order=1),
        _contact("plain-high-order", "Carlos", favorite_order=9),
        _contact("plain-ana", "ana"),
    ]

    result = sort_contacts(contacts)

    assert [contact.id for contact in result] == [
        "fav-1",
        "fav-2",
        "plain-ana",
        "plain-angela",
        "plain-high-order",
        "plain-zoe",
    ]


def test_sort_contacts_service_uses_last_name_and_is_stable():
    contacts = [
        _contact("perez-2", "Ana", "Pérez"),
        _contact("gomez", "Ana", "gómez"),
        _contact("perez-1", "Ana", "Perez"),
    ]

    result = sort_contacts(contacts)

    # "gómez" < "perez"; los dos "Pérez"/"Perez" empatan y conservan el orden de entrada.
    assert [contact.id for contact in result] == ["gomez", "perez-2", "perez-1"]
    assert sort_contacts([]) == []


def test_list_contacts_service_returns_only_that_adult_sorted(state):
    _seed_contact(state, contact_id="b", first_name="Beto")
    _seed_contact(state, contact_id="a", first_name="Ana")
    _seed_contact(state, OTHER_ADULT_UID, contact_id=FOREIGN_CONTACT_ID)

    result = list_contacts(ADULT_UID, state.contacts)

    assert [contact.id for contact in result] == ["a", "b"]


# --- Servicio: permisos ---


def test_create_contact_service_caregiver_without_manage_contacts_denied(state):
    _link_caregiver(state, manage_contacts=False)
    payload = ContactCreateRequest.model_validate(_create_body())

    with pytest.raises(ContactPermissionDeniedError):
        create_contact(
            _user(Role.CUIDADOR, CAREGIVER_UID),
            ADULT_UID,
            payload,
            state.contacts,
            state.users,
        )

    assert state.contacts.list_for_adult(ADULT_UID) == []


def test_create_contact_service_caregiver_link_without_explicit_permissions_denied(
    state,
):
    # D-R2-8: un cuidador sin permisos explícitos tiene los tres en False.
    state.users.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)
    payload = ContactCreateRequest.model_validate(_create_body())

    with pytest.raises(ContactPermissionDeniedError):
        create_contact(
            _user(Role.CUIDADOR, CAREGIVER_UID),
            ADULT_UID,
            payload,
            state.contacts,
            state.users,
        )


@pytest.mark.parametrize(
    ("role", "uid"),
    [
        (Role.FAMILIAR, FAMILY_UID),
        (Role.ADMINISTRADOR, ADMIN_UID),
        (None, NO_ROLE_UID),
        (Role.ADULTO_MAYOR, OTHER_ADULT_UID),
    ],
)
def test_create_contact_service_without_access_to_adult_denied(state, role, uid):
    # Defensa en profundidad: el servicio no confía en que el router ya filtró.
    payload = ContactCreateRequest.model_validate(_create_body())

    with pytest.raises(ContactPermissionDeniedError):
        create_contact(_user(role, uid), ADULT_UID, payload, state.contacts, state.users)

    assert state.contacts.list_for_adult(ADULT_UID) == []


def test_create_contact_service_uses_record_returned_by_repository(state):
    class _RenumberingRepository(InMemoryContactRepository):
        def add(self, adult_uid: str, contact: Contact) -> Contact:
            return super().add(adult_uid, contact.model_copy(update={"id": "db-1"}))

    contacts = _RenumberingRepository()
    payload = ContactCreateRequest.model_validate(_create_body())

    result = create_contact(
        _user(Role.ADULTO_MAYOR), ADULT_UID, payload, contacts, state.users
    )

    assert result.id == "db-1"
    assert contacts.get(ADULT_UID, "db-1") == result


def test_create_contact_service_family_linked_creates_non_favorite(state):
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    payload = ContactCreateRequest.model_validate(_create_body())

    result = create_contact(
        _user(Role.FAMILIAR, FAMILY_UID), ADULT_UID, payload, state.contacts, state.users
    )

    UUID(result.id)
    assert result.is_favorite is False
    assert result.favorite_order == 0
    assert result.permissions == ContactPermissions(**MIXED_PERMISSIONS)
    assert state.contacts.get(ADULT_UID, result.id) == result


@pytest.mark.parametrize(
    ("role", "uid"),
    [
        (Role.ADMINISTRADOR, ADMIN_UID),
        (None, NO_ROLE_UID),
        (Role.FAMILIAR, FAMILY_UID),
        (Role.ADULTO_MAYOR, OTHER_ADULT_UID),
    ],
)
def test_update_and_delete_contact_service_without_access_not_found(state, role, uid):
    _seed_contact(state)
    # Vinculado a OTRO adulto: el contacto sigue siendo inaccesible.
    state.users.link_adult(uid, OTHER_ADULT_UID, Role.FAMILIAR)
    user = _user(role, uid)

    with pytest.raises(ContactNotFoundError):
        update_contact(
            user,
            CONTACT_ID,
            ContactUpdateRequest(first_name="Otra"),
            state.contacts,
            state.users,
        )
    with pytest.raises(ContactNotFoundError):
        delete_contact(user, CONTACT_ID, state.contacts, state.users)

    assert state.contacts.get(ADULT_UID, CONTACT_ID) == _contact()


def test_update_contact_service_caregiver_with_manage_contacts(state):
    _seed_contact(state)
    _link_caregiver(state, manage_contacts=True)

    result = update_contact(
        _user(Role.CUIDADOR, CAREGIVER_UID),
        CONTACT_ID,
        ContactUpdateRequest(relationship="sobrina", phone=None),
        state.contacts,
        state.users,
    )

    assert result.relationship == "sobrina"
    assert result.phone is None
    assert result.first_name == "Ana"
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == result


def test_update_contact_service_concurrent_delete_raises_not_found(state):
    class _VanishingRepository(InMemoryContactRepository):
        def update(self, adult_uid: str, contact: Contact) -> Contact | None:
            return None

    contacts = _VanishingRepository()
    contacts.add(ADULT_UID, _contact())

    with pytest.raises(ContactNotFoundError):
        update_contact(
            _user(Role.ADULTO_MAYOR),
            CONTACT_ID,
            ContactUpdateRequest(first_name="Otra"),
            contacts,
            state.users,
        )


def test_delete_contact_service_missing_raises_not_found(state):
    with pytest.raises(ContactNotFoundError):
        delete_contact(
            _user(Role.ADULTO_MAYOR), "missing", state.contacts, state.users
        )


def test_delete_contact_service_caregiver_without_permission_denied(state):
    _seed_contact(state)
    _link_caregiver(state, manage_contacts=False)

    with pytest.raises(ContactPermissionDeniedError):
        delete_contact(
            _user(Role.CUIDADOR, CAREGIVER_UID), CONTACT_ID, state.contacts, state.users
        )

    assert state.contacts.get(ADULT_UID, CONTACT_ID) is not None


# --- Rutas: C1 GET /api/contacts ---


def test_list_orders_favorites_by_order_then_name(state, client):
    _as_user(Role.ADULTO_MAYOR)
    _seed_contact(state, contact_id="plain-carlos", first_name="Carlos", favorite_order=7)
    _seed_contact(state, contact_id="fav-ana", first_name="ana", is_favorite=True, favorite_order=2)
    _seed_contact(state, contact_id="plain-beatriz", first_name="beatriz")
    _seed_contact(state, contact_id="fav-zoe", first_name="Zoe", is_favorite=True, favorite_order=1)
    _seed_contact(state, contact_id="plain-angela", first_name="Ángela")
    _seed_contact(
        state, contact_id="fav-alvaro", first_name="Álvaro", is_favorite=True, favorite_order=2
    )
    _seed_contact(state, OTHER_ADULT_UID, contact_id=FOREIGN_CONTACT_ID, first_name="Aaron")

    response = client.get(CONTACTS_PATH)

    assert response.status_code == 200
    body = response.json()
    # Favoritos por favorite_order (empate en 2 -> por nombre, sin tildes ni mayúsculas);
    # después los no favoritos por nombre aunque tengan favorite_order > 0.
    assert [contact["id"] for contact in body] == [
        "fav-zoe",
        "fav-alvaro",
        "fav-ana",
        "plain-angela",
        "plain-beatriz",
        "plain-carlos",
    ]
    assert all(set(contact) == CONTACT_KEYS for contact in body)


def test_list_adult_implicit(state, client):
    _as_user(Role.ADULTO_MAYOR)
    own = _seed_contact(state)
    _seed_contact(state, OTHER_ADULT_UID, contact_id=FOREIGN_CONTACT_ID)

    response = client.get(CONTACTS_PATH)
    response_with_own_id = client.get(CONTACTS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    assert response.json() == [own.model_dump()]
    assert response_with_own_id.status_code == 200
    assert response_with_own_id.json() == [own.model_dump()]


def test_list_empty_for_adult_without_contacts(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.get(CONTACTS_PATH)

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("adult_id", [None, "", "   "])
@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_list_family_requires_adult_id_422(state, client, role, adult_id):
    _as_user(role, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, role)
    _seed_contact(state)

    params = {} if adult_id is None else {"adult_id": adult_id}
    response = client.get(CONTACTS_PATH, params=params)

    assert response.status_code == 422
    assert response.json() == {"detail": ADULT_ID_REQUIRED_DETAIL}


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_list_family_unlinked_adult_403(state, client, role):
    _as_user(role, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, role)
    _seed_contact(state)

    response = client.get(CONTACTS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


def test_list_family_linked_adult_200(state, client):
    _as_user(Role.FAMILIAR, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    own = _seed_contact(state)
    _seed_contact(state, OTHER_ADULT_UID, contact_id=FOREIGN_CONTACT_ID)

    response = client.get(CONTACTS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    assert response.json() == [own.model_dump()]


def test_list_caregiver_reads_without_manage_contacts(state, client):
    _as_user(Role.CUIDADOR, CAREGIVER_UID)
    _link_caregiver(state, manage_contacts=False)
    own = _seed_contact(state)

    response = client.get(CONTACTS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    assert response.json() == [own.model_dump()]


def test_list_contacts_admin_403(state, client):
    _as_user(Role.ADMINISTRADOR, ADMIN_UID)
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)
    _seed_contact(state)

    response = client.get(CONTACTS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


# --- Rutas: C2 POST /api/contacts ---


def test_create_contact_adult_200_exact_body(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(
        CONTACTS_PATH,
        json=_create_body(
            first_name="  Ana ",
            relationship=" hija ",
            # Lo que no es entrada de C2 se ignora: nace sin ser favorito.
            is_favorite=True,
            favorite_order=3,
            id="client-id",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == CONTACT_KEYS
    UUID(body["id"])
    assert body["id"] != "client-id"
    assert body == {
        "id": body["id"],
        "first_name": "Ana",
        "last_name": "Pérez",
        "relationship": "hija",
        "phone": "+573001234567",
        "email": "Ana.Perez@Example.com",
        "photo_url": "https://example.com/ana.png",
        "is_favorite": False,
        "favorite_order": 0,
        "permissions": MIXED_PERMISSIONS,
    }
    stored = state.contacts.get(ADULT_UID, body["id"])
    assert stored is not None
    assert stored.model_dump() == body


def test_create_contact_family_with_adult_id(state, client):
    _as_user(Role.FAMILIAR, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)

    response = client.post(CONTACTS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 200
    body = response.json()
    assert "adult_id" not in body
    assert state.contacts.get(ADULT_UID, body["id"]).model_dump() == body
    assert state.contacts.list_for_adult(FAMILY_UID) == []


@pytest.mark.parametrize("adult_id", [None, "", "   "])
@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_create_contact_family_requires_adult_id_422(state, client, role, adult_id):
    _as_user(role, FAMILY_UID)
    state.users.link_adult(
        FAMILY_UID,
        ADULT_UID,
        role,
        CaregiverPermissions(
            manage_contacts=True, manage_reminders=True, receive_alerts=True
        )
        if role == Role.CUIDADOR
        else None,
    )

    body = _create_body() if adult_id is None else _create_body(adult_id=adult_id)
    response = client.post(CONTACTS_PATH, json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": ADULT_ID_REQUIRED_DETAIL}
    assert state.contacts.list_for_adult(ADULT_UID) == []


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_create_contact_family_unlinked_adult_403(state, client, role):
    _as_user(role, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, role)

    response = client.post(CONTACTS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.contacts.list_for_adult(ADULT_UID) == []


@pytest.mark.parametrize("explicit_permissions", [True, False])
def test_create_contact_caregiver_without_manage_contacts_403(
    state, client, explicit_permissions
):
    _as_user(Role.CUIDADOR, CAREGIVER_UID)
    if explicit_permissions:
        _link_caregiver(state, manage_contacts=False)
    else:
        state.users.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)

    response = client.post(CONTACTS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.contacts.list_for_adult(ADULT_UID) == []


def test_create_contact_caregiver_with_manage_contacts(state, client):
    _as_user(Role.CUIDADOR, CAREGIVER_UID)
    _link_caregiver(state, manage_contacts=True)

    response = client.post(CONTACTS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 200
    body = response.json()
    assert state.contacts.get(ADULT_UID, body["id"]).model_dump() == body


@pytest.mark.parametrize("photo_url", ["javascript:alert(1)", "data:text/html,x"])
def test_create_contact_photo_url_invalid_scheme_422(state, client, photo_url):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(CONTACTS_PATH, json=_create_body(photo_url=photo_url))

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: photo_url: no es válido"}
    assert state.contacts.list_for_adult(ADULT_UID) == []


def test_create_contact_invalid_email_422(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(CONTACTS_PATH, json=_create_body(email="sin-arroba"))

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: email: no es válido"}
    assert state.contacts.list_for_adult(ADULT_UID) == []


def test_create_contact_missing_permissions_422(state, client):
    _as_user(Role.ADULTO_MAYOR)
    body = _create_body()
    del body["permissions"]

    response = client.post(CONTACTS_PATH, json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: permissions: es obligatorio"}
    assert state.contacts.list_for_adult(ADULT_UID) == []


def test_create_contact_incomplete_permissions_422(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(
        CONTACTS_PATH,
        json=_create_body(permissions={"calls": True, "video_calls": True, "messages": True}),
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Datos inválidos: permissions.help_alerts: es obligatorio"
    }


@pytest.mark.parametrize("field", ["first_name", "last_name", "relationship"])
def test_create_contact_blank_required_text_422(state, client, field):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(CONTACTS_PATH, json=_create_body(**{field: "   "}))

    assert response.status_code == 422
    assert response.json() == {"detail": f"Datos inválidos: {field}: es demasiado corto"}
    assert state.contacts.list_for_adult(ADULT_UID) == []


def test_create_contact_empty_optional_fields_become_null(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(
        CONTACTS_PATH, json=_create_body(phone="", email="   ", photo_url="")
    )
    response_without_optionals = client.post(
        CONTACTS_PATH,
        json={
            "first_name": "Luis",
            "last_name": "Gómez",
            "relationship": "vecino",
            "permissions": ALL_PERMISSIONS,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["phone"] is None
    assert body["email"] is None
    assert body["photo_url"] is None
    assert state.contacts.get(ADULT_UID, body["id"]).phone is None
    assert response_without_optionals.status_code == 200
    other = response_without_optionals.json()
    assert (other["phone"], other["email"], other["photo_url"]) == (None, None, None)


def test_create_contact_admin_403(state, client):
    _as_user(Role.ADMINISTRADOR, ADMIN_UID)
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)

    response = client.post(CONTACTS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.contacts.list_for_adult(ADULT_UID) == []


def test_adult_sends_foreign_adult_id_403(state, client):
    _as_user(Role.ADULTO_MAYOR)
    _seed_contact(state, OTHER_ADULT_UID, contact_id=FOREIGN_CONTACT_ID)

    create_response = client.post(
        CONTACTS_PATH, json=_create_body(adult_id=OTHER_ADULT_UID)
    )
    list_response = client.get(CONTACTS_PATH, params={"adult_id": OTHER_ADULT_UID})

    assert create_response.status_code == 403
    assert create_response.json() == {"detail": FORBIDDEN_DETAIL}
    assert list_response.status_code == 403
    assert list_response.json() == {"detail": FORBIDDEN_DETAIL}
    assert len(state.contacts.list_for_adult(OTHER_ADULT_UID)) == 1
    assert state.contacts.list_for_adult(ADULT_UID) == []


# --- Rutas: C3 PATCH /api/contacts/{id} ---


def test_update_contact_marks_favorite(state, client):
    _as_user(Role.FAMILIAR, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    original = _seed_contact(state)

    response = client.patch(
        _contact_path(), json={"is_favorite": True, "favorite_order": 1}
    )

    assert response.status_code == 200
    expected = {**original.model_dump(), "is_favorite": True, "favorite_order": 1}
    assert response.json() == expected
    assert state.contacts.get(ADULT_UID, CONTACT_ID).model_dump() == expected


def test_update_contact_adult_marks_favorite(state, client):
    # P6: el adulto_mayor también marca favoritos sobre sus propios contactos.
    _as_user(Role.ADULTO_MAYOR)
    _seed_contact(state, contact_id="fav", first_name="Zoe")
    _seed_contact(state)

    response = client.patch(
        _contact_path("fav"), json={"is_favorite": True, "favorite_order": 0}
    )
    list_response = client.get(CONTACTS_PATH)

    assert response.status_code == 200
    assert response.json()["is_favorite"] is True
    assert response.json()["favorite_order"] == 0
    assert [contact["id"] for contact in list_response.json()] == ["fav", CONTACT_ID]


def test_update_contact_partial_keeps_other_fields(state, client):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state, is_favorite=True, favorite_order=4)

    response = client.patch(
        _contact_path(),
        json={
            "first_name": "  Ana María ",
            "permissions": MIXED_PERMISSIONS,
            # id no es campo de C3: se ignora y el contacto conserva el suyo.
            "id": "otro-id",
        },
    )

    assert response.status_code == 200
    expected = {
        **original.model_dump(),
        "first_name": "Ana María",
        "permissions": MIXED_PERMISSIONS,
    }
    assert response.json() == expected
    assert state.contacts.get(ADULT_UID, CONTACT_ID).model_dump() == expected
    assert state.contacts.find("otro-id") is None


@pytest.mark.parametrize(
    "field",
    [
        "first_name",
        "last_name",
        "relationship",
        "permissions",
        "is_favorite",
        "favorite_order",
    ],
)
def test_update_contact_null_first_name_422(state, client, field):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state)

    response = client.patch(_contact_path(), json={field: None})

    assert response.status_code == 422
    assert response.json() == {"detail": f"Datos inválidos: {field}: no es válido"}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


def test_update_contact_empty_first_name_422(state, client):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state)

    response = client.patch(_contact_path(), json={"first_name": "  "})

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: first_name: es demasiado corto"}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


def test_update_contact_negative_favorite_order_422(state, client):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state)

    response = client.patch(
        _contact_path(), json={"is_favorite": True, "favorite_order": -1}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: favorite_order: no es válido"}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


def test_update_contact_null_phone_clears_it(state, client):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state)

    response = client.patch(
        _contact_path(), json={"phone": None, "email": "", "photo_url": None}
    )

    assert response.status_code == 200
    expected = {**original.model_dump(), "phone": None, "email": None, "photo_url": None}
    assert response.json() == expected
    assert state.contacts.get(ADULT_UID, CONTACT_ID).model_dump() == expected


def test_update_contact_photo_url_invalid_scheme_422(state, client):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state)

    response = client.patch(_contact_path(), json={"photo_url": "javascript:alert(1)"})

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: photo_url: no es válido"}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


@pytest.mark.parametrize(
    ("role", "uid"),
    [(Role.FAMILIAR, FAMILY_UID), (Role.ADULTO_MAYOR, ADULT_UID)],
)
def test_update_contact_ignores_adult_id_query(state, client, role, uid):
    _as_user(role, uid)
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    _seed_contact(state)

    response = client.patch(
        _contact_path(),
        params={"adult_id": OTHER_ADULT_UID},
        json={"relationship": "nieta"},
    )

    assert response.status_code == 200
    assert response.json()["relationship"] == "nieta"
    assert state.contacts.get(ADULT_UID, CONTACT_ID).relationship == "nieta"
    assert state.contacts.list_for_adult(OTHER_ADULT_UID) == []


def test_update_contact_without_adult_id_for_family_200(state, client):
    # En rutas con {id} nunca hay 422 por ausencia de adult_id (sección 5).
    _as_user(Role.FAMILIAR, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    _seed_contact(state)

    response = client.patch(_contact_path(), json={"last_name": "Gómez"})

    assert response.status_code == 200
    assert response.json()["last_name"] == "Gómez"


def test_update_contact_ignores_adult_id_in_body(state, client):
    _as_user(Role.FAMILIAR, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, Role.FAMILIAR)
    _seed_contact(state)

    response = client.patch(
        _contact_path(), json={"adult_id": OTHER_ADULT_UID, "first_name": "Lucía"}
    )

    assert response.status_code == 200
    assert response.json()["first_name"] == "Lucía"
    assert "adult_id" not in response.json()
    assert state.contacts.get(ADULT_UID, CONTACT_ID).first_name == "Lucía"
    assert state.contacts.list_for_adult(OTHER_ADULT_UID) == []


@pytest.mark.parametrize("contact_id", [FOREIGN_CONTACT_ID, "missing-contact"])
def test_update_contact_of_other_adult_404(state, client, contact_id):
    _as_user(Role.ADULTO_MAYOR)
    _seed_contact(state)
    foreign = _seed_contact(state, OTHER_ADULT_UID, contact_id=FOREIGN_CONTACT_ID)

    response = client.patch(_contact_path(contact_id), json={"first_name": "Otra"})

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert state.contacts.get(OTHER_ADULT_UID, FOREIGN_CONTACT_ID) == foreign


def test_update_contact_unlinked_family_404(state, client):
    _as_user(Role.FAMILIAR, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, Role.FAMILIAR)
    original = _seed_contact(state)

    response = client.patch(
        _contact_path(),
        params={"adult_id": ADULT_UID},
        json={"adult_id": ADULT_UID, "first_name": "Otra"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


def test_update_contact_unlinked_caregiver_404(state, client):
    _as_user(Role.CUIDADOR, CAREGIVER_UID)
    _link_caregiver(state, manage_contacts=True, adult_uid=OTHER_ADULT_UID)
    original = _seed_contact(state)

    response = client.patch(_contact_path(), json={"first_name": "Otra"})

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


def test_update_contact_caregiver_without_permission_403(state, client):
    _as_user(Role.CUIDADOR, CAREGIVER_UID)
    _link_caregiver(state, manage_contacts=False)
    original = _seed_contact(state)

    response = client.patch(_contact_path(), json={"is_favorite": True})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


def test_update_contact_caregiver_with_manage_contacts_200(state, client):
    _as_user(Role.CUIDADOR, CAREGIVER_UID)
    _link_caregiver(state, manage_contacts=True)
    _seed_contact(state)

    response = client.patch(
        _contact_path(), json={"is_favorite": True, "favorite_order": 2}
    )

    assert response.status_code == 200
    stored = state.contacts.get(ADULT_UID, CONTACT_ID)
    assert (stored.is_favorite, stored.favorite_order) == (True, 2)


def test_update_contact_admin_403(state, client):
    _as_user(Role.ADMINISTRADOR, ADMIN_UID)
    # Vinculado a propósito: el 403 sale del rol, no de la falta de vínculo.
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)
    original = _seed_contact(state)

    response = client.patch(_contact_path(), json={"first_name": "Otra"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


# --- Rutas: C4 DELETE /api/contacts/{id} ---


@pytest.mark.parametrize(
    ("role", "uid"),
    [
        (Role.ADULTO_MAYOR, ADULT_UID),
        (Role.FAMILIAR, FAMILY_UID),
        (Role.CUIDADOR, CAREGIVER_UID),
    ],
)
def test_delete_contact_204_and_gone(state, client, role, uid):
    _as_user(role, uid)
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    _link_caregiver(state, manage_contacts=True)
    _seed_contact(state)
    kept = _seed_contact(state, contact_id="contact-2", first_name="Beto")

    response = client.delete(_contact_path())
    list_response = client.get(
        CONTACTS_PATH,
        params={} if role == Role.ADULTO_MAYOR else {"adult_id": ADULT_UID},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert state.contacts.get(ADULT_UID, CONTACT_ID) is None
    assert state.contacts.find(CONTACT_ID) is None
    assert list_response.json() == [kept.model_dump()]


@pytest.mark.parametrize("contact_id", ["missing-contact", FOREIGN_CONTACT_ID])
def test_delete_contact_not_found_404(state, client, contact_id):
    _as_user(Role.ADULTO_MAYOR)
    foreign = _seed_contact(state, OTHER_ADULT_UID, contact_id=FOREIGN_CONTACT_ID)

    response = client.delete(_contact_path(contact_id))

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert state.contacts.get(OTHER_ADULT_UID, FOREIGN_CONTACT_ID) == foreign


def test_delete_contact_twice_404(state, client):
    _as_user(Role.ADULTO_MAYOR)
    _seed_contact(state)

    first = client.delete(_contact_path())
    second = client.delete(_contact_path())

    assert first.status_code == 204
    assert second.status_code == 404
    assert second.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}


def test_delete_contact_unlinked_family_404(state, client):
    _as_user(Role.FAMILIAR, FAMILY_UID)
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, Role.FAMILIAR)
    original = _seed_contact(state)

    response = client.delete(_contact_path(), params={"adult_id": ADULT_UID})

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


@pytest.mark.parametrize("explicit_permissions", [True, False])
def test_delete_contact_caregiver_without_manage_contacts_403(
    state, client, explicit_permissions
):
    _as_user(Role.CUIDADOR, CAREGIVER_UID)
    if explicit_permissions:
        _link_caregiver(state, manage_contacts=False)
    else:
        state.users.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)
    original = _seed_contact(state)

    response = client.delete(_contact_path())

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


def test_delete_contact_without_role_403(state, client):
    _as_user(None, NO_ROLE_UID)
    state.users.link_adult(NO_ROLE_UID, ADULT_UID, Role.FAMILIAR)
    original = _seed_contact(state)

    response = client.delete(_contact_path())

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.contacts.get(ADULT_UID, CONTACT_ID) == original


# --- Rutas: autenticación y roles en C1-C4 ---

ROUTES = [
    ("GET", CONTACTS_PATH, None),
    ("POST", CONTACTS_PATH, "create"),
    ("PATCH", CONTACT_PATH_TEMPLATE.format(contact_id=CONTACT_ID), "update"),
    ("DELETE", CONTACT_PATH_TEMPLATE.format(contact_id=CONTACT_ID), None),
]


def _route_body(kind: str | None) -> dict | None:
    if kind == "create":
        return _create_body(adult_id=ADULT_UID)
    if kind == "update":
        return {"first_name": "Otra"}
    return None


@pytest.mark.parametrize(("method", "path", "body_kind"), ROUTES)
def test_routes_require_bearer_401(state, client, method, path, body_kind):
    original = _seed_contact(state)

    response = client.request(method, path, json=_route_body(body_kind))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": BEARER_DETAIL}
    assert state.contacts.list_for_adult(ADULT_UID) == [original]


@pytest.mark.parametrize("role", [Role.ADMINISTRADOR, None])
@pytest.mark.parametrize(("method", "path", "body_kind"), ROUTES)
def test_routes_admin_or_no_role_403(state, client, method, path, body_kind, role):
    _as_user(role, ADMIN_UID)
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)
    original = _seed_contact(state)

    response = client.request(
        method,
        path,
        params={"adult_id": ADULT_UID},
        json=_route_body(body_kind),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.contacts.list_for_adult(ADULT_UID) == [original]


def test_contact_routes_in_openapi():
    paths = app.openapi()["paths"]

    assert set(paths[CONTACTS_PATH]) == {"get", "post"}
    assert set(paths["/api/contacts/{contact_id}"]) == {"patch", "delete"}
    assert "204" in paths["/api/contacts/{contact_id}"]["delete"]["responses"]
    assert "200" in paths[CONTACTS_PATH]["post"]["responses"]


# --- Ronda 3 (T-contacts): favorite_order repetido y límites de columna (D-R3-7, D-R3-26d) ---

ORDER_IN_USE_DETAIL = "Datos inválidos: favorite_order: ya está en uso por otro contacto"


class _OrderConflictContactRepository(InMemoryContactRepository):
    """Repositorio falso: update se comporta como el índice único de favoritos de V003.

    V004 quitó ese índice; la traducción a 422 se conserva y se prueba con este falso.
    """

    def update(self, adult_uid: str, contact: Contact) -> Contact | None:
        raise ContactOrderConflictError("orden en uso")


def test_update_contact_favorite_order_in_use_422(state, client):
    conflicting = _OrderConflictContactRepository()
    app.dependency_overrides[get_contact_repository] = lambda: conflicting
    _as_user(Role.ADULTO_MAYOR)
    original = conflicting.add(ADULT_UID, _contact())

    response = client.patch(
        _contact_path(), json={"is_favorite": True, "favorite_order": 1}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": ORDER_IN_USE_DETAIL}
    assert conflicting.get(ADULT_UID, CONTACT_ID) == original


def test_create_contact_relationship_of_100_chars_200(state, client):
    # relacion es varchar(100) en V004 (D-R3-26d): 100 caracteres se aceptan.
    _as_user(Role.ADULTO_MAYOR)
    relationship = "r" * 100

    response = client.post(CONTACTS_PATH, json=_create_body(relationship=relationship))

    assert response.status_code == 200
    body = response.json()
    assert body["relationship"] == relationship
    stored = state.contacts.get(ADULT_UID, body["id"])
    assert stored is not None and stored.relationship == relationship


def test_update_contact_relationship_of_100_chars_200(state, client):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state)
    relationship = "r" * 100

    response = client.patch(_contact_path(), json={"relationship": relationship})

    assert response.status_code == 200
    assert response.json() == {**original.model_dump(), "relationship": relationship}


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_contact_relationship_longer_than_100_chars_422(state, client, method):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state)
    relationship = "r" * 101

    if method == "POST":
        response = client.post(CONTACTS_PATH, json=_create_body(relationship=relationship))
    else:
        response = client.patch(_contact_path(), json={"relationship": relationship})

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: relationship: es demasiado largo"}
    assert state.contacts.list_for_adult(ADULT_UID) == [original]


def test_update_contact_favorite_order_above_int4_422(state, client):
    _as_user(Role.ADULTO_MAYOR)
    original = _seed_contact(state)

    response = client.patch(
        _contact_path(), json={"is_favorite": True, "favorite_order": 2**31}
    )
    accepted = client.patch(
        _contact_path(), json={"is_favorite": True, "favorite_order": 2**31 - 1}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: favorite_order: no es válido"}
    assert accepted.status_code == 200
    assert accepted.json() == {
        **original.model_dump(),
        "is_favorite": True,
        "favorite_order": 2**31 - 1,
    }


# SqlContactRepository sin red: una conexión falsa lanza la IntegrityError del driver.


class _FakeDiag:
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class _FakeDriverError(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__("error del driver")
        self.diag = _FakeDiag(constraint_name)


class _FailingConnection:
    def __init__(self, constraint_name: str) -> None:
        self._constraint_name = constraint_name

    def execute(self, *args, **kwargs):
        raise IntegrityError("SQL", {}, _FakeDriverError(self._constraint_name))


class _FailingDatabase:
    def __init__(self, constraint_name: str) -> None:
        self._constraint_name = constraint_name

    @contextmanager
    def transaction(self):
        yield _FailingConnection(self._constraint_name)


@pytest.mark.parametrize("operation", ["add", "update"])
def test_sql_contact_repository_translates_favorite_order_conflict(operation):
    repo = SqlContactRepository(_FailingDatabase(FAVORITE_ORDER_CONSTRAINT))
    contact = _contact("12", is_favorite=True, favorite_order=1)

    with pytest.raises(ContactOrderConflictError) as exc_info:
        getattr(repo, operation)(ADULT_UID, contact)

    # La causa SQL no se encadena: su texto trae la sentencia y el DETAIL de Postgres.
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


@pytest.mark.parametrize("operation", ["add", "update"])
def test_sql_contact_repository_reraises_other_integrity_errors(operation):
    repo = SqlContactRepository(_FailingDatabase("ck_adulto_contacto_nombre"))
    contact = _contact("12")

    with pytest.raises(IntegrityError):
        getattr(repo, operation)(ADULT_UID, contact)


def test_update_contact_service_translates_order_conflict(state):
    conflicting = _OrderConflictContactRepository()
    original = conflicting.add(ADULT_UID, _contact())
    payload = ContactUpdateRequest(is_favorite=True, favorite_order=1)

    with pytest.raises(ContactOrderInUseError):
        update_contact(
            _user(Role.ADULTO_MAYOR), CONTACT_ID, payload, conflicting, state.users
        )

    assert conflicting.get(ADULT_UID, CONTACT_ID) == original
