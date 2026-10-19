import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.calls_router import CALLS_UNAVAILABLE_DETAIL
from app.core.security import FORBIDDEN_DETAIL, get_current_user
from app.integrations import fcm_client
from app.integrations.daily_client import (
    DailyClient,
    DailyRoom,
    DailyUnavailableError,
    get_daily_client,
)
from app.integrations.fcm_client import FcmResult, FcmUnavailableError
from app.main import app
from app.models.auth import AuthenticatedUser
from app.models.calls import CallCreateRequest, CallRecord, CallResponse
from app.models.contacts import Contact, ContactPermissions
from app.models.enums import CallStatus, Role
from app.repositories.call_repository import (
    InMemoryCallRepository,
    get_call_repository,
)
from app.repositories.contact_repository import (
    InMemoryContactRepository,
    get_contact_repository,
)
from app.repositories.device_repository import (
    InMemoryDeviceRepository,
    get_device_repository,
)
from app.repositories.user_repository import (
    InMemoryUserRepository,
    get_user_repository,
)
from app.services import push_service
from app.services.call_service import (
    CallNotFoundError,
    CallPermissionDeniedError,
    ContactNotFoundError,
    list_calls,
    start_call,
    update_call_status,
)


CALLS_PATH = "/api/calls"
CALL_PATH_TEMPLATE = "/api/calls/{call_id}"
ADULT_UID = "adult-uid"
OTHER_ADULT_UID = "other-adult-uid"
FAMILY_UID = "family-uid"
ADULT_NAME = "Rosa Gómez"
CONTACT_ID = "contact-1"
FOREIGN_CONTACT_ID = "contact-of-other-adult"
ROOM_NAME = "cs-test"
ROOM_URL = "https://acme.daily.co/cs-test"
BEARER_DETAIL = "Debes enviar un token Bearer."
CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."
CALL_NOT_FOUND_DETAIL = "La llamada no existe."
ADULT_ID_REQUIRED_DETAIL = "Datos inválidos: adult_id: es obligatorio"
CALL_KEYS = {"id", "contact_id", "started_at", "status"}
T0 = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
CONTACT_UID = "contact-account-uid"
CONTACT_EMAIL = "ana.perez@example.com"
CONTACT_NAME = "Ana Pérez"
CONTACT_FCM_TOKEN = "fcm-token-ana"


class FakeDailyClient:
    """Sustituto de DailyClient: nunca hace red y registra los argumentos recibidos."""

    def __init__(self) -> None:
        self.expires_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
            hours=1
        )
        self.error: Exception | None = None
        self.token_error: Exception | None = None
        # Falla solo el token de ese participante (por ejemplo, el del contacto, P20).
        self.token_errors_by_user: dict[str, Exception] = {}
        self.room_calls: list[dict] = []
        self.token_calls: list[dict] = []

    async def create_room(
        self, *, audio_only: bool, now: datetime | None = None
    ) -> DailyRoom:
        self.room_calls.append({"audio_only": audio_only})
        if self.error is not None:
            raise self.error
        return DailyRoom(name=ROOM_NAME, url=ROOM_URL, expires_at=self.expires_at)

    async def create_meeting_token(
        self,
        *,
        room_name: str,
        user_id: str,
        user_name: str,
        audio_only: bool,
        expires_at: datetime,
    ) -> str:
        self.token_calls.append(
            {
                "room_name": room_name,
                "user_id": user_id,
                "user_name": user_name,
                "audio_only": audio_only,
                "expires_at": expires_at,
            }
        )
        if self.token_error is not None:
            raise self.token_error
        user_error = self.token_errors_by_user.get(user_id)
        if user_error is not None:
            raise user_error
        return f"token-{user_id}"


class FakeSendToTokens:
    """Sustituto de fcm_client.send_to_tokens: registra cada envío, sin red."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, tokens, title, body, data):
        self.calls.append(
            {"tokens": list(tokens), "title": title, "body": body, "data": dict(data)}
        )
        if self.error is not None:
            raise self.error
        return FcmResult(success_count=len(tokens), failure_count=0, invalid_tokens=[])


@pytest.fixture(autouse=True)
def state(monkeypatch):
    shared = SimpleNamespace(
        daily=FakeDailyClient(),
        contacts=InMemoryContactRepository(),
        calls=InMemoryCallRepository(),
        users=InMemoryUserRepository(),
        devices=InMemoryDeviceRepository(),
        push=FakeSendToTokens(),
    )
    # Ninguna push real: la de llamada entrante (P20) pasa por este fake.
    monkeypatch.setattr(fcm_client, "send_to_tokens", shared.push)
    # Nunca el get_daily_client real: el .env tiene la clave real y llamaría a api.daily.co.
    app.dependency_overrides[get_daily_client] = lambda: shared.daily
    app.dependency_overrides[get_contact_repository] = lambda: shared.contacts
    app.dependency_overrides[get_call_repository] = lambda: shared.calls
    app.dependency_overrides[get_user_repository] = lambda: shared.users
    app.dependency_overrides[get_device_repository] = lambda: shared.devices
    yield shared
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _user(
    role: Role | None,
    uid: str = ADULT_UID,
    display_name: str | None = ADULT_NAME,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=f"{uid}@example.com",
        display_name=display_name,
        role=role,
    )


def _as_user(role: Role | None, uid: str = ADULT_UID) -> None:
    user = _user(role, uid)
    app.dependency_overrides[get_current_user] = lambda: user


def _call_path(call_id: str) -> str:
    return CALL_PATH_TEMPLATE.format(call_id=call_id)


def _contact(
    contact_id: str = CONTACT_ID,
    *,
    calls: bool = True,
    video_calls: bool = True,
    email: str | None = None,
) -> Contact:
    return Contact(
        id=contact_id,
        first_name="Ana",
        last_name="Pérez",
        relationship="hija",
        phone="+573001234567",
        email=email,
        permissions=ContactPermissions(
            calls=calls,
            video_calls=video_calls,
            messages=True,
            help_alerts=True,
        ),
    )


def _record(
    call_id: str,
    started_at: datetime,
    *,
    adult_uid: str = ADULT_UID,
    status: CallStatus = CallStatus.NO_CONTESTADA,
) -> CallRecord:
    return CallRecord(
        id=call_id,
        adult_uid=adult_uid,
        contact_id=CONTACT_ID,
        started_at=started_at,
        status=status,
        audio_only=False,
        room_name=ROOM_NAME,
        room_url=ROOM_URL,
        room_expires_at=started_at + timedelta(hours=1),
    )


def _seed_history(state) -> None:
    state.calls.add(_record("oldest", T0, status=CallStatus.COMPLETADA))
    state.calls.add(_record("newest", T0 + timedelta(hours=2)))
    state.calls.add(
        _record("middle", T0 + timedelta(hours=1), status=CallStatus.CANCELADA)
    )
    state.calls.add(
        _record("foreign", T0 + timedelta(hours=3), adult_uid=OTHER_ADULT_UID)
    )


# --- Servicio ---


@pytest.mark.asyncio
async def test_start_call_video_uses_video_permission(state):
    state.contacts.add(ADULT_UID, _contact(calls=False, video_calls=True))

    result = await start_call(
        _user(Role.ADULTO_MAYOR),
        CallCreateRequest(contact_id=CONTACT_ID),
        state.contacts,
        state.calls,
        state.daily,
        state.users,
        state.devices,
    )

    assert result.room_url == ROOM_URL
    assert result.token == f"token-{ADULT_UID}"
    assert state.daily.room_calls == [{"audio_only": False}]
    assert state.daily.token_calls == [
        {
            "room_name": ROOM_NAME,
            "user_id": ADULT_UID,
            "user_name": ADULT_NAME,
            "audio_only": False,
            "expires_at": state.daily.expires_at,
        }
    ]


@pytest.mark.asyncio
async def test_start_call_audio_only_uses_calls_permission(state):
    state.contacts.add(ADULT_UID, _contact(calls=True, video_calls=False))
    adult_without_name = _user(Role.ADULTO_MAYOR, display_name=None)

    result = await start_call(
        adult_without_name,
        CallCreateRequest(contact_id=CONTACT_ID, audio_only=True),
        state.contacts,
        state.calls,
        state.daily,
        state.users,
        state.devices,
    )

    assert result.token == f"token-{ADULT_UID}"
    assert state.daily.room_calls == [{"audio_only": True}]
    assert len(state.daily.token_calls) == 1
    assert state.daily.token_calls[0]["audio_only"] is True
    # Sin display_name, el nombre visible en la sala es el uid.
    assert state.daily.token_calls[0]["user_name"] == ADULT_UID


@pytest.mark.asyncio
async def test_start_call_saves_record_as_no_contestada(state):
    state.contacts.add(ADULT_UID, _contact())
    before = datetime.now(timezone.utc).replace(microsecond=0)

    result = await start_call(
        _user(Role.ADULTO_MAYOR),
        CallCreateRequest(contact_id=CONTACT_ID, audio_only=True),
        state.contacts,
        state.calls,
        state.daily,
        state.users,
        state.devices,
    )

    after = datetime.now(timezone.utc)
    UUID(result.call_id)
    record = state.calls.get(ADULT_UID, result.call_id)
    assert record is not None
    assert record.id == result.call_id
    assert record.adult_uid == ADULT_UID
    assert record.contact_id == CONTACT_ID
    assert record.status == CallStatus.NO_CONTESTADA
    assert record.audio_only is True
    assert record.room_name == ROOM_NAME
    assert record.room_url == ROOM_URL
    assert record.room_expires_at == state.daily.expires_at
    assert record.started_at.utcoffset() == timedelta(0)
    assert record.started_at.microsecond == 0
    assert before <= record.started_at <= after
    assert state.calls.list_for_adult(ADULT_UID) == [record]


@pytest.mark.parametrize("contact_id", ["missing-contact", FOREIGN_CONTACT_ID])
@pytest.mark.asyncio
async def test_start_call_unknown_or_foreign_contact_raises_not_found(
    state, contact_id
):
    state.contacts.add(ADULT_UID, _contact())
    state.contacts.add(OTHER_ADULT_UID, _contact(FOREIGN_CONTACT_ID))

    with pytest.raises(ContactNotFoundError):
        await start_call(
            _user(Role.ADULTO_MAYOR),
            CallCreateRequest(contact_id=contact_id),
            state.contacts,
            state.calls,
            state.daily,
            state.users,
            state.devices,
        )

    assert state.daily.room_calls == []
    assert state.daily.token_calls == []
    assert state.calls.list_for_adult(ADULT_UID) == []


@pytest.mark.parametrize(
    ("audio_only", "calls", "video_calls"),
    [(True, False, True), (False, True, False)],
)
@pytest.mark.asyncio
async def test_start_call_permission_false_raises_denied(
    state, audio_only, calls, video_calls
):
    state.contacts.add(ADULT_UID, _contact(calls=calls, video_calls=video_calls))

    with pytest.raises(CallPermissionDeniedError):
        await start_call(
            _user(Role.ADULTO_MAYOR),
            CallCreateRequest(contact_id=CONTACT_ID, audio_only=audio_only),
            state.contacts,
            state.calls,
            state.daily,
            state.users,
            state.devices,
        )

    assert state.daily.room_calls == []
    assert state.daily.token_calls == []
    assert state.calls.list_for_adult(ADULT_UID) == []


@pytest.mark.parametrize("failing_step", ["room", "token"])
@pytest.mark.asyncio
async def test_start_call_daily_failure_saves_nothing(state, failing_step):
    state.contacts.add(ADULT_UID, _contact())
    error = DailyUnavailableError("Daily.co no está disponible temporalmente.")
    if failing_step == "room":
        state.daily.error = error
    else:
        state.daily.token_error = error

    with pytest.raises(DailyUnavailableError):
        await start_call(
            _user(Role.ADULTO_MAYOR),
            CallCreateRequest(contact_id=CONTACT_ID),
            state.contacts,
            state.calls,
            state.daily,
            state.users,
            state.devices,
        )

    assert state.calls.list_for_adult(ADULT_UID) == []
    expected_token_calls = 0 if failing_step == "room" else 1
    assert len(state.daily.token_calls) == expected_token_calls


def test_update_call_status_changes_status(state):
    state.calls.add(_record("call-1", T0))

    result = update_call_status(
        ADULT_UID, "call-1", CallStatus.COMPLETADA, state.calls
    )

    assert result == CallResponse(
        id="call-1",
        contact_id=CONTACT_ID,
        started_at=T0,
        status=CallStatus.COMPLETADA,
    )
    assert state.calls.get(ADULT_UID, "call-1").status == CallStatus.COMPLETADA


@pytest.mark.parametrize(
    ("adult_uid", "call_id"),
    [
        (ADULT_UID, "missing-call"),
        (ADULT_UID, "foreign-call"),
        (OTHER_ADULT_UID, "call-1"),
    ],
)
def test_update_call_status_foreign_or_missing_raises_not_found(
    state, adult_uid, call_id
):
    state.calls.add(_record("call-1", T0))
    state.calls.add(_record("foreign-call", T0, adult_uid=OTHER_ADULT_UID))

    with pytest.raises(CallNotFoundError):
        update_call_status(adult_uid, call_id, CallStatus.CANCELADA, state.calls)

    assert state.calls.get(ADULT_UID, "call-1").status == CallStatus.NO_CONTESTADA
    assert (
        state.calls.get(OTHER_ADULT_UID, "foreign-call").status
        == CallStatus.NO_CONTESTADA
    )


def test_list_calls_most_recent_first(state):
    _seed_history(state)

    result = list_calls(ADULT_UID, state.calls)

    assert [call.id for call in result] == ["newest", "middle", "oldest"]
    assert all(isinstance(call, CallResponse) for call in result)
    assert list_calls("adult-without-calls", state.calls) == []


# --- Rutas: L1 POST /api/calls ---


def test_l1_returns_call_id_room_url_token(state, client):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact())

    response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"call_id", "room_url", "token"}
    UUID(body["call_id"])
    assert body["room_url"] == ROOM_URL
    assert body["token"] == f"token-{ADULT_UID}"
    assert state.daily.room_calls == [{"audio_only": False}]
    record = state.calls.get(ADULT_UID, body["call_id"])
    assert record is not None
    assert record.status == CallStatus.NO_CONTESTADA


def test_l1_requires_bearer(state, client):
    state.contacts.add(ADULT_UID, _contact())

    response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": BEARER_DETAIL}
    assert state.daily.room_calls == []


@pytest.mark.parametrize(
    "role", [Role.FAMILIAR, Role.CUIDADOR, Role.ADMINISTRADOR, None]
)
def test_l1_rejects_non_adult_roles(state, client, role):
    _as_user(role, uid=FAMILY_UID)
    state.contacts.add(FAMILY_UID, _contact())

    response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})
    # El rol se comprueba antes de validar el cuerpo.
    response_without_body = client.post(CALLS_PATH, json={})

    for result in (response, response_without_body):
        assert result.status_code == 403
        assert result.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.daily.room_calls == []


@pytest.mark.parametrize("contact_id", ["missing-contact", FOREIGN_CONTACT_ID])
def test_l1_unknown_contact_is_404(state, client, contact_id):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(OTHER_ADULT_UID, _contact(FOREIGN_CONTACT_ID))

    response = client.post(CALLS_PATH, json={"contact_id": contact_id})

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert state.daily.room_calls == []


@pytest.mark.parametrize(
    ("audio_only", "calls", "video_calls"),
    [(True, False, True), (False, True, False)],
)
def test_l1_permission_false_is_403(state, client, audio_only, calls, video_calls):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact(calls=calls, video_calls=video_calls))

    response = client.post(
        CALLS_PATH, json={"contact_id": CONTACT_ID, "audio_only": audio_only}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.daily.room_calls == []
    assert state.calls.list_for_adult(ADULT_UID) == []


@pytest.mark.parametrize("body", [{}, {"audio_only": True}])
def test_l1_missing_contact_id_is_422(state, client, body):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(CALLS_PATH, json=body)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Datos inválidos: contact_id: es obligatorio"
    }
    assert state.daily.room_calls == []


def test_l1_daily_unavailable_is_503(state, client, caplog):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact())

    def daily_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            401,
            json={"error": "authentication-error", "info": "invalid key test-key"},
        )

    # DailyClient real con transporte simulado: nunca sale a api.daily.co.
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(daily_handler))
    real_daily = DailyClient(api_key="test-key", domain="acme", client=http_client)
    app.dependency_overrides[get_daily_client] = lambda: real_daily

    try:
        with caplog.at_level(logging.ERROR, logger="app.api.calls_router"):
            response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})
    finally:
        asyncio.run(http_client.aclose())

    assert response.status_code == 503
    assert response.json() == {
        "detail": "El servicio de llamadas no está disponible. Intenta más tarde."
    }
    assert response.json()["detail"] == CALLS_UNAVAILABLE_DETAIL
    assert "DailyUnavailableError" in caplog.text
    assert "test-key" not in caplog.text
    assert "test-key" not in response.text
    assert state.calls.list_for_adult(ADULT_UID) == []


def test_l1_daily_not_configured_is_503(state, client, caplog):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact())
    app.dependency_overrides[get_daily_client] = lambda: DailyClient(
        api_key="", domain=""
    )

    with caplog.at_level(logging.ERROR, logger="app.api.calls_router"):
        response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 503
    assert response.json() == {"detail": CALLS_UNAVAILABLE_DETAIL}
    assert "DailyNotConfiguredError" in caplog.text
    assert state.calls.list_for_adult(ADULT_UID) == []


# --- Rutas: L2 PATCH /api/calls/{call_id} ---


def test_l2_updates_status(state, client):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact())
    call_id = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID}).json()[
        "call_id"
    ]

    response = client.patch(_call_path(call_id), json={"status": "completada"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == CALL_KEYS
    assert body["id"] == call_id
    assert body["contact_id"] == CONTACT_ID
    assert body["status"] == "completada"
    assert state.calls.get(ADULT_UID, call_id).status == CallStatus.COMPLETADA
    assert client.get(CALLS_PATH).json() == [body]


@pytest.mark.parametrize("call_id", ["missing-call", "foreign-call"])
def test_l2_unknown_or_foreign_call_is_404(state, client, call_id):
    _as_user(Role.ADULTO_MAYOR)
    state.calls.add(_record("foreign-call", T0, adult_uid=OTHER_ADULT_UID))

    response = client.patch(_call_path(call_id), json={"status": "cancelada"})

    assert response.status_code == 404
    assert response.json() == {"detail": CALL_NOT_FOUND_DETAIL}
    assert (
        state.calls.get(OTHER_ADULT_UID, "foreign-call").status
        == CallStatus.NO_CONTESTADA
    )


@pytest.mark.parametrize("body", [{"status": "en_curso"}, {"status": "Completada"}])
def test_l2_invalid_status_is_422(state, client, body):
    _as_user(Role.ADULTO_MAYOR)
    state.calls.add(_record("call-1", T0))

    response = client.patch(_call_path("call-1"), json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: status: no es válido"}
    assert state.calls.get(ADULT_UID, "call-1").status == CallStatus.NO_CONTESTADA


def test_l2_requires_bearer(state, client):
    state.calls.add(_record("call-1", T0))

    response = client.patch(_call_path("call-1"), json={"status": "completada"})

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert state.calls.get(ADULT_UID, "call-1").status == CallStatus.NO_CONTESTADA


@pytest.mark.parametrize(
    "role", [Role.FAMILIAR, Role.CUIDADOR, Role.ADMINISTRADOR, None]
)
def test_l2_rejects_family(state, client, role):
    _as_user(role, uid=FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.calls.add(_record("call-1", T0))

    # adult_id no es entrada de L2: aunque llegue, no da acceso.
    response = client.patch(
        _call_path("call-1"),
        params={"adult_id": ADULT_UID},
        json={"status": "completada"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.calls.get(ADULT_UID, "call-1").status == CallStatus.NO_CONTESTADA


# --- Rutas: L3 GET /api/calls ---


def test_l3_adult_gets_own_history(state, client):
    _as_user(Role.ADULTO_MAYOR)
    _seed_history(state)

    response = client.get(CALLS_PATH)
    response_with_own_id = client.get(CALLS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    body = response.json()
    assert [call["id"] for call in body] == ["newest", "middle", "oldest"]
    assert all(set(call) == CALL_KEYS for call in body)
    assert body[0] == {
        "id": "newest",
        "contact_id": CONTACT_ID,
        "started_at": "2026-10-17T17:00:00Z",
        "status": "no_contestada",
    }
    assert [call["status"] for call in body] == [
        "no_contestada",
        "cancelada",
        "completada",
    ]
    for call in body:
        assert datetime.fromisoformat(call["started_at"]).tzinfo is not None
    assert response_with_own_id.status_code == 200
    assert response_with_own_id.json() == body


def test_l3_adult_with_foreign_adult_id_is_403(state, client):
    _as_user(Role.ADULTO_MAYOR)
    _seed_history(state)

    response = client.get(CALLS_PATH, params={"adult_id": OTHER_ADULT_UID})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


@pytest.mark.parametrize("adult_id", [None, "", "   "])
@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_l3_family_without_adult_id_is_422(state, client, role, adult_id):
    _as_user(role, uid=FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, role)
    params = {} if adult_id is None else {"adult_id": adult_id}

    response = client.get(CALLS_PATH, params=params)

    assert response.status_code == 422
    assert response.json() == {"detail": ADULT_ID_REQUIRED_DETAIL}


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_l3_unlinked_family_is_403(state, client, role):
    _as_user(role, uid=FAMILY_UID)
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, role)
    _seed_history(state)

    response = client.get(CALLS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_l3_linked_family_and_caregiver_get_history(state, client, role):
    _as_user(role, uid=FAMILY_UID)
    state.users.link_adult(FAMILY_UID, ADULT_UID, role)
    _seed_history(state)

    response = client.get(CALLS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    body = response.json()
    assert [call["id"] for call in body] == ["newest", "middle", "oldest"]
    assert all(set(call) == CALL_KEYS for call in body)


@pytest.mark.parametrize("adult_id", [None, ADULT_UID])
def test_l3_admin_is_403(state, client, adult_id):
    _as_user(Role.ADMINISTRADOR, uid="admin-uid")
    state.users.link_adult("admin-uid", ADULT_UID, Role.FAMILIAR)
    _seed_history(state)
    params = {} if adult_id is None else {"adult_id": adult_id}

    response = client.get(CALLS_PATH, params=params)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


def test_l3_requires_bearer(state, client):
    _seed_history(state)

    response = client.get(CALLS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}


def test_openapi_lists_call_routes():
    paths = app.openapi()["paths"]

    assert {"post", "get"} <= set(paths[CALLS_PATH])
    assert set(paths[CALL_PATH_TEMPLATE]) == {"patch"}
    post_success = paths[CALLS_PATH]["post"]["responses"]["200"]
    assert post_success["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CallStartResponse"
    }


# --- Push de llamada entrante al contacto (P20, D-R2-16) ---


def _link_contact_account(state, *, verified: bool = True, device: bool = True) -> None:
    """La cuenta del contacto verificó su correo (A10) y registró un navegador."""
    if verified:
        state.users.mark_email_verified(CONTACT_UID, CONTACT_EMAIL)
    if device:
        state.devices.register(CONTACT_UID, CONTACT_FCM_TOKEN)


def _adult_token_call(audio_only: bool, expires_at: datetime) -> dict:
    return {
        "room_name": ROOM_NAME,
        "user_id": ADULT_UID,
        "user_name": ADULT_NAME,
        "audio_only": audio_only,
        "expires_at": expires_at,
    }


@pytest.mark.parametrize(
    ("audio_only", "title", "audio_only_text"),
    [(False, "Videollamada entrante", "false"), (True, "Llamada entrante", "true")],
)
def test_l1_pushes_callee_token_when_contact_linked_with_device(
    state, client, audio_only, title, audio_only_text
):
    _as_user(Role.ADULTO_MAYOR)
    # El correo del contacto se compara normalizado (strip y minúsculas).
    state.contacts.add(ADULT_UID, _contact(email=" Ana.Perez@Example.com "))
    _link_contact_account(state)

    response = client.post(
        CALLS_PATH, json={"contact_id": CONTACT_ID, "audio_only": audio_only}
    )

    assert response.status_code == 200
    body = response.json()
    # El adulto recibe su propio token; el del contacto solo viaja en la push.
    assert body["token"] == f"token-{ADULT_UID}"
    assert state.daily.token_calls == [
        _adult_token_call(audio_only, state.daily.expires_at),
        {
            "room_name": ROOM_NAME,
            "user_id": CONTACT_UID,
            "user_name": CONTACT_NAME,
            "audio_only": audio_only,
            "expires_at": state.daily.expires_at,
        },
    ]
    assert state.push.calls == [
        {
            "tokens": [CONTACT_FCM_TOKEN],
            "title": title,
            "body": f"{ADULT_NAME} te está llamando",
            "data": {
                "type": "call",
                "call_id": body["call_id"],
                "room_url": ROOM_URL,
                "token": f"token-{CONTACT_UID}",
                "audio_only": audio_only_text,
                "from_name": ADULT_NAME,
            },
        }
    ]
    assert all(
        isinstance(value, str) for value in state.push.calls[0]["data"].values()
    )


@pytest.mark.asyncio
async def test_start_call_push_from_name_falls_back_to_uid(state):
    state.contacts.add(ADULT_UID, _contact(email=CONTACT_EMAIL))
    _link_contact_account(state)

    result = await start_call(
        _user(Role.ADULTO_MAYOR, display_name=None),
        CallCreateRequest(contact_id=CONTACT_ID),
        state.contacts,
        state.calls,
        state.daily,
        state.users,
        state.devices,
    )

    assert len(state.push.calls) == 1
    push = state.push.calls[0]
    assert push["body"] == f"{ADULT_UID} te está llamando"
    assert push["data"]["from_name"] == ADULT_UID
    assert push["data"]["call_id"] == result.call_id


@pytest.mark.parametrize(
    "case", ["contact_without_email", "email_not_verified", "other_verified_email"]
)
def test_l1_no_push_when_contact_not_linked(state, client, case):
    _as_user(Role.ADULTO_MAYOR)
    email = None if case == "contact_without_email" else CONTACT_EMAIL
    state.contacts.add(ADULT_UID, _contact(email=email))
    # La persona tiene navegador registrado, pero su correo no está vinculado al contacto.
    state.devices.register(CONTACT_UID, CONTACT_FCM_TOKEN)
    if case == "other_verified_email":
        state.users.mark_email_verified(CONTACT_UID, "otra.persona@example.com")

    response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    assert response.json()["token"] == f"token-{ADULT_UID}"
    assert state.push.calls == []
    assert state.daily.token_calls == [
        _adult_token_call(False, state.daily.expires_at)
    ]


def test_l1_no_push_when_contact_has_no_device_and_no_second_token(state, client):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact(email=CONTACT_EMAIL))
    _link_contact_account(state, device=False)
    # Un navegador de otra persona no cuenta.
    state.devices.register("someone-else", "fcm-token-someone-else")

    response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    assert state.push.calls == []
    assert state.daily.token_calls == [
        _adult_token_call(False, state.daily.expires_at)
    ]
    assert len(state.calls.list_for_adult(ADULT_UID)) == 1


def test_l1_callee_token_failure_still_200(state, client, caplog):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact(email=CONTACT_EMAIL))
    _link_contact_account(state)
    state.daily.token_errors_by_user[CONTACT_UID] = DailyUnavailableError(
        "Daily.co no está disponible temporalmente."
    )

    with caplog.at_level(logging.ERROR, logger="app.services.call_service"):
        response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    body = response.json()
    assert body["token"] == f"token-{ADULT_UID}"
    assert state.calls.get(ADULT_UID, body["call_id"]) is not None
    assert [call["user_id"] for call in state.daily.token_calls] == [
        ADULT_UID,
        CONTACT_UID,
    ]
    assert state.push.calls == []
    assert "DailyUnavailableError" in caplog.text


@pytest.mark.parametrize("failure", ["fcm_unavailable", "unexpected"])
def test_l1_push_failure_still_200(state, client, caplog, monkeypatch, failure):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact(email=CONTACT_EMAIL))
    _link_contact_account(state)
    if failure == "fcm_unavailable":
        state.push.error = FcmUnavailableError(
            "El servicio de notificaciones no está disponible."
        )
    else:

        def exploding_notify_users(*args, **kwargs):
            raise RuntimeError("fallo inesperado")

        monkeypatch.setattr(push_service, "notify_users", exploding_notify_users)

    with caplog.at_level(logging.ERROR):
        response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    body = response.json()
    assert body["room_url"] == ROOM_URL
    assert body["token"] == f"token-{ADULT_UID}"
    assert state.calls.get(ADULT_UID, body["call_id"]) is not None
    if failure == "fcm_unavailable":
        assert len(state.push.calls) == 1
        assert "FcmUnavailableError" in caplog.text
    else:
        assert state.push.calls == []
        assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("push_fails", [False, True])
def test_callee_token_not_stored_nor_logged(state, client, caplog, push_fails):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact(email=CONTACT_EMAIL))
    _link_contact_account(state)
    if push_fails:
        state.push.error = FcmUnavailableError(
            "El servicio de notificaciones no está disponible."
        )
    callee_token = f"token-{CONTACT_UID}"

    with caplog.at_level(logging.DEBUG):
        response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    assert callee_token not in response.text
    # La push sí lo lleva: es su único canal hacia el contacto.
    assert state.push.calls[0]["data"]["token"] == callee_token
    record = state.calls.get(ADULT_UID, response.json()["call_id"])
    assert record is not None
    assert callee_token not in {str(value) for value in asdict(record).values()}
    assert callee_token not in caplog.text
    assert CONTACT_FCM_TOKEN not in caplog.text


# --- Ronda 3 (D-R3-15, D-R3-23): la resolución del destinatario no tumba L1 ---


def test_l1_callee_lookup_database_error_still_200_and_call_saved(
    state, client, caplog, monkeypatch
):
    _as_user(Role.ADULTO_MAYOR)
    state.contacts.add(ADULT_UID, _contact(email=CONTACT_EMAIL))
    _link_contact_account(state)

    def failing_lookup(email):
        raise OperationalError("x", {}, Exception("x"))

    monkeypatch.setattr(state.users, "find_uid_by_verified_email", failing_lookup)

    with caplog.at_level(logging.ERROR, logger="app.services.call_service"):
        response = client.post(CALLS_PATH, json={"contact_id": CONTACT_ID})

    assert response.status_code == 200
    body = response.json()
    assert body["room_url"] == ROOM_URL
    assert body["token"] == f"token-{ADULT_UID}"
    record = state.calls.get(ADULT_UID, body["call_id"])
    assert record is not None
    assert record.status == CallStatus.NO_CONTESTADA
    # Sin destinatario resuelto no se pide el segundo token ni se envía push.
    assert state.daily.token_calls == [
        _adult_token_call(False, state.daily.expires_at)
    ]
    assert state.push.calls == []
    assert "OperationalError" in caplog.text
