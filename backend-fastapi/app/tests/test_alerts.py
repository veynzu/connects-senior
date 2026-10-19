import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api import alerts_router
from app.core.config import get_settings
from app.core.scheduler import registered_ticks
from app.core.security import FORBIDDEN_DETAIL, get_current_user
from app.integrations import fcm_client
from app.integrations.fcm_client import FcmResult, FcmUnavailableError
from app.main import app
from app.models.alerts import AlertCreateRequest, AlertDetail, AlertRecord, AlertSummary
from app.models.auth import AuthenticatedUser
from app.models.contacts import Contact, ContactPermissions
from app.models.enums import AlertCategory, AlertStatus, Role
from app.models.users import CaregiverPermissions, UserProfile
from app.repositories.alert_repository import (
    InMemoryAlertRepository,
    get_alert_repository,
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
from app.services import alert_service
from app.services.alert_service import (
    ALERT_PUSH_TITLE,
    MAX_ESCALATION_ROUNDS,
    AlertAlreadyClosedError,
    AlertLocationIncompleteError,
    AlertNotFoundError,
    AlertPermissionDeniedError,
    acknowledge_alert,
    alert_recipients,
    create_alert,
    escalate_pending_alerts,
    get_alert,
    list_alerts,
    update_alert_status,
)


ALERTS_PATH = "/api/alerts"
ALERT_PATH_TEMPLATE = "/api/alerts/{alert_id}"
ACK_PATH_TEMPLATE = "/api/alerts/{alert_id}/acknowledge"
ADULT_UID = "adult-uid"
OTHER_ADULT_UID = "other-adult-uid"
FAMILY_UID = "family-uid"
OTHER_FAMILY_UID = "other-family-uid"
CAREGIVER_UID = "caregiver-uid"
MUTED_CAREGIVER_UID = "muted-caregiver-uid"
BARE_CAREGIVER_UID = "bare-caregiver-uid"
CONTACT_UID = "contact-user-uid"
OTHER_CONTACT_UID = "other-contact-user-uid"
ADMIN_UID = "admin-uid"
NO_ROLE_UID = "no-role-uid"
ADULT_NAME = "Rosa Gómez"
CONTACT_EMAIL = "hija@example.com"
TOKEN_FAMILY = "fcm-family"
TOKEN_CAREGIVER = "fcm-caregiver"
TOKEN_MUTED = "fcm-muted-caregiver"
TOKEN_BARE = "fcm-bare-caregiver"
TOKEN_CONTACT_1 = "fcm-contact-1"
TOKEN_CONTACT_2 = "fcm-contact-2"
TOKEN_OTHER_CONTACT = "fcm-other-contact"
BEARER_DETAIL = "Debes enviar un token Bearer."
ALERT_NOT_FOUND_DETAIL = "La solicitud no existe."
ALERT_CLOSED_DETAIL = "La solicitud ya fue atendida."
LOCATION_INCOMPLETE_DETAIL = (
    "Datos inválidos: latitude y longitude deben enviarse juntas"
)
ADULT_ID_REQUIRED_DETAIL = "Datos inválidos: adult_id: es obligatorio"
BODY_UNAVAILABLE = f"{ADULT_NAME} pidió ayuda. Ubicación no disponible."
BODY_AVAILABLE = f"{ADULT_NAME} pidió ayuda. Ubicación disponible."
CREATE_KEYS = {"id", "status", "created_at", "location_available", "has_trusted_contacts"}
SUMMARY_KEYS = {"id", "adult_name", "category", "status", "created_at", "location_available"}
DETAIL_KEYS = SUMMARY_KEYS | {"latitude", "longitude"}
T0 = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
ESCALATION_SECONDS = 120
RECEIVE_ALERTS = CaregiverPermissions(
    manage_contacts=False, manage_reminders=False, receive_alerts=True
)
NO_RECEIVE_ALERTS = CaregiverPermissions(
    manage_contacts=True, manage_reminders=True, receive_alerts=False
)


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

    @property
    def tokens(self) -> list[list[str]]:
        return [call["tokens"] for call in self.calls]


@pytest.fixture(autouse=True)
def state(monkeypatch):
    shared = SimpleNamespace(
        alerts=InMemoryAlertRepository(),
        contacts=InMemoryContactRepository(),
        users=InMemoryUserRepository(),
        devices=InMemoryDeviceRepository(),
        push=FakeSendToTokens(),
    )
    monkeypatch.setattr(fcm_client, "send_to_tokens", shared.push)
    app.dependency_overrides[get_alert_repository] = lambda: shared.alerts
    app.dependency_overrides[get_contact_repository] = lambda: shared.contacts
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
    *,
    email: str | None = None,
    email_verified: bool | None = None,
    display_name: str | None = ADULT_NAME,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=email if email is not None else f"{uid}@example.com",
        email_verified=email_verified,
        display_name=display_name,
        role=role,
    )


def _as_user(role: Role | None, uid: str = ADULT_UID, **kwargs) -> AuthenticatedUser:
    user = _user(role, uid, **kwargs)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _contact(
    contact_id: str = "contact-1",
    *,
    email: str | None = None,
    help_alerts: bool = True,
) -> Contact:
    return Contact(
        id=contact_id,
        first_name="Ana",
        last_name="Pérez",
        relationship="hija",
        email=email,
        permissions=ContactPermissions(
            calls=True, video_calls=True, messages=True, help_alerts=help_alerts
        ),
    )


def _link_family(state, uid: str = FAMILY_UID, *, token: str | None = TOKEN_FAMILY):
    state.users.link_adult(uid, ADULT_UID, Role.FAMILIAR)
    if token is not None:
        state.devices.register(uid, token)


def _link_caregiver(
    state,
    uid: str = CAREGIVER_UID,
    permissions: CaregiverPermissions | None = RECEIVE_ALERTS,
    *,
    token: str | None = TOKEN_CAREGIVER,
):
    state.users.link_adult(uid, ADULT_UID, Role.CUIDADOR, permissions)
    if token is not None:
        state.devices.register(uid, token)


def _create(state, now: datetime = T0, adult_uid: str = ADULT_UID, **payload):
    return create_alert(
        _user(Role.ADULTO_MAYOR, adult_uid),
        AlertCreateRequest(**payload),
        state.alerts,
        state.contacts,
        state.users,
        state.devices,
        now=now,
    )


def _alert_path(alert_id: str) -> str:
    return ALERT_PATH_TEMPLATE.format(alert_id=alert_id)


def _ack_path(alert_id: str) -> str:
    return ACK_PATH_TEMPLATE.format(alert_id=alert_id)


def _expected_data(alert_id: str, category: str = "", location: str = "false") -> dict:
    return {
        "type": "alert",
        "alert_id": alert_id,
        "adult_id": ADULT_UID,
        "adult_name": ADULT_NAME,
        "category": category,
        "location_available": location,
    }


def _escalate(state, now: datetime) -> int:
    return escalate_pending_alerts(
        state.alerts,
        state.contacts,
        state.users,
        state.devices,
        ESCALATION_SECONDS,
        now=now,
    )


# --- H1: crear solicitud ---


def test_create_alert_without_body_200(state, client):
    _as_user(Role.ADULTO_MAYOR)
    before = datetime.now(timezone.utc).replace(microsecond=0)

    response = client.post(ALERTS_PATH)

    after = datetime.now(timezone.utc)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == CREATE_KEYS
    UUID(body["id"])
    assert body["status"] == "pendiente"
    assert body["location_available"] is False
    assert body["has_trusted_contacts"] is False
    created_at = datetime.fromisoformat(body["created_at"])
    assert created_at.utcoffset() == timedelta(0)
    assert before <= created_at <= after
    record = state.alerts.get(body["id"])
    assert record is not None
    assert record.adult_uid == ADULT_UID
    assert record.adult_name == ADULT_NAME
    assert record.category is None
    assert record.status == AlertStatus.PENDIENTE
    assert record.latitude is None and record.longitude is None
    assert record.has_trusted_contacts is False
    assert record.notification_round == 0
    assert record.last_notified_at == record.created_at == created_at
    assert record.acknowledged_by is None


def test_create_alert_with_empty_json_body_200(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH, json={})

    assert response.status_code == 200
    assert response.json()["location_available"] is False


def test_create_alert_without_location_location_available_false_and_push_says_unavailable(
    state, client
):
    _link_caregiver(state)
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH, json={"category": "ayuda_en_casa"})

    assert response.status_code == 200
    body = response.json()
    assert body["location_available"] is False
    assert state.alerts.get(body["id"]).category == AlertCategory.AYUDA_EN_CASA
    assert state.push.calls == [
        {
            "tokens": [TOKEN_CAREGIVER],
            "title": ALERT_PUSH_TITLE,
            "body": BODY_UNAVAILABLE,
            "data": _expected_data(body["id"], category="ayuda_en_casa"),
        }
    ]
    assert ALERT_PUSH_TITLE == "Solicitud de ayuda"


def test_create_alert_with_location(state, client):
    _link_caregiver(state)
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(
        ALERTS_PATH, json={"latitude": 4.5339, "longitude": -75.6811}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["location_available"] is True
    record = state.alerts.get(body["id"])
    assert record.latitude == 4.5339
    assert record.longitude == -75.6811
    assert len(state.push.calls) == 1
    assert state.push.calls[0]["body"] == BODY_AVAILABLE
    assert state.push.calls[0]["data"] == _expected_data(body["id"], location="true")
    assert all(isinstance(value, str) for value in state.push.calls[0]["data"].values())


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 4.5},
        {"longitude": -75.6},
        {"latitude": 4.5, "longitude": None},
        {"latitude": None, "longitude": -75.6, "category": "otro"},
    ],
)
def test_create_alert_single_coordinate_422(state, client, payload):
    _link_family(state)
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH, json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": LOCATION_INCOMPLETE_DETAIL}
    assert state.alerts.list_for_adult(ADULT_UID) == []
    assert state.push.calls == []


@pytest.mark.parametrize("category", ["emergencia", "AYUDA_EN_CASA", 5])
def test_create_alert_invalid_category_422(state, client, category):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH, json={"category": category})

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: category: no es válido"}
    assert state.alerts.list_for_adult(ADULT_UID) == []


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"latitude": 90.5, "longitude": 0}, "latitude"),
        ({"latitude": 0, "longitude": -180.5}, "longitude"),
        ({"latitude": "norte", "longitude": 0}, "latitude"),
    ],
)
def test_create_alert_invalid_coordinates_422(state, client, payload, field):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH, json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": f"Datos inválidos: {field}: no es válido"}
    assert state.alerts.list_for_adult(ADULT_UID) == []


def test_create_alert_no_trusted_contacts_pushes_family(state, client):
    _link_family(state)
    _link_caregiver(state)
    # Contacto sin help_alerts, con cuenta verificada y dispositivo: no es destinatario.
    state.contacts.add(
        ADULT_UID, _contact("contact-x", email=CONTACT_EMAIL, help_alerts=False)
    )
    state.users.mark_email_verified(CONTACT_UID, CONTACT_EMAIL)
    state.devices.register(CONTACT_UID, TOKEN_CONTACT_1)
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["has_trusted_contacts"] is False
    # RF-26: sin contactos de confianza, también el familiar responsable.
    assert state.push.tokens == [[TOKEN_CAREGIVER], [TOKEN_FAMILY]]
    assert state.push.calls[1]["data"] == _expected_data(body["id"])
    assert state.push.calls[1]["body"] == BODY_UNAVAILABLE


@pytest.mark.parametrize(
    "scenario",
    ["contact_without_account", "email_never_verified", "verified_without_device"],
)
def test_create_alert_trusted_but_unreachable_pushes_family(state, client, scenario):
    _link_family(state)
    state.contacts.add(ADULT_UID, _contact(email=CONTACT_EMAIL, help_alerts=True))
    if scenario == "email_never_verified":
        # Hay una persona con ese correo y dispositivo, pero nunca lo verificó.
        state.users.add_profile(
            UserProfile(
                uid=CONTACT_UID,
                first_name="Ana",
                last_name="Pérez",
                email=CONTACT_EMAIL,
                role=Role.FAMILIAR,
            )
        )
        state.devices.register(CONTACT_UID, TOKEN_CONTACT_1)
    elif scenario == "verified_without_device":
        state.users.mark_email_verified(CONTACT_UID, CONTACT_EMAIL)
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH)

    assert response.status_code == 200
    assert response.json()["has_trusted_contacts"] is True
    # Regla derivada P9: ningún destinatario alcanzable -> familiar responsable.
    assert state.push.tokens == [[TOKEN_FAMILY]]


def test_create_alert_pushes_caregiver_with_receive_alerts_only(state, client):
    _link_family(state)
    _link_caregiver(state, CAREGIVER_UID, RECEIVE_ALERTS, token=TOKEN_CAREGIVER)
    _link_caregiver(state, MUTED_CAREGIVER_UID, NO_RECEIVE_ALERTS, token=TOKEN_MUTED)
    _link_caregiver(state, BARE_CAREGIVER_UID, None, token=TOKEN_BARE)
    # Contacto de confianza sin cuenta: has_trusted_contacts true.
    state.contacts.add(ADULT_UID, _contact(email="nadie@example.com"))
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH)

    assert response.status_code == 200
    assert response.json()["has_trusted_contacts"] is True
    # Hay un destinatario alcanzable: el familiar no recibe esta push.
    assert state.push.tokens == [[TOKEN_CAREGIVER]]


def test_create_alert_pushes_verified_contact_devices(state, client):
    _link_family(state)
    state.contacts.add(
        ADULT_UID, _contact("contact-trusted", email="Hija@Example.com", help_alerts=True)
    )
    state.contacts.add(
        ADULT_UID,
        _contact("contact-plain", email="otro@example.com", help_alerts=False),
    )
    state.users.mark_email_verified(CONTACT_UID, CONTACT_EMAIL)
    state.devices.register(CONTACT_UID, TOKEN_CONTACT_1)
    state.devices.register(CONTACT_UID, TOKEN_CONTACT_2)
    state.users.mark_email_verified(OTHER_CONTACT_UID, "otro@example.com")
    state.devices.register(OTHER_CONTACT_UID, TOKEN_OTHER_CONTACT)
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH, json={"category": "compania"})

    assert response.status_code == 200
    assert response.json()["has_trusted_contacts"] is True
    assert state.push.tokens == [[TOKEN_CONTACT_1, TOKEN_CONTACT_2]]
    assert state.push.calls[0]["data"]["category"] == "compania"


def test_create_alert_push_failure_still_200(state, client):
    _link_caregiver(state)
    _link_family(state)
    state.push.error = FcmUnavailableError("El servicio de notificaciones no está disponible.")
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH)

    assert response.status_code == 200
    body = response.json()
    assert state.alerts.get(body["id"]) is not None
    # Se intentó la push al cuidador y, sin contactos de confianza, al familiar.
    assert state.push.tokens == [[TOKEN_CAREGIVER], [TOKEN_FAMILY]]


def test_create_alert_without_family_or_recipients_still_200(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(ALERTS_PATH)

    assert response.status_code == 200
    assert state.push.calls == []


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_create_alert_non_adult_403(state, client, role):
    _link_family(state)
    _as_user(role, FAMILY_UID)

    response = client.post(ALERTS_PATH, json={})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.alerts.list_pending() == []
    assert state.push.calls == []


@pytest.mark.parametrize(
    "role", [Role.FAMILIAR, Role.CUIDADOR, Role.ADMINISTRADOR, None]
)
def test_create_alert_service_rejects_non_adult(state, role):
    with pytest.raises(AlertPermissionDeniedError):
        create_alert(
            _user(role, FAMILY_UID),
            None,
            state.alerts,
            state.contacts,
            state.users,
            state.devices,
            now=T0,
        )

    assert state.alerts.list_pending() == []


def test_create_alert_service_single_coordinate_raises(state):
    with pytest.raises(AlertLocationIncompleteError):
        _create(state, latitude=4.5)

    assert state.alerts.list_pending() == []


@pytest.mark.parametrize(
    ("with_profile", "display_name", "expected"),
    [
        (True, "Otro Nombre", "Rosa María Gómez"),
        (False, ADULT_NAME, ADULT_NAME),
        (False, None, ADULT_UID),
    ],
)
def test_create_alert_adult_name_from_profile_token_or_uid(
    state, with_profile, display_name, expected
):
    if with_profile:
        state.users.add_profile(
            UserProfile(
                uid=ADULT_UID,
                first_name="Rosa María",
                last_name="Gómez",
                email="rosa@example.com",
                role=Role.ADULTO_MAYOR,
            )
        )

    created = create_alert(
        _user(Role.ADULTO_MAYOR, display_name=display_name),
        None,
        state.alerts,
        state.contacts,
        state.users,
        state.devices,
        now=T0,
    )

    assert state.alerts.get(created.id).adult_name == expected
    assert list_alerts(ADULT_UID, state.alerts)[0].adult_name == expected


def test_create_alert_service_uses_now_and_returns_contract_shape(state):
    created = _create(state, category=AlertCategory.PROBLEMA, latitude=1.0, longitude=2.0)

    assert created.created_at == T0
    assert created.status == AlertStatus.PENDIENTE
    assert created.location_available is True
    assert created.has_trusted_contacts is False
    assert set(created.model_dump()) == CREATE_KEYS


# --- H2: listar ---


def test_list_alerts_family_with_adult_id_desc(state, client):
    _link_family(state)
    first = _create(state, now=T0)
    newest_hour = _create(state, now=T0 + timedelta(hours=2))
    middle = _create(state, now=T0 + timedelta(hours=1), category=AlertCategory.OTRO)
    tie_first = _create(state, now=T0 + timedelta(hours=3))
    tie_second = _create(state, now=T0 + timedelta(hours=3))
    _create(state, now=T0 + timedelta(hours=4), adult_uid=OTHER_ADULT_UID)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.get(ALERTS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    body = response.json()
    # En empate dentro del mismo segundo va primero la última insertada.
    assert [item["id"] for item in body] == [
        tie_second.id,
        tie_first.id,
        newest_hour.id,
        middle.id,
        first.id,
    ]
    assert all(set(item) == SUMMARY_KEYS for item in body)
    middle_item = body[3]
    assert middle_item["adult_name"] == ADULT_NAME
    assert middle_item["category"] == "otro"
    assert middle_item["status"] == "pendiente"
    assert middle_item["location_available"] is False
    assert datetime.fromisoformat(middle_item["created_at"]) == T0 + timedelta(hours=1)
    assert body[4]["category"] is None


def test_list_alerts_adult_implicit(state, client):
    own = _create(state)
    _create(state, adult_uid=OTHER_ADULT_UID)
    _as_user(Role.ADULTO_MAYOR)

    without_adult_id = client.get(ALERTS_PATH)
    with_own_adult_id = client.get(ALERTS_PATH, params={"adult_id": ADULT_UID})

    assert without_adult_id.status_code == 200
    assert [item["id"] for item in without_adult_id.json()] == [own.id]
    assert with_own_adult_id.status_code == 200
    assert with_own_adult_id.json() == without_adult_id.json()


def test_list_alerts_caregiver_without_permissions_can_read(state, client):
    _link_caregiver(state, permissions=None)
    created = _create(state)
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.get(ALERTS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [created.id]


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_list_family_missing_adult_id_422(state, client, role):
    state.users.link_adult(FAMILY_UID, ADULT_UID, role)
    _create(state)
    _as_user(role, FAMILY_UID)

    response = client.get(ALERTS_PATH)

    assert response.status_code == 422
    assert response.json() == {"detail": ADULT_ID_REQUIRED_DETAIL}


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_list_unlinked_403(state, client, role):
    _create(state)
    _as_user(role, OTHER_FAMILY_UID)

    response = client.get(ALERTS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


def test_list_alerts_adult_foreign_adult_id_403(state, client):
    _create(state, adult_uid=OTHER_ADULT_UID)
    _as_user(Role.ADULTO_MAYOR)

    response = client.get(ALERTS_PATH, params={"adult_id": OTHER_ADULT_UID})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


def test_list_alerts_service_returns_summaries(state):
    created = _create(state, latitude=1.0, longitude=2.0)

    result = list_alerts(ADULT_UID, state.alerts)

    assert result == [
        AlertSummary(
            id=created.id,
            adult_name=ADULT_NAME,
            category=None,
            status=AlertStatus.PENDIENTE,
            created_at=T0,
            location_available=True,
        )
    ]
    assert list_alerts(OTHER_ADULT_UID, state.alerts) == []


# --- H3: detalle ---


def test_get_alert_includes_coordinates(state, client):
    _link_family(state)
    created = _create(
        state, category=AlertCategory.PROBLEMA, latitude=4.5339, longitude=-75.6811
    )
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.get(_alert_path(created.id))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == DETAIL_KEYS
    created_at = body.pop("created_at")
    assert datetime.fromisoformat(created_at) == T0
    assert body == {
        "id": created.id,
        "adult_name": ADULT_NAME,
        "category": "problema",
        "status": "pendiente",
        "location_available": True,
        "latitude": 4.5339,
        "longitude": -75.6811,
    }


def test_get_alert_without_location_returns_null_coordinates(state, client):
    created = _create(state)
    _as_user(Role.ADULTO_MAYOR)

    response = client.get(_alert_path(created.id))

    assert response.status_code == 200
    body = response.json()
    assert body["location_available"] is False
    assert body["latitude"] is None
    assert body["longitude"] is None


@pytest.mark.parametrize(
    ("role", "uid", "use_real_id"),
    [
        (Role.FAMILIAR, OTHER_FAMILY_UID, True),
        (Role.CUIDADOR, OTHER_FAMILY_UID, True),
        (Role.ADULTO_MAYOR, OTHER_ADULT_UID, True),
        (Role.FAMILIAR, FAMILY_UID, False),
    ],
)
def test_get_alert_unlinked_404(state, client, role, uid, use_real_id):
    _link_family(state)
    created = _create(state)
    _as_user(role, uid)
    alert_id = created.id if use_real_id else "missing-alert"

    response = client.get(_alert_path(alert_id))

    assert response.status_code == 404
    assert response.json() == {"detail": ALERT_NOT_FOUND_DETAIL}


@pytest.mark.parametrize("role", [Role.ADMINISTRADOR, None])
def test_get_alert_service_admin_or_no_role_not_found(state, role):
    created = _create(state)
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)

    with pytest.raises(AlertNotFoundError):
        get_alert(_user(role, ADMIN_UID), created.id, state.alerts, state.users)


@pytest.mark.parametrize("route", ["H3", "H4", "H5"])
def test_alert_contact_email_match_without_role_link_404(state, client, route):
    state.contacts.add(ADULT_UID, _contact(email=CONTACT_EMAIL, help_alerts=True))
    state.users.mark_email_verified(CONTACT_UID, CONTACT_EMAIL)
    state.devices.register(CONTACT_UID, TOKEN_CONTACT_1)
    created = _create(state)
    # El contacto recibió la push de H1, pero no tiene vínculo de rol con el adulto.
    assert state.push.tokens == [[TOKEN_CONTACT_1]]
    _as_user(Role.FAMILIAR, CONTACT_UID, email=CONTACT_EMAIL, email_verified=True)

    if route == "H3":
        response = client.get(_alert_path(created.id))
    elif route == "H4":
        response = client.post(_ack_path(created.id))
    else:
        response = client.patch(_alert_path(created.id), json={"status": "atendida"})

    assert response.status_code == 404
    assert response.json() == {"detail": ALERT_NOT_FOUND_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE


# --- H4: atender ---


def test_acknowledge_by_family_sets_en_atencion(state, client):
    _link_family(state)
    created = _create(state, latitude=1.5, longitude=2.5)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.post(_ack_path(created.id))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == DETAIL_KEYS
    assert body["id"] == created.id
    assert body["status"] == "en_atencion"
    assert body["latitude"] == 1.5
    assert body["longitude"] == 2.5
    record = state.alerts.get(created.id)
    assert record.status == AlertStatus.EN_ATENCION
    assert record.acknowledged_by == FAMILY_UID


def test_acknowledge_caregiver_without_receive_alerts_403(state, client):
    _link_caregiver(state, permissions=NO_RECEIVE_ALERTS)
    created = _create(state)
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.post(_ack_path(created.id))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE


def test_acknowledge_caregiver_linked_without_permissions_403(state, client):
    _link_caregiver(state, permissions=None)
    created = _create(state)
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.post(_ack_path(created.id))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE


def test_acknowledge_caregiver_with_receive_alerts(state, client):
    _link_caregiver(state)
    created = _create(state)
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.post(_ack_path(created.id))

    assert response.status_code == 200
    assert response.json()["status"] == "en_atencion"
    assert state.alerts.get(created.id).acknowledged_by == CAREGIVER_UID


def test_acknowledge_adult_own_alert_403(state, client):
    created = _create(state)
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(_ack_path(created.id))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE


def test_acknowledge_twice_idempotent(state, client):
    _link_family(state)
    _link_caregiver(state)
    created = _create(state)
    _as_user(Role.FAMILIAR, FAMILY_UID)
    first = client.post(_ack_path(created.id))
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    second = client.post(_ack_path(created.id))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["status"] == "en_atencion"
    # Quien la atendió primero se conserva.
    assert state.alerts.get(created.id).acknowledged_by == FAMILY_UID


def test_acknowledge_closed_422(state, client):
    _link_family(state)
    created = _create(state)
    update_alert_status(
        _user(Role.FAMILIAR, FAMILY_UID),
        created.id,
        AlertStatus.ATENDIDA,
        state.alerts,
        state.users,
    )
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.post(_ack_path(created.id))

    assert response.status_code == 422
    assert response.json() == {"detail": ALERT_CLOSED_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.ATENDIDA


def test_acknowledge_unknown_alert_404(state, client):
    _link_family(state)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.post(_ack_path("missing-alert"))

    assert response.status_code == 404
    assert response.json() == {"detail": ALERT_NOT_FOUND_DETAIL}


def test_acknowledge_service_errors(state):
    _link_family(state)
    created = _create(state)
    family = _user(Role.FAMILIAR, FAMILY_UID)

    with pytest.raises(AlertPermissionDeniedError):
        acknowledge_alert(_user(Role.ADULTO_MAYOR), created.id, state.alerts, state.users)
    with pytest.raises(AlertNotFoundError):
        acknowledge_alert(
            _user(Role.FAMILIAR, OTHER_FAMILY_UID), created.id, state.alerts, state.users
        )
    result = acknowledge_alert(family, created.id, state.alerts, state.users)
    assert isinstance(result, AlertDetail)
    assert result.status == AlertStatus.EN_ATENCION
    update_alert_status(family, created.id, AlertStatus.ATENDIDA, state.alerts, state.users)
    with pytest.raises(AlertAlreadyClosedError):
        acknowledge_alert(family, created.id, state.alerts, state.users)


# --- H5: actualizar estado ---


def test_update_status_atendida(state, client):
    _link_family(state)
    created = _create(state)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.patch(_alert_path(created.id), json={"status": "atendida"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == DETAIL_KEYS
    assert body["status"] == "atendida"
    assert state.alerts.get(created.id).status == AlertStatus.ATENDIDA


def test_update_status_accepts_any_status_value(state, client):
    _link_caregiver(state)
    created = _create(state)
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    to_en_atencion = client.patch(_alert_path(created.id), json={"status": "en_atencion"})
    to_atendida = client.patch(_alert_path(created.id), json={"status": "atendida"})
    back_to_pendiente = client.patch(_alert_path(created.id), json={"status": "pendiente"})

    assert to_en_atencion.status_code == 200
    assert to_en_atencion.json()["status"] == "en_atencion"
    assert to_atendida.json()["status"] == "atendida"
    assert back_to_pendiente.status_code == 200
    assert back_to_pendiente.json()["status"] == "pendiente"
    record = state.alerts.get(created.id)
    assert record.status == AlertStatus.PENDIENTE
    assert record.acknowledged_by == CAREGIVER_UID


def test_update_status_ignores_adult_id(state, client):
    _link_family(state)
    created = _create(state)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.patch(
        _alert_path(created.id),
        params={"adult_id": OTHER_ADULT_UID},
        json={"status": "atendida", "adult_id": OTHER_ADULT_UID},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "atendida"


def test_update_status_caregiver_without_receive_alerts_403(state, client):
    _link_caregiver(state, permissions=NO_RECEIVE_ALERTS)
    created = _create(state)
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.patch(_alert_path(created.id), json={"status": "atendida"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE


def test_update_status_adult_403(state, client):
    created = _create(state)
    _as_user(Role.ADULTO_MAYOR)

    response = client.patch(_alert_path(created.id), json={"status": "atendida"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE


def test_update_status_unknown_alert_404(state, client):
    _link_family(state)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.patch(_alert_path("missing-alert"), json={"status": "atendida"})

    assert response.status_code == 404
    assert response.json() == {"detail": ALERT_NOT_FOUND_DETAIL}


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"status": "cerrada"}, "Datos inválidos: status: no es válido"),
        ({"status": None}, "Datos inválidos: status: no es válido"),
        ({}, "Datos inválidos: status: es obligatorio"),
    ],
)
def test_update_invalid_status_422(state, client, payload, detail):
    _link_family(state)
    created = _create(state)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.patch(_alert_path(created.id), json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE


# --- Destinatarios ---


def test_alert_recipients_dedupes_and_excludes_adult(state):
    # El cuidador también es contacto de confianza verificado: aparece una sola vez.
    _link_caregiver(state, CAREGIVER_UID, RECEIVE_ALERTS)
    _link_caregiver(state, MUTED_CAREGIVER_UID, NO_RECEIVE_ALERTS, token=None)
    state.contacts.add(ADULT_UID, _contact("c-caregiver", email="cuidador@example.com"))
    state.users.mark_email_verified(CAREGIVER_UID, "cuidador@example.com")
    # Un contacto con el correo verificado del propio adulto no lo vuelve destinatario.
    state.contacts.add(ADULT_UID, _contact("c-self", email="rosa@example.com"))
    state.users.mark_email_verified(ADULT_UID, "rosa@example.com")
    state.contacts.add(ADULT_UID, _contact("c-trusted", email=CONTACT_EMAIL))
    state.users.mark_email_verified(CONTACT_UID, CONTACT_EMAIL)
    state.contacts.add(
        ADULT_UID, _contact("c-plain", email="otro@example.com", help_alerts=False)
    )
    state.users.mark_email_verified(OTHER_CONTACT_UID, "otro@example.com")
    created = _create(state)

    recipients = alert_recipients(state.alerts.get(created.id), state.contacts, state.users)

    assert recipients == [CAREGIVER_UID, CONTACT_UID]


# --- Escalamiento (RF-24) ---


def test_escalation_resends_after_interval_and_includes_family(state):
    _link_family(state)
    _link_caregiver(state)
    state.contacts.add(ADULT_UID, _contact(email="nadie@example.com"))
    created = _create(state, now=T0)
    assert state.push.tokens == [[TOKEN_CAREGIVER]]

    assert _escalate(state, T0 + timedelta(seconds=ESCALATION_SECONDS - 1)) == 0
    assert len(state.push.calls) == 1
    assert state.alerts.get(created.id).notification_round == 0

    assert _escalate(state, T0 + timedelta(seconds=ESCALATION_SECONDS)) == 1

    # Una sola push con los destinatarios más el familiar responsable (los tokens
    # salen en el orden en que se registraron los dispositivos: tokens_of).
    assert state.push.calls[1] == {
        "tokens": [TOKEN_FAMILY, TOKEN_CAREGIVER],
        "title": ALERT_PUSH_TITLE,
        "body": BODY_UNAVAILABLE,
        "data": _expected_data(created.id),
    }
    record = state.alerts.get(created.id)
    assert record.notification_round == 1
    assert record.last_notified_at == T0 + timedelta(seconds=ESCALATION_SECONDS)
    assert record.status == AlertStatus.PENDIENTE

    # El intervalo se cuenta desde el último envío.
    assert _escalate(state, T0 + timedelta(seconds=ESCALATION_SECONDS + 1)) == 0
    assert len(state.push.calls) == 2


def test_escalation_stops_after_max_rounds(state):
    _link_family(state)
    created = _create(state, now=T0)
    assert state.push.tokens == [[TOKEN_FAMILY]]

    for round_number in range(1, MAX_ESCALATION_ROUNDS + 1):
        now = T0 + timedelta(seconds=ESCALATION_SECONDS * round_number)
        assert _escalate(state, now) == 1
        assert state.alerts.get(created.id).notification_round == round_number

    assert MAX_ESCALATION_ROUNDS == 3
    assert _escalate(state, T0 + timedelta(seconds=ESCALATION_SECONDS * 4)) == 0
    assert _escalate(state, T0 + timedelta(hours=10)) == 0
    assert state.alerts.get(created.id).notification_round == MAX_ESCALATION_ROUNDS
    assert len(state.push.calls) == 1 + MAX_ESCALATION_ROUNDS
    assert all(tokens == [TOKEN_FAMILY] for tokens in state.push.tokens)


def test_escalation_does_not_revert_acknowledged_alert(state):
    _link_family(state)
    family = _user(Role.FAMILIAR, FAMILY_UID)
    acknowledged = _create(state, now=T0)
    closed = _create(state, now=T0)
    acknowledge_alert(family, acknowledged.id, state.alerts, state.users)
    update_alert_status(family, closed.id, AlertStatus.ATENDIDA, state.alerts, state.users)
    pushes_before = len(state.push.calls)

    assert _escalate(state, T0 + timedelta(hours=1)) == 0

    assert state.alerts.get(acknowledged.id).status == AlertStatus.EN_ATENCION
    assert state.alerts.get(acknowledged.id).notification_round == 0
    assert state.alerts.get(acknowledged.id).acknowledged_by == FAMILY_UID
    assert state.alerts.get(closed.id).status == AlertStatus.ATENDIDA
    assert state.alerts.get(closed.id).notification_round == 0
    assert len(state.push.calls) == pushes_before


def test_escalation_skips_alert_attended_after_it_was_listed(state):
    class StaleAlertRepository(InMemoryAlertRepository):
        """list_pending devuelve una foto vieja: simula que alguien la atendió después."""

        def __init__(self) -> None:
            super().__init__()
            self.snapshot: list[AlertRecord] = []

        def list_pending(self) -> list[AlertRecord]:
            return list(self.snapshot)

    stale = StaleAlertRepository()
    state.alerts = stale
    _link_family(state)
    created = _create(state, now=T0)
    stale.snapshot = stale.list_for_adult(ADULT_UID)
    acknowledge_alert(
        _user(Role.FAMILIAR, FAMILY_UID), created.id, state.alerts, state.users
    )
    pushes_before = len(state.push.calls)

    assert _escalate(state, T0 + timedelta(hours=1)) == 0

    record = stale.get(created.id)
    assert record.status == AlertStatus.EN_ATENCION
    assert record.notification_round == 0
    assert len(state.push.calls) == pushes_before


def test_escalation_push_failure_still_counts_round(state):
    _link_family(state)
    created = _create(state, now=T0)
    state.push.error = FcmUnavailableError("El servicio de notificaciones no está disponible.")

    assert _escalate(state, T0 + timedelta(seconds=ESCALATION_SECONDS)) == 1

    assert state.alerts.get(created.id).notification_round == 1


def test_escalation_tick_uses_default_providers(monkeypatch):
    captured: list[inspect.BoundArguments] = []
    signature = inspect.signature(alert_service.escalate_pending_alerts)

    def fake_escalate(*args, **kwargs):
        captured.append(signature.bind(*args, **kwargs))
        return 0

    monkeypatch.setattr(alerts_router, "escalate_pending_alerts", fake_escalate)

    alerts_router.alert_escalation_tick()

    assert len(captured) == 1
    arguments = captured[0].arguments
    assert arguments["alerts"] is get_alert_repository()
    assert arguments["contacts"] is get_contact_repository()
    assert arguments["users"] is get_user_repository()
    assert arguments["devices"] is get_device_repository()
    assert arguments["escalation_seconds"] == get_settings().alert_escalation_seconds
    assert "now" not in arguments
    assert registered_ticks()["alert_escalation"] is alerts_router.alert_escalation_tick


# --- Repositorio ---


def _record(alert_id: str, status: AlertStatus, adult_uid: str = ADULT_UID) -> AlertRecord:
    return AlertRecord(
        id=alert_id,
        adult_uid=adult_uid,
        adult_name=ADULT_NAME,
        category=None,
        status=status,
        created_at=T0,
        latitude=None,
        longitude=None,
        has_trusted_contacts=False,
        last_notified_at=T0,
    )


def test_update_if_status_ignores_non_pending():
    repository = InMemoryAlertRepository()
    repository.add(_record("alert-1", AlertStatus.EN_ATENCION))
    repository.add(_record("alert-2", AlertStatus.PENDIENTE))

    ignored = repository.update_if_status(
        replace(_record("alert-1", AlertStatus.PENDIENTE), notification_round=1),
        AlertStatus.PENDIENTE,
    )
    applied = repository.update_if_status(
        replace(_record("alert-2", AlertStatus.PENDIENTE), notification_round=1),
        AlertStatus.PENDIENTE,
    )
    missing = repository.update_if_status(
        _record("missing", AlertStatus.PENDIENTE), AlertStatus.PENDIENTE
    )

    assert ignored is None
    stored = repository.get("alert-1")
    assert stored.status == AlertStatus.EN_ATENCION
    assert stored.notification_round == 0
    assert applied is not None
    assert applied.notification_round == 1
    assert repository.get("alert-2").notification_round == 1
    assert missing is None
    assert repository.get("missing") is None


def test_alert_repository_returns_copies_and_lists_pending():
    repository = InMemoryAlertRepository()
    added = repository.add(_record("alert-1", AlertStatus.PENDIENTE))
    repository.add(_record("alert-2", AlertStatus.ATENDIDA))
    repository.add(_record("alert-3", AlertStatus.PENDIENTE, adult_uid=OTHER_ADULT_UID))

    added.status = AlertStatus.ATENDIDA
    fetched = repository.get("alert-1")
    fetched.notification_round = 99

    assert repository.get("alert-1").status == AlertStatus.PENDIENTE
    assert repository.get("alert-1").notification_round == 0
    assert [record.id for record in repository.list_pending()] == ["alert-1", "alert-3"]
    assert repository.update(_record("missing", AlertStatus.PENDIENTE)) is None
    assert [record.id for record in repository.list_for_adult(OTHER_ADULT_UID)] == [
        "alert-3"
    ]


# --- Roles y autenticación en todas las rutas ---


ROUTES = [
    pytest.param("post", ALERTS_PATH, None, False, id="H1"),
    pytest.param("get", ALERTS_PATH, None, False, id="H2"),
    pytest.param("get", ALERT_PATH_TEMPLATE, None, True, id="H3"),
    pytest.param("post", ACK_PATH_TEMPLATE, None, True, id="H4"),
    pytest.param("patch", ALERT_PATH_TEMPLATE, {"status": "atendida"}, True, id="H5"),
]


def _route_request(client, method, template, payload, with_id, alert_id):
    path = template.format(alert_id=alert_id) if with_id else template
    params = {"adult_id": ADULT_UID} if method == "get" and not with_id else None
    return client.request(method, path, json=payload, params=params)


@pytest.mark.parametrize(("method", "template", "payload", "with_id"), ROUTES)
def test_alert_routes_admin_403(state, client, method, template, payload, with_id):
    created = _create(state)
    # Vinculado a propósito: el 403 no depende del vínculo.
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)
    _as_user(Role.ADMINISTRADOR, ADMIN_UID)

    response = _route_request(client, method, template, payload, with_id, created.id)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE
    assert len(state.alerts.list_for_adult(ADULT_UID)) == 1


@pytest.mark.parametrize(("method", "template", "payload", "with_id"), ROUTES)
def test_alert_routes_role_none_403(state, client, method, template, payload, with_id):
    created = _create(state)
    state.users.link_adult(NO_ROLE_UID, ADULT_UID, Role.FAMILIAR)
    _as_user(None, NO_ROLE_UID)

    response = _route_request(client, method, template, payload, with_id, created.id)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE
    assert len(state.alerts.list_for_adult(ADULT_UID)) == 1


@pytest.mark.parametrize(("method", "template", "payload", "with_id"), ROUTES)
def test_routes_require_bearer_401(state, client, method, template, payload, with_id):
    created = _create(state)

    response = _route_request(client, method, template, payload, with_id, created.id)

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    assert state.alerts.get(created.id).status == AlertStatus.PENDIENTE
