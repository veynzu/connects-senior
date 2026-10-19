import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.security import FORBIDDEN_DETAIL, get_current_user
from app.integrations import fcm_client
from app.integrations.fcm_client import FcmResult, FcmUnavailableError
from app.main import app
from app.models.auth import AuthenticatedUser
from app.models.contacts import Contact, ContactPermissions
from app.models.enums import Role
from app.models.messages import MessageCreateRequest, MessageRecord
from app.models.users import UserProfile
from app.repositories.contact_repository import (
    InMemoryContactRepository,
    get_contact_repository,
)
from app.repositories.device_repository import (
    InMemoryDeviceRepository,
    get_device_repository,
)
from app.repositories.errors import RecipientNotLinkedError
from app.repositories.message_repository import (
    InMemoryMessageRepository,
    get_message_repository,
)
from app.repositories.user_repository import (
    InMemoryUserRepository,
    get_user_repository,
)
from app.services.message_service import (
    MESSAGE_PUSH_TITLE,
    MessageContactNotFoundError,
    MessagePermissionDeniedError,
    list_conversation,
    send_message,
)


MESSAGES_PATH = "/api/messages"
BEARER_DETAIL = "Debes enviar un token Bearer."
CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."
PUSH_LOGGER = "app.services.push_service"

ADULT_UID = "adult-uid"
ADULT_EMAIL = "rosa@example.com"
ADULT_PROFILE_NAME = "Rosa Gómez"
ADULT_TOKEN_NAME = "Rosa G."
OTHER_ADULT_UID = "other-adult-uid"
FAMILY_UID = "family-uid"
FAMILY_EMAIL = "ana.perez@example.com"
FAMILY_DISPLAY_NAME = "Ana Pérez Ruiz"
CAREGIVER_UID = "caregiver-uid"
CAREGIVER_EMAIL = "carlos@example.com"
CAREGIVER_DISPLAY_NAME = "Carlos Díaz"
STRANGER_UID = "stranger-uid"

CONTACT_ID = "contact-1"
CAREGIVER_CONTACT_ID = "contact-2"
MUTED_CONTACT_ID = "contact-muted"
FOREIGN_CONTACT_ID = "contact-of-other-adult"
MISSING_CONTACT_ID = "contact-that-does-not-exist"

ADULT_FCM_TOKEN = "fcm-token-adult"
FAMILY_FCM_TOKEN = "fcm-token-family"
STRANGER_FCM_TOKEN = "fcm-token-stranger"

MESSAGE_KEYS = {"id", "from_me", "text", "created_at"}
T0 = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)


class FakeSendToTokens:
    """Sustituto de fcm_client.send_to_tokens: registra cada llamada, sin red."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, tokens, title, body, data):
        self.calls.append(
            {"tokens": list(tokens), "title": title, "body": body, "data": data}
        )
        if self.error is not None:
            raise self.error
        return FcmResult(success_count=len(tokens), failure_count=0, invalid_tokens=[])


def _contact(
    contact_id: str,
    *,
    first_name: str = "Ana",
    last_name: str = "Pérez",
    email: str | None = None,
    messages: bool = True,
) -> Contact:
    return Contact(
        id=contact_id,
        first_name=first_name,
        last_name=last_name,
        relationship="hija",
        phone="+573001234567",
        email=email,
        permissions=ContactPermissions(
            calls=True,
            video_calls=True,
            messages=messages,
            help_alerts=True,
        ),
    )


@pytest.fixture(autouse=True)
def state(monkeypatch):
    shared = SimpleNamespace(
        contacts=InMemoryContactRepository(),
        users=InMemoryUserRepository(),
        devices=InMemoryDeviceRepository(),
        messages=InMemoryMessageRepository(),
        push=FakeSendToTokens(),
    )
    # Ninguna push real: push_service llama a fcm_client.send_to_tokens por el módulo.
    monkeypatch.setattr(fcm_client, "send_to_tokens", shared.push)

    shared.users.add_profile(
        UserProfile(
            uid=ADULT_UID,
            first_name="Rosa",
            last_name="Gómez",
            email=ADULT_EMAIL,
            role=Role.ADULTO_MAYOR,
        )
    )
    shared.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    # Cuidador sin permisos explícitos: los mensajes no dependen de ellos.
    shared.users.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)
    # El email del contacto llega con mayúsculas y espacios: se compara normalizado.
    shared.contacts.add(ADULT_UID, _contact(CONTACT_ID, email=" Ana.Perez@Example.com "))
    shared.contacts.add(
        ADULT_UID,
        _contact(
            CAREGIVER_CONTACT_ID,
            first_name="Carlos",
            last_name="Díaz",
            email=CAREGIVER_EMAIL,
        ),
    )
    shared.contacts.add(
        ADULT_UID, _contact(MUTED_CONTACT_ID, email=FAMILY_EMAIL, messages=False)
    )
    # Contacto de otro adulto con el mismo email del familiar: el familiar no está vinculado.
    shared.contacts.add(OTHER_ADULT_UID, _contact(FOREIGN_CONTACT_ID, email=FAMILY_EMAIL))
    shared.devices.register(ADULT_UID, ADULT_FCM_TOKEN)
    shared.devices.register(FAMILY_UID, FAMILY_FCM_TOKEN)

    app.dependency_overrides[get_contact_repository] = lambda: shared.contacts
    app.dependency_overrides[get_user_repository] = lambda: shared.users
    app.dependency_overrides[get_device_repository] = lambda: shared.devices
    app.dependency_overrides[get_message_repository] = lambda: shared.messages
    yield shared
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _adult(display_name: str | None = ADULT_TOKEN_NAME) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=ADULT_UID,
        email=ADULT_EMAIL,
        email_verified=True,
        display_name=display_name,
        role=Role.ADULTO_MAYOR,
    )


def _family(
    *,
    email: str | None = "Ana.Perez@example.com",
    email_verified: bool | None = True,
    display_name: str | None = FAMILY_DISPLAY_NAME,
    uid: str = FAMILY_UID,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=email,
        email_verified=email_verified,
        display_name=display_name,
        role=Role.FAMILIAR,
    )


def _caregiver() -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=CAREGIVER_UID,
        email=CAREGIVER_EMAIL,
        email_verified=True,
        display_name=CAREGIVER_DISPLAY_NAME,
        role=Role.CUIDADOR,
    )


def _as(user: AuthenticatedUser) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _make_family_reachable(state) -> None:
    """El familiar marcó su correo verificado en A10 (D-R2-3); su dispositivo ya está en el fixture."""
    state.users.mark_email_verified(FAMILY_UID, FAMILY_EMAIL)


def _record(
    message_id: str,
    created_at: datetime,
    *,
    from_adult: bool,
    adult_uid: str = ADULT_UID,
    contact_id: str = CONTACT_ID,
) -> MessageRecord:
    return MessageRecord(
        id=message_id,
        adult_uid=adult_uid,
        contact_id=contact_id,
        text=f"texto {message_id}",
        created_at=created_at,
        from_adult=from_adult,
    )


def _seed_conversation(state) -> None:
    # Insertados fuera de orden, con un empate en created_at (m-2a antes que m-2b).
    state.messages.add(_record("m-3", T0 + timedelta(minutes=2), from_adult=False))
    state.messages.add(_record("m-1", T0, from_adult=True))
    state.messages.add(_record("m-2a", T0 + timedelta(minutes=1), from_adult=False))
    state.messages.add(_record("m-2b", T0 + timedelta(minutes=1), from_adult=True))
    # Mensajes de otras conversaciones: nunca aparecen.
    state.messages.add(
        _record(
            "other-contact",
            T0 + timedelta(seconds=30),
            from_adult=True,
            contact_id=CAREGIVER_CONTACT_ID,
        )
    )
    state.messages.add(
        _record(
            "other-adult",
            T0 + timedelta(seconds=45),
            from_adult=True,
            adult_uid=OTHER_ADULT_UID,
            contact_id=FOREIGN_CONTACT_ID,
        )
    )


def _all_stored(state) -> list[MessageRecord]:
    return [
        record
        for adult_uid, contact_id in (
            (ADULT_UID, CONTACT_ID),
            (ADULT_UID, CAREGIVER_CONTACT_ID),
            (ADULT_UID, MUTED_CONTACT_ID),
            (OTHER_ADULT_UID, FOREIGN_CONTACT_ID),
        )
        for record in state.messages.list_conversation(adult_uid, contact_id)
    ]


def _expected_data(record: MessageRecord, from_name: str) -> dict[str, str]:
    return {
        "type": "message",
        "message_id": record.id,
        "contact_id": record.contact_id,
        "adult_id": record.adult_uid,
        "from_name": from_name,
    }


# --- M1: POST /api/messages (adulto mayor) ---


def test_m1_adult_201_and_pushes_linked_contact(state, client):
    _make_family_reachable(state)
    _as(_adult())

    response = client.post(
        MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "  Hola, hija  "}
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "created_at"}
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    stored = state.messages.list_conversation(ADULT_UID, CONTACT_ID)
    assert len(stored) == 1
    record = stored[0]
    assert record.id == body["id"]
    assert record.text == "Hola, hija"
    assert record.from_adult is True
    assert record.adult_uid == ADULT_UID
    assert record.contact_id == CONTACT_ID
    assert record.created_at.microsecond == 0
    assert state.push.calls == [
        {
            "tokens": [FAMILY_FCM_TOKEN],
            "title": MESSAGE_PUSH_TITLE,
            "body": "Hola, hija",
            "data": _expected_data(record, ADULT_PROFILE_NAME),
        }
    ]


def test_m1_adult_no_push_when_contact_not_verified(state, client):
    # El familiar tiene dispositivo pero nunca marcó su correo como verificado (A10).
    _as(_adult())

    response = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola"})

    assert response.status_code == 201
    assert len(state.messages.list_conversation(ADULT_UID, CONTACT_ID)) == 1
    assert state.push.calls == []


def test_m1_adult_no_push_when_contact_uid_not_linked_to_adult(state, client):
    # El correo del contacto está verificado por un uid que no puede leer la conversación.
    state.users.mark_email_verified(STRANGER_UID, FAMILY_EMAIL)
    state.devices.register(STRANGER_UID, STRANGER_FCM_TOKEN)
    _as(_adult())

    response = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola"})

    assert response.status_code == 201
    assert len(state.messages.list_conversation(ADULT_UID, CONTACT_ID)) == 1
    assert state.push.calls == []


@pytest.mark.parametrize(
    "error",
    [FcmUnavailableError("El servicio de notificaciones no está disponible."),
     RuntimeError("fallo inesperado")],
    ids=["fcm_unavailable", "unexpected"],
)
def test_m1_push_failure_still_201(state, client, caplog, error):
    _make_family_reachable(state)
    state.push.error = error
    _as(_adult())
    secret_text = "Texto privado del mensaje"

    with caplog.at_level(logging.ERROR, logger=PUSH_LOGGER):
        response = client.post(
            MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": secret_text}
        )

    assert response.status_code == 201
    stored = state.messages.list_conversation(ADULT_UID, CONTACT_ID)
    assert [record.id for record in stored] == [response.json()["id"]]
    assert len(state.push.calls) == 1
    assert any(record.name == PUSH_LOGGER for record in caplog.records)
    assert secret_text not in caplog.text
    assert FAMILY_FCM_TOKEN not in caplog.text


@pytest.mark.parametrize("contact_id", [FOREIGN_CONTACT_ID, MISSING_CONTACT_ID])
def test_m1_adult_foreign_contact_404(state, client, contact_id):
    _as(_adult())

    response = client.post(MESSAGES_PATH, json={"contact_id": contact_id, "text": "Hola"})

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert _all_stored(state) == []
    assert state.push.calls == []


@pytest.mark.parametrize("user_factory", [_adult, _family], ids=["adult", "family"])
def test_m1_permissions_messages_false_403(state, client, user_factory):
    _make_family_reachable(state)
    _as(user_factory())

    response = client.post(
        MESSAGES_PATH, json={"contact_id": MUTED_CONTACT_ID, "text": "Hola"}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _all_stored(state) == []
    assert state.push.calls == []


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_m1_blank_text_422(state, client, text):
    _as(_adult())

    response = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": text})

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: text: es demasiado corto"}
    assert _all_stored(state) == []
    assert state.push.calls == []


def test_m1_text_too_long_422(state, client):
    _as(_adult())

    response = client.post(
        MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "a" * 2001}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: text: es demasiado largo"}
    assert _all_stored(state) == []


def test_m1_text_max_length_201_and_push_body_truncated_to_120(state, client):
    _make_family_reachable(state)
    _as(_adult())
    text = "b" * 2000

    response = client.post(
        MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": f"  {text}  "}
    )

    assert response.status_code == 201
    assert state.messages.list_conversation(ADULT_UID, CONTACT_ID)[0].text == text
    assert state.push.calls[0]["body"] == "b" * 120


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"contact_id": CONTACT_ID}, "Datos inválidos: text: es obligatorio"),
        ({"text": "Hola"}, "Datos inválidos: contact_id: es obligatorio"),
        ({"contact_id": "  ", "text": "Hola"}, "Datos inválidos: contact_id: es demasiado corto"),
    ],
    ids=["missing_text", "missing_contact_id", "blank_contact_id"],
)
def test_m1_missing_fields_422(state, client, payload, detail):
    _as(_adult())

    response = client.post(MESSAGES_PATH, json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert _all_stored(state) == []


def test_m1_ignores_extra_fields_like_adult_id(state, client):
    _as(_adult())

    response = client.post(
        MESSAGES_PATH,
        json={
            "contact_id": CONTACT_ID,
            "text": "Hola",
            "adult_id": OTHER_ADULT_UID,
            "from_adult": False,
        },
    )

    assert response.status_code == 201
    stored = state.messages.list_conversation(ADULT_UID, CONTACT_ID)
    assert len(stored) == 1
    assert stored[0].from_adult is True
    assert state.messages.list_conversation(OTHER_ADULT_UID, CONTACT_ID) == []


# --- M1: POST /api/messages (familiar y cuidador, P19) ---


def test_m1_family_linked_contact_201_saved_as_contact_to_adult_and_pushes_adult(
    state, client
):
    # No hace falta A10 para escribir: basta el email verificado del token (D-R2-3).
    _as(_family())

    response = client.post(
        MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola, mamá"}
    )

    assert response.status_code == 201
    stored = state.messages.list_conversation(ADULT_UID, CONTACT_ID)
    assert len(stored) == 1
    record = stored[0]
    assert record.id == response.json()["id"]
    assert record.from_adult is False
    assert record.adult_uid == ADULT_UID
    assert record.contact_id == CONTACT_ID
    assert record.text == "Hola, mamá"
    assert state.push.calls == [
        {
            "tokens": [ADULT_FCM_TOKEN],
            "title": MESSAGE_PUSH_TITLE,
            "body": "Hola, mamá",
            "data": _expected_data(record, FAMILY_DISPLAY_NAME),
        }
    ]


def test_m1_caregiver_without_permissions_linked_contact_201(state, client):
    _as(_caregiver())

    response = client.post(
        MESSAGES_PATH, json={"contact_id": CAREGIVER_CONTACT_ID, "text": "Ya voy"}
    )

    assert response.status_code == 201
    record = state.messages.list_conversation(ADULT_UID, CAREGIVER_CONTACT_ID)[0]
    assert record.from_adult is False
    assert state.push.calls[0]["tokens"] == [ADULT_FCM_TOKEN]
    assert state.push.calls[0]["data"] == _expected_data(record, CAREGIVER_DISPLAY_NAME)


def test_m1_family_no_push_when_adult_has_no_device(state, client):
    state.devices.remove_tokens([ADULT_FCM_TOKEN])
    _as(_family())

    response = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola"})

    assert response.status_code == 201
    assert len(state.messages.list_conversation(ADULT_UID, CONTACT_ID)) == 1
    assert state.push.calls == []


@pytest.mark.parametrize("email_verified", [False, None])
def test_m1_family_email_unverified_403(state, client, email_verified):
    _as(_family(email_verified=email_verified))

    response = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _all_stored(state) == []
    assert state.push.calls == []


@pytest.mark.parametrize("email", ["otra.persona@example.com", None])
def test_m1_family_email_mismatch_403(state, client, email):
    _as(_family(email=email))

    response = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _all_stored(state) == []
    assert state.push.calls == []


@pytest.mark.parametrize("contact_id", [FOREIGN_CONTACT_ID, MISSING_CONTACT_ID])
def test_m1_family_unlinked_adult_contact_404(state, client, contact_id):
    _as(_family())

    response = client.post(MESSAGES_PATH, json={"contact_id": contact_id, "text": "Hola"})

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert _all_stored(state) == []
    assert state.push.calls == []


# --- M2: GET /api/messages?contact_id= ---


def test_m2_adult_from_me_and_ascending_order(state, client):
    _seed_conversation(state)
    _as(_adult())

    response = client.get(MESSAGES_PATH, params={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    body = response.json()
    assert all(set(item) == MESSAGE_KEYS for item in body)
    assert [(item["id"], item["from_me"]) for item in body] == [
        ("m-1", True),
        ("m-2a", False),
        ("m-2b", True),
        ("m-3", False),
    ]
    assert body[0]["text"] == "texto m-1"
    assert datetime.fromisoformat(body[0]["created_at"]) == T0
    assert datetime.fromisoformat(body[3]["created_at"]) == T0 + timedelta(minutes=2)


def test_m2_family_linked_sees_inverted_from_me(state, client):
    _seed_conversation(state)
    _as(_family())

    response = client.get(MESSAGES_PATH, params={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    assert [(item["id"], item["from_me"]) for item in response.json()] == [
        ("m-1", False),
        ("m-2a", True),
        ("m-2b", False),
        ("m-3", True),
    ]


def test_m2_conversation_round_trip_from_both_sides(state, client):
    _as(_adult())
    first = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola"})
    _as(_family())
    second = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola, mamá"})
    assert first.status_code == second.status_code == 201

    family_view = client.get(MESSAGES_PATH, params={"contact_id": CONTACT_ID}).json()
    _as(_adult())
    adult_view = client.get(MESSAGES_PATH, params={"contact_id": CONTACT_ID}).json()

    assert [(item["id"], item["from_me"]) for item in adult_view] == [
        (first.json()["id"], True),
        (second.json()["id"], False),
    ]
    assert [(item["id"], item["from_me"]) for item in family_view] == [
        (first.json()["id"], False),
        (second.json()["id"], True),
    ]


@pytest.mark.parametrize(
    ("email", "email_verified"),
    [
        ("otra.persona@example.com", True),
        ("Ana.Perez@example.com", False),
        ("Ana.Perez@example.com", None),
        (None, True),
    ],
    ids=["email_mismatch", "unverified", "verified_none", "no_email"],
)
def test_m2_family_contact_email_mismatch_or_unverified_returns_empty(
    state, client, email, email_verified
):
    _seed_conversation(state)
    _as(_family(email=email, email_verified=email_verified))

    response = client.get(MESSAGES_PATH, params={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("contact_id", [FOREIGN_CONTACT_ID, MISSING_CONTACT_ID])
def test_m2_family_unlinked_adult_contact_404(state, client, contact_id):
    _seed_conversation(state)
    _as(_family())

    response = client.get(MESSAGES_PATH, params={"contact_id": contact_id})

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}


@pytest.mark.parametrize("contact_id", [FOREIGN_CONTACT_ID, MISSING_CONTACT_ID])
def test_m2_adult_foreign_contact_404(state, client, contact_id):
    _seed_conversation(state)
    _as(_adult())

    response = client.get(MESSAGES_PATH, params={"contact_id": contact_id})

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}


def test_m2_permissions_messages_false_still_lists(state, client):
    # El contrato solo condiciona M1 a permissions.messages; la lectura no se corta.
    state.messages.add(
        _record("muted-1", T0, from_adult=True, contact_id=MUTED_CONTACT_ID)
    )
    _as(_adult())

    response = client.get(MESSAGES_PATH, params={"contact_id": MUTED_CONTACT_ID})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["muted-1"]


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({}, "Datos inválidos: contact_id: es obligatorio"),
        ({"contact_id": ""}, "Datos inválidos: contact_id: es demasiado corto"),
    ],
    ids=["missing", "empty"],
)
def test_m2_missing_contact_id_422(state, client, params, detail):
    _as(_adult())

    response = client.get(MESSAGES_PATH, params=params)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}


# --- Roles y autenticación ---

ROUTES = [
    ("post", {"json": {"contact_id": CONTACT_ID, "text": "Hola"}}),
    ("get", {"params": {"contact_id": CONTACT_ID}}),
]
ROUTE_IDS = ["M1", "M2"]


@pytest.mark.parametrize(("method", "kwargs"), ROUTES, ids=ROUTE_IDS)
def test_messages_admin_403(state, client, method, kwargs):
    _as(
        AuthenticatedUser(
            uid="admin-uid",
            email=FAMILY_EMAIL,
            email_verified=True,
            role=Role.ADMINISTRADOR,
        )
    )

    response = getattr(client, method)(MESSAGES_PATH, **kwargs)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _all_stored(state) == []
    assert state.push.calls == []


@pytest.mark.parametrize(("method", "kwargs"), ROUTES, ids=ROUTE_IDS)
def test_messages_role_none_403(state, client, method, kwargs):
    _as(
        AuthenticatedUser(
            uid=FAMILY_UID,
            email=FAMILY_EMAIL,
            email_verified=True,
            role=None,
        )
    )

    response = getattr(client, method)(MESSAGES_PATH, **kwargs)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _all_stored(state) == []
    assert state.push.calls == []


@pytest.mark.parametrize(("method", "kwargs"), ROUTES, ids=ROUTE_IDS)
def test_routes_require_bearer_401(state, client, method, kwargs):
    response = getattr(client, method)(MESSAGES_PATH, **kwargs)

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert _all_stored(state) == []


def test_openapi_lists_messages_routes():
    paths = app.openapi()["paths"]

    assert set(paths["/api/messages"]) == {"get", "post"}
    assert "201" in paths["/api/messages"]["post"]["responses"]


# --- Servicio ---


def test_send_message_uses_injected_now_without_microseconds(state):
    now = datetime(2026, 10, 17, 8, 30, 15, 987654, tzinfo=timezone(timedelta(hours=-5)))

    response = send_message(
        _adult(),
        MessageCreateRequest(contact_id=CONTACT_ID, text="Hola"),
        state.contacts,
        state.messages,
        state.users,
        state.devices,
        now=now,
    )

    assert response.created_at == now.replace(microsecond=0)
    assert state.messages.list_conversation(ADULT_UID, CONTACT_ID)[0].created_at == (
        now.replace(microsecond=0)
    )


class _RenumberingMessageRepository(InMemoryMessageRepository):
    """Simula la implementación SQL: el id lo asigna el repositorio al guardar (D-R2-17)."""

    def add(self, record: MessageRecord) -> MessageRecord:
        return super().add(replace(record, id="42"))


def test_send_message_uses_record_returned_by_repository(state):
    _make_family_reachable(state)
    messages = _RenumberingMessageRepository()

    response = send_message(
        _adult(),
        MessageCreateRequest(contact_id=CONTACT_ID, text="Hola"),
        state.contacts,
        messages,
        state.users,
        state.devices,
    )

    assert response.id == "42"
    assert state.push.calls[0]["data"]["message_id"] == "42"


@pytest.mark.parametrize("role", [Role.ADMINISTRADOR, None])
def test_service_rejects_admin_and_role_none(state, role):
    user = AuthenticatedUser(uid="someone", email=FAMILY_EMAIL, email_verified=True, role=role)

    with pytest.raises(MessagePermissionDeniedError):
        send_message(
            user,
            MessageCreateRequest(contact_id=CONTACT_ID, text="Hola"),
            state.contacts,
            state.messages,
            state.users,
            state.devices,
        )
    with pytest.raises(MessagePermissionDeniedError):
        list_conversation(user, CONTACT_ID, state.contacts, state.messages, state.users)
    assert _all_stored(state) == []


def test_service_contact_not_found_and_not_linked(state):
    with pytest.raises(MessageContactNotFoundError):
        list_conversation(
            _family(), FOREIGN_CONTACT_ID, state.contacts, state.messages, state.users
        )
    with pytest.raises(MessagePermissionDeniedError):
        send_message(
            _family(email_verified=False),
            MessageCreateRequest(contact_id=CONTACT_ID, text="Hola"),
            state.contacts,
            state.messages,
            state.users,
            state.devices,
        )
    assert (
        list_conversation(
            _family(email_verified=False),
            CONTACT_ID,
            state.contacts,
            state.messages,
            state.users,
        )
        == []
    )


def test_push_from_name_adult_without_profile_uses_display_name_then_uid(state):
    # Repositorio de usuarios sin perfil del adulto, con el familiar alcanzable.
    users = InMemoryUserRepository()
    users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    users.mark_email_verified(FAMILY_UID, FAMILY_EMAIL)

    for adult, expected in ((_adult(), ADULT_TOKEN_NAME), (_adult(display_name=None), ADULT_UID)):
        send_message(
            adult,
            MessageCreateRequest(contact_id=CONTACT_ID, text="Hola"),
            state.contacts,
            state.messages,
            users,
            state.devices,
        )
        assert state.push.calls[-1]["data"]["from_name"] == expected


def test_push_from_name_contact_without_display_name_uses_contact_name(state):
    send_message(
        _family(display_name=None),
        MessageCreateRequest(contact_id=CONTACT_ID, text="Hola"),
        state.contacts,
        state.messages,
        state.users,
        state.devices,
    )

    assert state.push.calls[0]["tokens"] == [ADULT_FCM_TOKEN]
    assert state.push.calls[0]["data"]["from_name"] == "Ana Pérez"


# --- Repositorio ---


def test_message_repository_stable_ascending_order_and_copies():
    repository = InMemoryMessageRepository()
    repository.add(_record("late", T0 + timedelta(minutes=5), from_adult=True))
    first_tie = repository.add(_record("tie-1", T0, from_adult=False))
    repository.add(_record("tie-2", T0, from_adult=True))
    repository.add(_record("elsewhere", T0, from_adult=True, contact_id=CAREGIVER_CONTACT_ID))

    first_tie.text = "modificado fuera del repositorio"
    listed = repository.list_conversation(ADULT_UID, CONTACT_ID)
    listed[0].text = "modificado tras listar"

    assert [record.id for record in repository.list_conversation(ADULT_UID, CONTACT_ID)] == [
        "tie-1",
        "tie-2",
        "late",
    ]
    assert repository.list_conversation(ADULT_UID, CONTACT_ID)[0].text == "texto tie-1"
    assert repository.list_conversation(OTHER_ADULT_UID, CONTACT_ID) == []


def test_get_message_repository_returns_shared_default():
    assert get_message_repository() is get_message_repository()
    assert isinstance(get_message_repository(), InMemoryMessageRepository)


# --- Base PostgreSQL: contacto sin cuenta y push protegida (ronda 3, D-R3-12, D-R3-23) ---

RECIPIENT_UNAVAILABLE_DETAIL = "El contacto no tiene una cuenta en Connects Senior."
MESSAGE_SERVICE_LOGGER = "app.services.message_service"
NO_ACCOUNT_CONTACT_ID = "contact-without-account"


class _RecipientNotLinkedMessageRepository(InMemoryMessageRepository):
    """Simula la implementación SQL con un contacto sin cuenta: add no puede guardar."""

    def __init__(self) -> None:
        super().__init__()
        self.add_calls = 0

    def add(self, record: MessageRecord) -> MessageRecord:
        self.add_calls += 1
        raise RecipientNotLinkedError("sin usuario del contacto")


class _FailingUserRepository:
    """Envuelve el repositorio de usuarios; un método falla como con la base caída."""

    def __init__(self, inner: InMemoryUserRepository, failing_method: str) -> None:
        self._inner = inner
        self._failing_method = failing_method
        self.failures = 0

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if name != self._failing_method:
            return attribute

        def fail(*args, **kwargs):
            self.failures += 1
            raise OperationalError("x", {}, Exception("x"))

        return fail


@pytest.mark.parametrize("user_factory", [_adult, _family], ids=["adult", "family"])
def test_m1_recipient_without_account_422_exact_detail(state, client, user_factory):
    _make_family_reachable(state)
    failing = _RecipientNotLinkedMessageRepository()
    app.dependency_overrides[get_message_repository] = lambda: failing
    _as(user_factory())

    response = client.post(MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": "Hola"})

    assert response.status_code == 422
    assert response.json() == {"detail": RECIPIENT_UNAVAILABLE_DETAIL}
    assert failing.add_calls == 1
    assert failing.list_conversation(ADULT_UID, CONTACT_ID) == []
    assert state.push.calls == []


def test_m1_in_memory_still_saves_message_to_contact_without_account(state, client):
    # Con InMemory (sqlite en DATABASE_URL) no hay usuario que exigir: se guarda como antes.
    state.contacts.add(ADULT_UID, _contact(NO_ACCOUNT_CONTACT_ID, email=None))
    _as(_adult())

    response = client.post(
        MESSAGES_PATH, json={"contact_id": NO_ACCOUNT_CONTACT_ID, "text": "Hola"}
    )

    assert response.status_code == 201
    stored = state.messages.list_conversation(ADULT_UID, NO_ACCOUNT_CONTACT_ID)
    assert [(record.id, record.text, record.from_adult) for record in stored] == [
        (response.json()["id"], "Hola", True)
    ]
    assert state.push.calls == []
    listed = client.get(MESSAGES_PATH, params={"contact_id": NO_ACCOUNT_CONTACT_ID})
    assert [(item["id"], item["from_me"]) for item in listed.json()] == [
        (response.json()["id"], True)
    ]


@pytest.mark.parametrize(
    "failing_method", ["find_uid_by_verified_email", "is_linked", "get_profile"]
)
def test_m1_recipient_resolution_failure_after_saving_still_201(
    state, client, caplog, failing_method
):
    # D-R3-23: con la base caída después de guardar, M1 no puede responder 503.
    _make_family_reachable(state)
    users = _FailingUserRepository(state.users, failing_method)
    app.dependency_overrides[get_user_repository] = lambda: users
    _as(_adult())
    secret_text = "Texto privado del mensaje"

    with caplog.at_level(logging.ERROR, logger=MESSAGE_SERVICE_LOGGER):
        response = client.post(
            MESSAGES_PATH, json={"contact_id": CONTACT_ID, "text": secret_text}
        )

    assert response.status_code == 201
    stored = state.messages.list_conversation(ADULT_UID, CONTACT_ID)
    assert [(record.id, record.text) for record in stored] == [
        (response.json()["id"], secret_text)
    ]
    assert users.failures == 1
    assert state.push.calls == []
    assert any(record.name == MESSAGE_SERVICE_LOGGER for record in caplog.records)
    assert "OperationalError" in caplog.text
    assert secret_text not in caplog.text
    assert FAMILY_EMAIL not in caplog.text
