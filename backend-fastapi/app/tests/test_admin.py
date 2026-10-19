import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as firebase_auth
from firebase_admin.exceptions import FirebaseError

from app.core.firebase import FirebaseConfigurationError
from app.core.security import AUTH_UNAVAILABLE_DETAIL, FORBIDDEN_DETAIL, get_current_user
from app.main import app
from app.models.admin import AdminUserResponse, AdminUserUpdateRequest
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.services import admin_service
from app.services.admin_service import (
    AdminUserNotFoundError,
    SelfDisableError,
    list_users,
    set_user_disabled,
)
from app.services.auth_service import AuthProviderUnavailableError


USERS_PATH = "/api/admin/users"
USER_PATH_TEMPLATE = "/api/admin/users/{uid}"
ADMIN_UID = "admin-uid"
FAMILY_UID = "family-uid"
CAREGIVER_UID = "caregiver-uid"
ADULT_UID = "adult-uid"
BEARER_DETAIL = "Debes enviar un token Bearer."
USER_NOT_FOUND_DETAIL = "El usuario no existe."
SELF_DISABLE_DETAIL = "No puedes deshabilitar tu propia cuenta."
LEAKED_MESSAGE = "detalle-interno-de-firebase projects/secret-project"
USER_KEYS = {"uid", "email", "display_name", "role", "disabled"}
FAKE_APP = object()


def make_record(
    uid: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
    claims: object = None,
    disabled: bool = False,
) -> SimpleNamespace:
    """Sustituto mínimo de firebase_admin.auth.UserRecord / ExportedUserRecord."""
    return SimpleNamespace(
        uid=uid,
        email=email,
        display_name=display_name,
        custom_claims=claims,
        disabled=disabled,
    )


class FakePage:
    def __init__(self, fake: "FakeFirebaseAuth", index: int) -> None:
        self._fake = fake
        self._index = index
        self.users = list(fake.pages[index])

    def get_next_page(self) -> "FakePage | None":
        self._fake.next_page_calls += 1
        if self._fake.next_page_error is not None:
            raise self._fake.next_page_error
        if self._index + 1 < len(self._fake.pages):
            return FakePage(self._fake, self._index + 1)
        return None


class FakeFirebaseAuth:
    """Sustituye list_users, update_user, revoke_refresh_tokens y get_firebase_app.

    Nunca hace red y registra cada llamada.
    """

    def __init__(self) -> None:
        self.pages: list[list[SimpleNamespace]] = [[]]
        self.records: dict[str, SimpleNamespace] = {}
        self.app_error: Exception | None = None
        self.list_error: Exception | None = None
        self.next_page_error: Exception | None = None
        self.update_error: Exception | None = None
        self.revoke_error: Exception | None = None
        self.app_calls = 0
        self.next_page_calls = 0
        self.list_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []
        self.revoke_calls: list[tuple[str, object]] = []

    def get_firebase_app(self) -> object:
        self.app_calls += 1
        if self.app_error is not None:
            raise self.app_error
        return FAKE_APP

    def list_users(self, page_token=None, max_results=1000, app=None) -> FakePage:
        self.list_calls.append({"page_token": page_token, "app": app})
        if self.list_error is not None:
            raise self.list_error
        return FakePage(self, 0)

    def update_user(self, uid: str, **kwargs) -> SimpleNamespace:
        self.update_calls.append((uid, kwargs))
        if self.update_error is not None:
            raise self.update_error
        record = self.records.get(uid)
        if record is None:
            raise firebase_auth.UserNotFoundError(
                "No user record found for the given identifier (USER_NOT_FOUND)."
            )
        record.disabled = kwargs["disabled"]
        return make_record(
            record.uid,
            email=record.email,
            display_name=record.display_name,
            claims=record.custom_claims,
            disabled=record.disabled,
        )

    def revoke_refresh_tokens(self, uid: str, app=None) -> None:
        self.revoke_calls.append((uid, app))
        if self.revoke_error is not None:
            raise self.revoke_error

    @property
    def firebase_calls(self) -> int:
        return (
            self.app_calls
            + len(self.list_calls)
            + len(self.update_calls)
            + len(self.revoke_calls)
        )


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    fake_auth = FakeFirebaseAuth()
    # Nunca el Admin SDK real: si existiera la cuenta de servicio, tocaría usuarios reales.
    monkeypatch.setattr(admin_service, "get_firebase_app", fake_auth.get_firebase_app)
    monkeypatch.setattr(admin_service.firebase_auth, "list_users", fake_auth.list_users)
    monkeypatch.setattr(admin_service.firebase_auth, "update_user", fake_auth.update_user)
    monkeypatch.setattr(
        admin_service.firebase_auth,
        "revoke_refresh_tokens",
        fake_auth.revoke_refresh_tokens,
    )
    try:
        yield fake_auth
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def as_user(role: Role | None, uid: str = ADMIN_UID) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        uid=uid,
        email=f"{uid}@example.com",
        email_verified=True,
        display_name="Persona de prueba",
        role=role,
    )


def add_user(fake: FakeFirebaseAuth, record: SimpleNamespace) -> SimpleNamespace:
    fake.records[record.uid] = record
    return record


# --- B1: GET /api/admin/users ---


def test_b1_lists_users_with_validated_role_and_disabled(fake, client):
    fake.pages = [
        [
            make_record(
                ADMIN_UID,
                email="admin@example.com",
                display_name="Ana Admin",
                claims={"role": "administrador"},
            ),
            make_record(
                FAMILY_UID,
                email="familia@example.com",
                display_name="Luis Pérez",
                claims={"role": "familiar", "otro": 1},
                disabled=True,
            ),
            make_record(
                CAREGIVER_UID,
                email="cuidador@example.com",
                claims={"role": "cuidador"},
            ),
            make_record(ADULT_UID, display_name="Rosa Gómez", claims={"role": "adulto_mayor"}),
            make_record("no-claims-uid", email="sin@example.com", claims=None),
            make_record("other-claims-uid", claims={"plan": "basic"}),
        ]
    ]
    as_user(Role.ADMINISTRADOR)

    response = client.get(USERS_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "uid": ADMIN_UID,
            "email": "admin@example.com",
            "display_name": "Ana Admin",
            "role": "administrador",
            "disabled": False,
        },
        {
            "uid": FAMILY_UID,
            "email": "familia@example.com",
            "display_name": "Luis Pérez",
            "role": "familiar",
            "disabled": True,
        },
        {
            "uid": CAREGIVER_UID,
            "email": "cuidador@example.com",
            "display_name": None,
            "role": "cuidador",
            "disabled": False,
        },
        {
            "uid": ADULT_UID,
            "email": None,
            "display_name": "Rosa Gómez",
            "role": "adulto_mayor",
            "disabled": False,
        },
        {
            "uid": "no-claims-uid",
            "email": "sin@example.com",
            "display_name": None,
            "role": None,
            "disabled": False,
        },
        {
            "uid": "other-claims-uid",
            "email": None,
            "display_name": None,
            "role": None,
            "disabled": False,
        },
    ]
    assert all(set(item) == USER_KEYS for item in body)
    assert fake.list_calls == [{"page_token": None, "app": FAKE_APP}]
    assert fake.update_calls == []
    assert fake.revoke_calls == []


def test_b1_paginates_two_pages(fake, client):
    fake.pages = [
        [make_record("uid-1", claims={"role": "familiar"}), make_record("uid-2")],
        [make_record("uid-3", claims={"role": "cuidador"}, disabled=True)],
    ]
    as_user(Role.ADMINISTRADOR)

    response = client.get(USERS_PATH)

    assert response.status_code == 200
    assert [item["uid"] for item in response.json()] == ["uid-1", "uid-2", "uid-3"]
    assert [item["role"] for item in response.json()] == ["familiar", None, "cuidador"]
    assert [item["disabled"] for item in response.json()] == [False, False, True]
    assert len(fake.list_calls) == 1
    assert fake.next_page_calls == 2


def test_b1_empty_project_returns_empty_list(fake, client):
    as_user(Role.ADMINISTRADOR)

    response = client.get(USERS_PATH)

    assert response.status_code == 200
    assert response.json() == []


def test_b1_invalid_claim_role_null_and_log_without_value(fake, client, caplog):
    caplog.set_level(logging.WARNING, logger="app.services.auth_service")
    fake.pages = [
        [
            make_record("bad-role-uid", claims={"role": "superadmin"}),
            make_record("numeric-role-uid", claims={"role": 7}),
            make_record("odd-claims-uid", claims="role=administrador"),
        ]
    ]
    as_user(Role.ADMINISTRADOR)

    response = client.get(USERS_PATH)

    assert response.status_code == 200
    assert [item["role"] for item in response.json()] == [None, None, None]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.services.auth_service"
    ]
    assert any("bad-role-uid" in message for message in warnings)
    assert any("numeric-role-uid" in message for message in warnings)
    assert "superadmin" not in caplog.text


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR, Role.ADULTO_MAYOR])
def test_b1_non_admin_403(fake, client, role):
    as_user(role, uid=f"{role}-uid")

    response = client.get(USERS_PATH)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert fake.firebase_calls == 0


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", USERS_PATH, None),
        ("PATCH", USER_PATH_TEMPLATE.format(uid=FAMILY_UID), {"disabled": True}),
    ],
)
def test_b1_and_b2_user_without_role_403(fake, client, method, path, body):
    add_user(fake, make_record(FAMILY_UID))
    as_user(None, uid="no-role-uid")

    response = client.request(method, path, json=body)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert fake.firebase_calls == 0


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", USERS_PATH, None),
        ("PATCH", USER_PATH_TEMPLATE.format(uid=FAMILY_UID), {"disabled": True}),
    ],
)
def test_b1_and_b2_require_bearer_401(fake, client, method, path, body):
    add_user(fake, make_record(FAMILY_UID))

    response = client.request(method, path, json=body)

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    assert fake.firebase_calls == 0


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", USERS_PATH, None),
        ("PATCH", USER_PATH_TEMPLATE.format(uid=FAMILY_UID), {"disabled": True}),
    ],
)
def test_b1_and_b2_firebase_not_configured_503(fake, client, caplog, method, path, body):
    caplog.set_level(logging.ERROR, logger="app.api.admin_router")
    add_user(fake, make_record(FAMILY_UID))
    fake.app_error = FirebaseConfigurationError(
        "No se encontró el archivo de credenciales de Firebase."
    )
    as_user(Role.ADMINISTRADOR)

    response = client.request(method, path, json=body)

    assert response.status_code == 503
    assert response.json() == {"detail": AUTH_UNAVAILABLE_DETAIL}
    assert fake.list_calls == []
    assert fake.update_calls == []
    assert fake.revoke_calls == []
    assert any(
        record.name == "app.api.admin_router"
        and "FirebaseConfigurationError" in record.getMessage()
        for record in caplog.records
    )


def test_b1_firebase_error_503_without_leaking_message(fake, client, caplog):
    caplog.set_level(logging.DEBUG)
    fake.list_error = FirebaseError("UNKNOWN", LEAKED_MESSAGE)
    as_user(Role.ADMINISTRADOR)

    response = client.get(USERS_PATH)

    assert response.status_code == 503
    assert response.json() == {"detail": AUTH_UNAVAILABLE_DETAIL}
    assert LEAKED_MESSAGE not in response.text
    assert LEAKED_MESSAGE not in caplog.text
    assert any(
        record.name == "app.api.admin_router"
        and "AuthProviderUnavailableError" in record.getMessage()
        for record in caplog.records
    )


def test_b1_next_page_failure_503(fake, client):
    fake.pages = [[make_record("uid-1")], [make_record("uid-2")]]
    fake.next_page_error = FirebaseError("UNAVAILABLE", LEAKED_MESSAGE)
    as_user(Role.ADMINISTRADOR)

    response = client.get(USERS_PATH)

    assert response.status_code == 503
    assert response.json() == {"detail": AUTH_UNAVAILABLE_DETAIL}


# --- B2: PATCH /api/admin/users/{uid} ---


def test_b2_disables_and_revokes(fake, client, caplog):
    caplog.set_level(logging.INFO, logger="app.services.admin_service")
    add_user(
        fake,
        make_record(
            FAMILY_UID,
            email="familia@example.com",
            display_name="Luis Pérez",
            claims={"role": "familiar"},
        ),
    )
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid=FAMILY_UID), json={"disabled": True}
    )

    assert response.status_code == 200
    assert response.json() == {
        "uid": FAMILY_UID,
        "email": "familia@example.com",
        "display_name": "Luis Pérez",
        "role": "familiar",
        "disabled": True,
    }
    assert fake.update_calls == [(FAMILY_UID, {"disabled": True, "app": FAKE_APP})]
    assert fake.revoke_calls == [(FAMILY_UID, FAKE_APP)]
    assert fake.records[FAMILY_UID].disabled is True
    assert any(
        FAMILY_UID in record.getMessage() and ADMIN_UID in record.getMessage()
        for record in caplog.records
        if record.name == "app.services.admin_service"
    )


def test_b2_enables_without_revoke(fake, client):
    add_user(
        fake,
        make_record(CAREGIVER_UID, claims={"role": "cuidador"}, disabled=True),
    )
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid=CAREGIVER_UID), json={"disabled": False}
    )

    assert response.status_code == 200
    assert response.json() == {
        "uid": CAREGIVER_UID,
        "email": None,
        "display_name": None,
        "role": "cuidador",
        "disabled": False,
    }
    assert fake.update_calls == [(CAREGIVER_UID, {"disabled": False, "app": FAKE_APP})]
    assert fake.revoke_calls == []


def test_b2_response_role_null_when_claim_invalid(fake, client):
    add_user(fake, make_record(FAMILY_UID, claims={"role": "superadmin"}))
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid=FAMILY_UID), json={"disabled": True}
    )

    assert response.status_code == 200
    assert response.json()["role"] is None
    assert set(response.json()) == USER_KEYS


def test_b2_unknown_uid_404(fake, client):
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid="missing-uid"), json={"disabled": True}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": USER_NOT_FOUND_DETAIL}
    assert fake.update_calls == [("missing-uid", {"disabled": True, "app": FAKE_APP})]
    assert fake.revoke_calls == []


def test_b2_malformed_uid_404(fake, client):
    fake.update_error = ValueError("Invalid uid: \"" + "x" * 129 + "\".")
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid="x" * 129), json={"disabled": True}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": USER_NOT_FOUND_DETAIL}
    assert fake.revoke_calls == []


def test_b2_self_disable_422_does_not_call_firebase(fake, client):
    add_user(fake, make_record(ADMIN_UID, claims={"role": "administrador"}))
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid=ADMIN_UID), json={"disabled": True}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": SELF_DISABLE_DETAIL}
    assert fake.firebase_calls == 0
    assert fake.records[ADMIN_UID].disabled is False


def test_b2_self_enable_allowed(fake, client):
    add_user(fake, make_record(ADMIN_UID, claims={"role": "administrador"}))
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid=ADMIN_UID), json={"disabled": False}
    )

    assert response.status_code == 200
    assert response.json() == {
        "uid": ADMIN_UID,
        "email": None,
        "display_name": None,
        "role": "administrador",
        "disabled": False,
    }
    assert fake.update_calls == [(ADMIN_UID, {"disabled": False, "app": FAKE_APP})]
    assert fake.revoke_calls == []


@pytest.mark.parametrize(
    ("body", "expected_detail"),
    [
        ({}, "Datos inválidos: disabled: es obligatorio"),
        ({"otro": True}, "Datos inválidos: disabled: es obligatorio"),
        ({"disabled": None}, "Datos inválidos: disabled: no es válido"),
        ({"disabled": "quizas"}, "Datos inválidos: disabled: no es válido"),
    ],
)
def test_b2_body_without_disabled_422(fake, client, body, expected_detail):
    add_user(fake, make_record(FAMILY_UID))
    as_user(Role.ADMINISTRADOR)

    response = client.patch(USER_PATH_TEMPLATE.format(uid=FAMILY_UID), json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": expected_detail}
    assert fake.firebase_calls == 0


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR, Role.ADULTO_MAYOR])
def test_b2_non_admin_roles_403(fake, client, role):
    add_user(fake, make_record(FAMILY_UID))
    as_user(role, uid=f"{role}-uid")

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid=FAMILY_UID), json={"disabled": True}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert fake.firebase_calls == 0
    assert fake.records[FAMILY_UID].disabled is False


def test_b2_revoke_failure_503(fake, client, caplog):
    caplog.set_level(logging.DEBUG)
    add_user(fake, make_record(FAMILY_UID, claims={"role": "familiar"}))
    fake.revoke_error = FirebaseError("INTERNAL", LEAKED_MESSAGE)
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid=FAMILY_UID), json={"disabled": True}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": AUTH_UNAVAILABLE_DETAIL}
    assert fake.revoke_calls == [(FAMILY_UID, FAKE_APP)]
    assert LEAKED_MESSAGE not in caplog.text


def test_b2_update_firebase_error_503(fake, client):
    add_user(fake, make_record(FAMILY_UID))
    fake.update_error = FirebaseError("UNAVAILABLE", LEAKED_MESSAGE)
    as_user(Role.ADMINISTRADOR)

    response = client.patch(
        USER_PATH_TEMPLATE.format(uid=FAMILY_UID), json={"disabled": True}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": AUTH_UNAVAILABLE_DETAIL}
    assert LEAKED_MESSAGE not in response.text
    assert fake.revoke_calls == []


# --- Servicio ---


def test_service_list_users_value_error_is_unavailable(fake):
    fake.list_error = ValueError(LEAKED_MESSAGE)

    with pytest.raises(AuthProviderUnavailableError) as raised:
        list_users()

    assert LEAKED_MESSAGE not in str(raised.value)


def test_service_list_users_propagates_configuration_error(fake):
    fake.app_error = FirebaseConfigurationError("La ruta de credenciales de Firebase no está configurada.")

    with pytest.raises(FirebaseConfigurationError):
        list_users()

    assert fake.list_calls == []


def test_service_set_user_disabled_returns_response_model(fake):
    add_user(fake, make_record(FAMILY_UID, email="f@example.com", claims={"role": "familiar"}))

    result = set_user_disabled(ADMIN_UID, FAMILY_UID, True)

    assert isinstance(result, AdminUserResponse)
    assert result == AdminUserResponse(
        uid=FAMILY_UID,
        email="f@example.com",
        display_name=None,
        role=Role.FAMILIAR,
        disabled=True,
    )
    assert fake.revoke_calls == [(FAMILY_UID, FAKE_APP)]


def test_service_self_disable_raises_before_firebase(fake):
    with pytest.raises(SelfDisableError) as raised:
        set_user_disabled(ADMIN_UID, ADMIN_UID, True)

    assert str(raised.value) == SELF_DISABLE_DETAIL
    assert fake.firebase_calls == 0


def test_service_unknown_uid_raises_not_found(fake):
    with pytest.raises(AdminUserNotFoundError) as raised:
        set_user_disabled(ADMIN_UID, "missing-uid", False)

    assert str(raised.value) == USER_NOT_FOUND_DETAIL


def test_service_revoke_not_found_raises_not_found(fake):
    add_user(fake, make_record(FAMILY_UID))
    fake.revoke_error = firebase_auth.UserNotFoundError("gone")

    with pytest.raises(AdminUserNotFoundError):
        set_user_disabled(ADMIN_UID, FAMILY_UID, True)


def test_update_request_ignores_extra_fields():
    payload = AdminUserUpdateRequest.model_validate({"disabled": True, "role": "administrador"})

    assert payload.model_dump() == {"disabled": True}


def test_admin_routes_in_openapi():
    paths = app.openapi()["paths"]

    assert set(paths[USERS_PATH]) == {"get"}
    assert set(paths[USER_PATH_TEMPLATE]) == {"patch"}
