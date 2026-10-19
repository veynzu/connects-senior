import json
import logging
from collections.abc import Callable
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as firebase_auth
from firebase_admin import exceptions as firebase_exceptions
from sqlalchemy.exc import OperationalError

from app.api import auth_router
from app.core import security
from app.core.firebase import FirebaseConfigurationError
from app.core.security import FORBIDDEN_DETAIL, get_current_user
from app.main import app
from app.models.auth import (
    AdultCreateRequest,
    AuthenticatedUser,
    CaregiverCreateRequest,
    DeviceRegisterRequest,
    LoginRequest,
    LoginResponse,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
)
from app.models.enums import Role
from app.models.users import AdultLink, CaregiverPermissions, UserProfile
from app.repositories.device_repository import (
    InMemoryDeviceRepository,
    get_device_repository,
)
from app.repositories.user_repository import (
    InMemoryUserRepository,
    get_user_repository,
)
from app.services import auth_service


SIGN_IN_ROUTE = ("identitytoolkit.googleapis.com", "/v1/accounts:signInWithPassword")
LOOKUP_ROUTE = ("identitytoolkit.googleapis.com", "/v1/accounts:lookup")
REFRESH_ROUTE = ("securetoken.googleapis.com", "/v1/token")

Handler = Callable[[httpx.Request], httpx.Response]


def _firebase_transport(
    routes: dict[tuple[str, str], Handler],
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        route = (request.url.host, request.url.path)
        if route not in routes:
            raise AssertionError(f"Petición inesperada a {route}")
        if seen is not None:
            seen.append(request)
        return routes[route](request)

    return httpx.MockTransport(handler)


def _sign_in_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "idToken": "id-token",
            "refreshToken": "refresh-token",
            "expiresIn": "3600",
            "localId": "firebase-uid",
            "email": "user@example.com",
            "displayName": "Test User",
        },
    )


def _lookup_handler(
    custom_attributes: str | None = None,
    local_id: str = "firebase-uid",
    email_verified: bool | None = True,
) -> Handler:
    """accounts:lookup simulado; email_verified=None omite emailVerified."""

    def handler(request: httpx.Request) -> httpx.Response:
        user = {
            "localId": local_id,
            "email": "user@example.com",
            "displayName": "Test User",
            "photoUrl": "https://example.com/avatar.png",
        }
        if email_verified is not None:
            user["emailVerified"] = email_verified
        if custom_attributes is not None:
            user["customAttributes"] = custom_attributes
        return httpx.Response(200, json={"users": [user]})

    return handler


def _firebase_error(message: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": message,
                    "status": "INVALID_ARGUMENT",
                }
            },
        )

    return handler


@pytest.fixture
def web_api_key(monkeypatch):
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(firebase_web_api_key="test-api-key"),
    )


async def _sign_in_with_routes(
    routes: dict[tuple[str, str], Handler],
) -> LoginResponse:
    async with httpx.AsyncClient(transport=_firebase_transport(routes)) as client:
        return await auth_service.sign_in_with_email_password(
            LoginRequest(email="user@example.com", password="secret123"),
            client=client,
        )


async def _refresh_with_routes(
    routes: dict[tuple[str, str], Handler],
) -> LoginResponse:
    async with httpx.AsyncClient(transport=_firebase_transport(routes)) as client:
        return await auth_service.refresh_session(
            RefreshRequest(refresh_token="old-refresh-token"),
            client=client,
        )


def _login_response(role: Role | None = None) -> LoginResponse:
    return LoginResponse(
        id_token="id-token",
        refresh_token="refresh-token",
        expires_in=3600,
        user=AuthenticatedUser(uid="firebase-uid", email="user@example.com", role=role),
    )


@pytest.mark.asyncio
async def test_sign_in_with_email_password_returns_firebase_tokens(monkeypatch):
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(firebase_web_api_key="test-api-key"),
    )

    def sign_in_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "test-api-key"
        assert json.loads(request.content) == {
            "email": "user@example.com",
            "password": "secret123",
            "returnSecureToken": True,
        }
        return _sign_in_response(request)

    async with httpx.AsyncClient(
        transport=_firebase_transport(
            {SIGN_IN_ROUTE: sign_in_handler, LOOKUP_ROUTE: _lookup_handler()}
        )
    ) as client:
        response = await auth_service.sign_in_with_email_password(
            LoginRequest(email="user@example.com", password="secret123"),
            client=client,
        )

    assert response.id_token == "id-token"
    assert response.refresh_token == "refresh-token"
    assert response.expires_in == 3600
    assert response.user.uid == "firebase-uid"
    assert response.user.role is None


@pytest.mark.asyncio
async def test_sign_in_hides_invalid_credential_details(monkeypatch):
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(firebase_web_api_key="test-api-key"),
    )

    def firebase_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "INVALID_LOGIN_CREDENTIALS"}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(firebase_handler)
    ) as client:
        with pytest.raises(
            auth_service.InvalidCredentialsError,
            match="Correo o contraseña incorrectos",
        ):
            await auth_service.sign_in_with_email_password(
                LoginRequest(email="user@example.com", password="incorrect"),
                client=client,
            )


@pytest.mark.asyncio
async def test_sign_in_reports_disabled_email_password_provider(monkeypatch):
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(firebase_web_api_key="test-api-key"),
    )

    def firebase_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "OPERATION_NOT_ALLOWED"}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(firebase_handler)
    ) as client:
        with pytest.raises(auth_service.AuthMethodDisabledError, match="habilitado"):
            await auth_service.sign_in_with_email_password(
                LoginRequest(email="user@example.com", password="secret123"),
                client=client,
            )


def test_verify_firebase_id_token_maps_user_claims(monkeypatch):
    monkeypatch.setattr(auth_service, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth_service.firebase_auth,
        "verify_id_token",
        lambda token, app, check_revoked: {
            "uid": "firebase-uid",
            "email": "user@example.com",
            "email_verified": True,
            "name": "Test User",
            "picture": "https://example.com/avatar.png",
        },
    )

    user = auth_service.verify_firebase_id_token("valid-token")

    assert user == AuthenticatedUser(
        uid="firebase-uid",
        email="user@example.com",
        email_verified=True,
        display_name="Test User",
        photo_url="https://example.com/avatar.png",
    )


def test_login_route_returns_tokens(monkeypatch):
    async def fake_sign_in(payload: LoginRequest, **kwargs) -> LoginResponse:
        return LoginResponse(
            id_token="id-token",
            refresh_token="refresh-token",
            expires_in=3600,
            user=AuthenticatedUser(uid="firebase-uid", email=payload.email),
        )

    monkeypatch.setattr(auth_router, "sign_in_with_email_password", fake_sign_in)

    response = TestClient(app).post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "secret123"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["uid"] == "firebase-uid"


def test_me_route_requires_bearer_token():
    response = TestClient(app).get("/api/auth/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_route_returns_authenticated_user(monkeypatch):
    # Correo verificado: sin él get_current_user responde 403 (D-LL-4).
    monkeypatch.setattr(
        security,
        "verify_firebase_id_token",
        lambda token: AuthenticatedUser(
            uid="firebase-uid", email="user@example.com", email_verified=True
        ),
    )

    response = TestClient(app).get(
        "/api/auth/me",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert response.status_code == 200
    assert response.json()["uid"] == "firebase-uid"


# --- Rol en A1 (accounts:lookup) ---


@pytest.mark.asyncio
async def test_sign_in_reads_role_from_lookup_custom_attributes(web_api_key):
    lookup_requests: list[httpx.Request] = []
    lookup = _lookup_handler('{"role": "familiar"}')

    def lookup_handler(request: httpx.Request) -> httpx.Response:
        lookup_requests.append(request)
        return lookup(request)

    response = await _sign_in_with_routes(
        {SIGN_IN_ROUTE: _sign_in_response, LOOKUP_ROUTE: lookup_handler}
    )

    assert len(lookup_requests) == 1
    assert lookup_requests[0].url.params["key"] == "test-api-key"
    assert json.loads(lookup_requests[0].content) == {"idToken": "id-token"}
    assert response.user.role is Role.FAMILIAR
    assert response.user.email_verified is True
    assert response.user.photo_url == "https://example.com/avatar.png"
    assert response.id_token == "id-token"
    assert response.refresh_token == "refresh-token"
    assert response.expires_in == 3600


@pytest.mark.asyncio
async def test_sign_in_ignores_unknown_role_claim(web_api_key, caplog):
    caplog.set_level(logging.WARNING, logger="app.services.auth_service")

    response = await _sign_in_with_routes(
        {
            SIGN_IN_ROUTE: _sign_in_response,
            LOOKUP_ROUTE: _lookup_handler('{"role": "superadmin"}'),
        }
    )

    assert response.user.role is None
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "app.services.auth_service"
    ]
    assert any("firebase-uid" in message for message in warnings)
    assert "superadmin" not in caplog.text


@pytest.mark.asyncio
async def test_sign_in_ignores_malformed_custom_attributes(web_api_key):
    response = await _sign_in_with_routes(
        {
            SIGN_IN_ROUTE: _sign_in_response,
            LOOKUP_ROUTE: _lookup_handler("no-es-json"),
        }
    )

    assert response.user.role is None
    assert response.user.uid == "firebase-uid"


@pytest.mark.asyncio
async def test_sign_in_rejects_lookup_for_other_uid(web_api_key):
    with pytest.raises(auth_service.AuthProviderUnavailableError):
        await _sign_in_with_routes(
            {
                SIGN_IN_ROUTE: _sign_in_response,
                LOOKUP_ROUTE: _lookup_handler('{"role": "familiar"}', local_id="other-uid"),
            }
        )


@pytest.mark.asyncio
async def test_sign_in_maps_lookup_error_to_unavailable(web_api_key):
    with pytest.raises(auth_service.AuthProviderUnavailableError):
        await _sign_in_with_routes(
            {
                SIGN_IN_ROUTE: _sign_in_response,
                LOOKUP_ROUTE: _firebase_error("INVALID_ID_TOKEN"),
            }
        )


@pytest.mark.asyncio
async def test_sign_in_closes_own_client_after_lookup(web_api_key, monkeypatch):
    real_async_client = httpx.AsyncClient
    created: list[httpx.AsyncClient] = []
    seen: list[httpx.Request] = []
    routes = {
        SIGN_IN_ROUTE: _sign_in_response,
        LOOKUP_ROUTE: _lookup_handler('{"role": "familiar"}'),
    }

    def factory(**kwargs) -> httpx.AsyncClient:
        client = real_async_client(transport=_firebase_transport(routes, seen), **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(auth_service.httpx, "AsyncClient", factory)

    response = await auth_service.sign_in_with_email_password(
        LoginRequest(email="user@example.com", password="secret123")
    )

    assert response.user.role is Role.FAMILIAR
    assert [(request.url.host, request.url.path) for request in seen] == [
        SIGN_IN_ROUTE,
        LOOKUP_ROUTE,
    ]
    assert len(created) == 1
    assert created[0].is_closed is True


# --- A3 refresh (servicio) ---


@pytest.mark.asyncio
async def test_refresh_session_returns_tokens_and_user(web_api_key):
    seen: list[httpx.Request] = []

    def refresh_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "test-api-key"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"],
            "refresh_token": ["old-refresh-token"],
        }
        return httpx.Response(
            200,
            json={
                "id_token": "id-token",
                "refresh_token": "new-refresh-token",
                "expires_in": "3600",
                "user_id": "firebase-uid",
                "token_type": "Bearer",
                "project_id": "demo-project",
                "access_token": "id-token",
            },
        )

    lookup = _lookup_handler('{"role": "cuidador"}')

    def lookup_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "test-api-key"
        assert json.loads(request.content) == {"idToken": "id-token"}
        return lookup(request)

    async with httpx.AsyncClient(
        transport=_firebase_transport(
            {REFRESH_ROUTE: refresh_handler, LOOKUP_ROUTE: lookup_handler},
            seen,
        )
    ) as client:
        response = await auth_service.refresh_session(
            RefreshRequest(refresh_token="old-refresh-token"),
            client=client,
        )

    assert [(request.url.host, request.url.path) for request in seen] == [
        REFRESH_ROUTE,
        LOOKUP_ROUTE,
    ]
    assert response.id_token == "id-token"
    assert response.refresh_token == "new-refresh-token"
    assert response.expires_in == 3600
    assert response.user.uid == "firebase-uid"
    assert response.user.email == "user@example.com"
    assert response.user.role is Role.CUIDADOR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    ["TOKEN_EXPIRED", "INVALID_REFRESH_TOKEN", "USER_NOT_FOUND", "MISSING_REFRESH_TOKEN"],
)
async def test_refresh_session_maps_session_errors_to_invalid_token(
    web_api_key, error_code
):
    with pytest.raises(auth_service.InvalidTokenError) as error_info:
        await _refresh_with_routes({REFRESH_ROUTE: _firebase_error(error_code)})

    assert str(error_info.value) == "La sesión expiró. Inicia sesión de nuevo."


@pytest.mark.asyncio
async def test_refresh_session_maps_user_disabled(web_api_key):
    with pytest.raises(auth_service.UserDisabledError) as error_info:
        await _refresh_with_routes({REFRESH_ROUTE: _firebase_error("USER_DISABLED")})

    assert str(error_info.value) == "La cuenta está deshabilitada."


@pytest.mark.asyncio
async def test_refresh_session_maps_unknown_error_to_unavailable(web_api_key):
    with pytest.raises(auth_service.AuthProviderUnavailableError):
        await _refresh_with_routes({REFRESH_ROUTE: _firebase_error("INVALID_GRANT_TYPE")})


@pytest.mark.asyncio
async def test_refresh_session_rejects_invalid_response(web_api_key):
    def refresh_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "refresh_token": "new-refresh-token",
                "expires_in": "3600",
                "user_id": "firebase-uid",
            },
        )

    with pytest.raises(
        auth_service.AuthProviderUnavailableError,
        match="respuesta de autenticación inválida",
    ):
        await _refresh_with_routes({REFRESH_ROUTE: refresh_handler})


@pytest.mark.asyncio
async def test_refresh_session_requires_web_api_key(monkeypatch):
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(firebase_web_api_key="   "),
    )
    seen: list[httpx.Request] = []

    async with httpx.AsyncClient(
        transport=_firebase_transport({}, seen)
    ) as client:
        with pytest.raises(FirebaseConfigurationError) as error_info:
            await auth_service.refresh_session(
                RefreshRequest(refresh_token="old-refresh-token"),
                client=client,
            )

    assert "FIREBASE_WEB_API_KEY" not in str(error_info.value)
    assert str(error_info.value) == "La clave web de Firebase no está configurada."
    assert seen == []


# --- Rol en A2 (claim del token verificado) ---


@pytest.mark.parametrize(
    ("claim", "expected_role"),
    [("familiar", Role.FAMILIAR), ("superadmin", None)],
)
def test_verify_firebase_id_token_reads_role_claim(monkeypatch, claim, expected_role):
    monkeypatch.setattr(auth_service, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth_service.firebase_auth,
        "verify_id_token",
        lambda token, app, check_revoked: {"uid": "firebase-uid", "role": claim},
    )

    user = auth_service.verify_firebase_id_token("valid-token")

    assert user.uid == "firebase-uid"
    assert user.role == expected_role


# --- Rutas ---


def test_login_route_includes_role_in_user(monkeypatch):
    async def fake_sign_in(payload: LoginRequest, **kwargs) -> LoginResponse:
        return _login_response(Role.FAMILIAR)

    monkeypatch.setattr(auth_router, "sign_in_with_email_password", fake_sign_in)

    response = TestClient(app).post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "secret123"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["role"] == "familiar"


def test_me_route_returns_role_from_token(monkeypatch):
    monkeypatch.setattr(auth_service, "get_firebase_app", lambda: object())
    # Correo verificado: sin él get_current_user responde 403 (D-LL-4).
    monkeypatch.setattr(
        auth_service.firebase_auth,
        "verify_id_token",
        lambda token, app, check_revoked: {
            "uid": "firebase-uid",
            "role": "cuidador",
            "email_verified": True,
        },
    )

    response = TestClient(app).get(
        "/api/auth/me",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert response.status_code == 200
    assert response.json()["uid"] == "firebase-uid"
    assert response.json()["role"] == "cuidador"


def test_refresh_route_returns_tokens(monkeypatch):
    received: list[RefreshRequest] = []

    async def fake(payload, **kwargs):
        received.append(payload)
        return _login_response(Role.FAMILIAR)

    monkeypatch.setattr(auth_router, "refresh_session", fake)

    response = TestClient(app).post(
        "/api/auth/refresh",
        json={"refresh_token": "refresh-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id_token": "id-token",
        "refresh_token": "refresh-token",
        "expires_in": 3600,
        "user": {
            "uid": "firebase-uid",
            "email": "user@example.com",
            "email_verified": None,
            "display_name": None,
            "photo_url": None,
            "role": "familiar",
        },
    }
    assert [payload.refresh_token for payload in received] == ["refresh-token"]


def test_refresh_route_ignores_bearer_header(monkeypatch):
    async def fake(payload, **kwargs):
        return _login_response(Role.FAMILIAR)

    monkeypatch.setattr(auth_router, "refresh_session", fake)

    response = TestClient(app).post(
        "/api/auth/refresh",
        json={"refresh_token": "refresh-token"},
        headers={"Authorization": "Bearer token-vencido"},
    )

    assert response.status_code == 200
    assert response.json()["id_token"] == "id-token"


def test_refresh_route_maps_invalid_token_to_401(monkeypatch):
    async def fake(payload, **kwargs):
        raise auth_service.InvalidTokenError("La sesión expiró. Inicia sesión de nuevo.")

    monkeypatch.setattr(auth_router, "refresh_session", fake)

    response = TestClient(app).post(
        "/api/auth/refresh",
        json={"refresh_token": "refresh-token"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "La sesión expiró. Inicia sesión de nuevo."}
    assert "www-authenticate" not in response.headers


def test_refresh_route_maps_user_disabled_to_403(monkeypatch):
    async def fake(payload, **kwargs):
        raise auth_service.UserDisabledError("La cuenta está deshabilitada.")

    monkeypatch.setattr(auth_router, "refresh_session", fake)

    response = TestClient(app).post(
        "/api/auth/refresh",
        json={"refresh_token": "refresh-token"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "La cuenta está deshabilitada."}


def test_refresh_route_requires_refresh_token(monkeypatch):
    received: list[RefreshRequest] = []

    async def fake(payload, **kwargs):
        received.append(payload)
        return _login_response()

    monkeypatch.setattr(auth_router, "refresh_session", fake)
    client = TestClient(app)

    missing = client.post("/api/auth/refresh", json={})
    blank = client.post("/api/auth/refresh", json={"refresh_token": "   "})

    assert missing.status_code == 422
    assert blank.status_code == 422
    assert received == []


@pytest.mark.parametrize(
    "error_class",
    [
        FirebaseConfigurationError,
        auth_service.AuthMethodDisabledError,
        auth_service.AuthProviderUnavailableError,
    ],
)
def test_login_route_hides_internal_error_on_503(monkeypatch, caplog, error_class):
    caplog.set_level(logging.ERROR, logger="app.api.auth_router")

    async def fake_sign_in(payload: LoginRequest, **kwargs) -> LoginResponse:
        raise error_class("mensaje-interno")

    monkeypatch.setattr(auth_router, "sign_in_with_email_password", fake_sign_in)

    response = TestClient(app).post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "secret123"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
    assert "mensaje-interno" not in response.text
    assert any(
        record.levelno == logging.ERROR and error_class.__name__ in record.getMessage()
        for record in caplog.records
    )


def test_refresh_route_hides_internal_error_on_503(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.api.auth_router")
    client = TestClient(app)

    for error_class in (
        FirebaseConfigurationError,
        auth_service.AuthProviderUnavailableError,
    ):
        caplog.clear()

        async def fake(payload, error_class=error_class, **kwargs):
            raise error_class("mensaje-interno")

        monkeypatch.setattr(auth_router, "refresh_session", fake)

        response = client.post(
            "/api/auth/refresh",
            json={"refresh_token": "refresh-token"},
        )

        assert response.status_code == 503
        assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
        assert "mensaje-interno" not in response.text
        assert any(
            record.levelno == logging.ERROR
            and error_class.__name__ in record.getMessage()
            for record in caplog.records
        )


def test_me_route_hides_provider_error_on_503(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.core.security")
    client = TestClient(app)

    for error_class in (
        FirebaseConfigurationError,
        auth_service.AuthProviderUnavailableError,
    ):
        caplog.clear()

        def fake_verify(token, error_class=error_class):
            raise error_class("mensaje-interno")

        monkeypatch.setattr(security, "verify_firebase_id_token", fake_verify)

        response = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 503
        assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
        assert "mensaje-interno" not in response.text
        assert any(
            record.levelno == logging.ERROR
            and error_class.__name__ in record.getMessage()
            for record in caplog.records
        )


# =====================================================================================
# Tareas 5 y 8: A4 registro, A5 recuperar contraseña, A6 perfil, A7 y A8 adultos,
# A9 cuidadores, A10 dispositivos.
# =====================================================================================


OOB_ROUTE = ("identitytoolkit.googleapis.com", "/v1/accounts:sendOobCode")
REGISTER_PATH = "/api/auth/register"
PASSWORD_RESET_PATH = "/api/auth/password-reset"
ME_PATH = "/api/auth/me"
ADULTS_PATH = "/api/auth/adults"
CAREGIVERS_PATH = "/api/auth/adults/{adult_id}/caregivers"
DEVICES_PATH = "/api/auth/devices"

FAMILY_UID = "family-uid"
ADULT_UID = "adult-uid"
OTHER_ADULT_UID = "other-adult-uid"
CAREGIVER_UID = "caregiver-uid"
NEW_UID = "firebase-uid"

BEARER_DETAIL = "Debes enviar un token Bearer."
EMAIL_EXISTS_DETAIL = "El correo ya está registrado."
WEAK_PASSWORD_DETAIL = "La contraseña debe tener al menos 6 caracteres."
ADULT_NOT_FOUND_DETAIL = "El adulto mayor no existe."
USER_NOT_FOUND_DETAIL = "El usuario no existe."
PHOTO_URL_INVALID_DETAIL = "Datos inválidos: photo_url: no es válido"
PASSWORD_TOO_SHORT_DETAIL = "Datos inválidos: password: es demasiado corto"
EMAIL_INVALID_DETAIL = "Datos inválidos: email: no es válido"

USER_KEYS = {"uid", "email", "email_verified", "display_name", "photo_url", "role"}
ADULT_KEYS = {
    "id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "birth_date",
    "address",
    "photo_url",
}
FIREBASE_APP = object()

REGISTER_BODY = {
    "first_name": "Ana",
    "last_name": "López",
    "email": "ana@example.com",
    "password": "secret123",
}
ADULT_BODY = {
    "first_name": "Rosa",
    "last_name": "Gómez",
    "email": "Rosa@Example.com",
    "password": "secret123",
    "phone": "3001234567",
    "birth_date": "1950-05-01",
    "address": "Calle 1 # 2-3",
    "photo_url": "https://example.com/rosa.png",
}
CAREGIVER_PERMISSIONS = {
    "manage_contacts": True,
    "manage_reminders": False,
    "receive_alerts": True,
}
CAREGIVER_BODY = {
    "first_name": "Luis",
    "last_name": "Pérez",
    "email": "luis@example.com",
    "permissions": CAREGIVER_PERMISSIONS,
}
NON_FAMILY_ROLES = [Role.CUIDADOR, Role.ADULTO_MAYOR, Role.ADMINISTRADOR, None]


class FakeFirebaseAuth:
    """Sustituto de firebase_admin.auth: nunca hace red y registra cada llamada."""

    def __init__(self) -> None:
        # Orden de las llamadas a Firebase (Admin SDK y REST) en un solo registro.
        self.events: list[str] = []
        self.next_uid = NEW_UID
        self.created: list[dict] = []
        self.claims: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.lookups: list[str] = []
        self.updated: list[tuple[str, dict]] = []
        # correo -> (uid, custom_claims) de las cuentas que ya existen.
        self.existing: dict[str, tuple[str, dict | None]] = {}
        self.create_error: Exception | None = None
        self.claims_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.lookup_error: Exception | None = None
        self.update_error: Exception | None = None

    def create_user(self, **kwargs):
        self.events.append("create_user")
        self.created.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return SimpleNamespace(uid=self.next_uid)

    def set_custom_user_claims(self, uid, custom_claims, app=None):
        assert app is FIREBASE_APP
        self.events.append("set_custom_user_claims")
        self.claims.append((uid, custom_claims))
        if self.claims_error is not None:
            raise self.claims_error

    def delete_user(self, uid, app=None):
        assert app is FIREBASE_APP
        self.events.append("delete_user")
        self.deleted.append(uid)
        if self.delete_error is not None:
            raise self.delete_error

    def get_user_by_email(self, email, app=None):
        assert app is FIREBASE_APP
        self.events.append("get_user_by_email")
        self.lookups.append(email)
        if self.lookup_error is not None:
            raise self.lookup_error
        if email not in self.existing:
            raise firebase_auth.UserNotFoundError("No user record found")
        uid, custom_claims = self.existing[email]
        return SimpleNamespace(uid=uid, custom_claims=custom_claims)

    def update_user(self, uid, **kwargs):
        self.events.append("update_user")
        self.updated.append((uid, kwargs))
        if self.update_error is not None:
            raise self.update_error
        return SimpleNamespace(uid=uid)


@pytest.fixture(autouse=True)
def account_state():
    shared = SimpleNamespace(
        users=InMemoryUserRepository(),
        devices=InMemoryDeviceRepository(),
    )
    app.dependency_overrides[get_user_repository] = lambda: shared.users
    app.dependency_overrides[get_device_repository] = lambda: shared.devices
    yield shared
    app.dependency_overrides.clear()


@pytest.fixture
def firebase(monkeypatch) -> FakeFirebaseAuth:
    fake = FakeFirebaseAuth()
    monkeypatch.setattr(auth_service, "get_firebase_app", lambda: FIREBASE_APP)
    for name in (
        "create_user",
        "set_custom_user_claims",
        "delete_user",
        "get_user_by_email",
        "update_user",
    ):
        monkeypatch.setattr(auth_service.firebase_auth, name, getattr(fake, name))
    return fake


@pytest.fixture
def rest(monkeypatch, firebase):
    """Los httpx.AsyncClient que crea auth_service usan un MockTransport enrutado."""
    real_async_client = httpx.AsyncClient
    state = SimpleNamespace(routes={}, seen=[], clients=[])

    def handler(request: httpx.Request) -> httpx.Response:
        route = (request.url.host, request.url.path)
        firebase.events.append(request.url.path)
        if route not in state.routes:
            raise AssertionError(f"Petición inesperada a {route}")
        state.seen.append(request)
        return state.routes[route](request)

    def factory(**kwargs) -> httpx.AsyncClient:
        client = real_async_client(transport=httpx.MockTransport(handler), **kwargs)
        state.clients.append(client)
        return client

    monkeypatch.setattr(auth_service.httpx, "AsyncClient", factory)
    return state


def _without_web_api_key(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(firebase_web_api_key="  "),
    )


def _oob_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"kind": "identitytoolkit#GetOobConfirmationCodeResponse"})


def _oob_network_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("sin red", request=request)


def _oob_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("lento", request=request)


def _oob_server_error_not_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="<html>Internal error</html>")


def _register_routes(oob_handler: Handler = _oob_ok) -> dict[tuple[str, str], Handler]:
    return {
        SIGN_IN_ROUTE: _sign_in_response,
        LOOKUP_ROUTE: _lookup_handler('{"role": "familiar"}'),
        OOB_ROUTE: oob_handler,
    }


def _requests_to(state, route: tuple[str, str]) -> list[httpx.Request]:
    return [
        request
        for request in state.seen
        if (request.url.host, request.url.path) == route
    ]


def _as_user(
    uid: str = FAMILY_UID,
    role: Role | None = Role.FAMILIAR,
    **fields,
) -> AuthenticatedUser:
    user = AuthenticatedUser(uid=uid, role=role, **fields)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _seed_family_link(users: InMemoryUserRepository) -> None:
    users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)


# --- A4: registro de familiar (rutas) ---


def test_register_creates_user_sets_claim_before_sign_in_and_returns_tokens(
    web_api_key, firebase, rest, account_state
):
    rest.routes.update(_register_routes())

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == 200
    assert response.json() == {
        "id_token": "id-token",
        "refresh_token": "refresh-token",
        "expires_in": 3600,
        "user": {
            "uid": NEW_UID,
            "email": "user@example.com",
            "email_verified": True,
            "display_name": "Test User",
            "photo_url": "https://example.com/avatar.png",
            "role": "familiar",
        },
    }
    # El claim se fija antes del inicio de sesión (D-02.3) y el correo va al final.
    assert firebase.events == [
        "create_user",
        "set_custom_user_claims",
        SIGN_IN_ROUTE[1],
        LOOKUP_ROUTE[1],
        OOB_ROUTE[1],
    ]
    assert firebase.created == [
        {
            "email": "ana@example.com",
            "password": "secret123",
            "display_name": "Ana López",
            "app": FIREBASE_APP,
        }
    ]
    assert firebase.claims == [(NEW_UID, {"role": "familiar"})]
    assert json.loads(_requests_to(rest, SIGN_IN_ROUTE)[0].content) == {
        "email": "ana@example.com",
        "password": "secret123",
        "returnSecureToken": True,
    }
    assert account_state.users.get_profile(NEW_UID) == UserProfile(
        uid=NEW_UID,
        first_name="Ana",
        last_name="López",
        email="ana@example.com",
        role=Role.FAMILIAR,
    )
    assert len(rest.clients) == 1
    assert rest.clients[0].is_closed is True


@pytest.mark.parametrize(
    "oob_handler",
    [_oob_ok, _firebase_error("TOO_MANY_ATTEMPTS_TRY_LATER"), _oob_network_error],
    ids=["accepted", "firebase_error", "network_error"],
)
def test_register_sends_verify_email_best_effort(
    web_api_key, firebase, rest, account_state, caplog, oob_handler
):
    caplog.set_level(logging.INFO, logger="app.services.auth_service")
    rest.routes.update(_register_routes(oob_handler))

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == 200
    assert response.json()["id_token"] == "id-token"
    assert response.json()["user"]["role"] == "familiar"
    oob_requests = _requests_to(rest, OOB_ROUTE)
    assert len(oob_requests) == 1
    assert oob_requests[0].url.params["key"] == "test-api-key"
    assert json.loads(oob_requests[0].content) == {
        "requestType": "VERIFY_EMAIL",
        "idToken": "id-token",
    }
    assert account_state.users.get_profile(NEW_UID) is not None
    assert "id-token" not in caplog.text
    assert "secret123" not in caplog.text
    if oob_handler is _oob_network_error:
        assert any(
            record.levelno == logging.WARNING
            and "AuthProviderUnavailableError" in record.getMessage()
            for record in caplog.records
        )


def test_register_password_keeps_surrounding_spaces(
    web_api_key, firebase, rest, account_state
):
    rest.routes.update(_register_routes())
    body = {
        "first_name": "  Ana  ",
        "last_name": " López ",
        "email": "  ana@example.com ",
        "password": "  secret 123  ",
    }

    response = TestClient(app).post(REGISTER_PATH, json=body)

    assert response.status_code == 200
    assert firebase.created[0]["password"] == "  secret 123  "
    assert firebase.created[0]["display_name"] == "Ana López"
    assert firebase.created[0]["email"] == "ana@example.com"
    assert json.loads(_requests_to(rest, SIGN_IN_ROUTE)[0].content)["password"] == (
        "  secret 123  "
    )
    profile = account_state.users.get_profile(NEW_UID)
    assert (profile.first_name, profile.last_name) == ("Ana", "López")


def test_register_email_exists_422(web_api_key, firebase, rest, account_state):
    firebase.create_error = firebase_auth.EmailAlreadyExistsError(
        "mensaje-interno", None, None
    )

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == 422
    assert response.json() == {"detail": EMAIL_EXISTS_DETAIL}
    assert firebase.claims == []
    assert rest.seen == []
    assert account_state.users.get_profile(NEW_UID) is None


@pytest.mark.parametrize("email", ["no-es-correo", "ana@example", "ana lopez@example.com"])
def test_register_invalid_email_422(web_api_key, firebase, rest, email):
    response = TestClient(app).post(REGISTER_PATH, json={**REGISTER_BODY, "email": email})

    assert response.status_code == 422
    assert response.json() == {"detail": EMAIL_INVALID_DETAIL}
    assert firebase.created == []
    assert rest.seen == []


def test_register_short_password_422(web_api_key, firebase, rest):
    response = TestClient(app).post(
        REGISTER_PATH, json={**REGISTER_BODY, "password": "12345"}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": PASSWORD_TOO_SHORT_DETAIL}
    assert "12345" not in response.text
    assert firebase.created == []


@pytest.mark.parametrize(
    ("field", "detail"),
    [
        ("first_name", "Datos inválidos: first_name: es obligatorio"),
        ("last_name", "Datos inválidos: last_name: es obligatorio"),
        ("email", "Datos inválidos: email: es obligatorio"),
        ("password", "Datos inválidos: password: es obligatorio"),
    ],
)
def test_register_missing_field_422(firebase, field, detail):
    body = {key: value for key, value in REGISTER_BODY.items() if key != field}

    response = TestClient(app).post(REGISTER_PATH, json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert firebase.created == []


def test_register_blank_first_name_422(firebase):
    response = TestClient(app).post(
        REGISTER_PATH, json={**REGISTER_BODY, "first_name": "   "}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: first_name: es demasiado corto"}
    assert firebase.created == []


def test_register_without_web_api_key_503_creates_nothing(
    monkeypatch, firebase, rest, account_state
):
    _without_web_api_key(monkeypatch)

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == 503
    assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
    assert firebase.created == []
    assert firebase.claims == []
    assert rest.seen == []
    assert account_state.users.get_profile(NEW_UID) is None


@pytest.mark.parametrize("failure", ["firebase_error", "missing_credentials"])
def test_register_firebase_unavailable_503(
    monkeypatch, web_api_key, firebase, rest, account_state, caplog, failure
):
    caplog.set_level(logging.ERROR, logger="app.api.auth_router")
    if failure == "firebase_error":
        firebase.create_error = firebase_exceptions.UnavailableError("mensaje-interno")
        expected_error = "AuthProviderUnavailableError"
    else:

        def missing_credentials():
            raise FirebaseConfigurationError("mensaje-interno")

        monkeypatch.setattr(auth_service, "get_firebase_app", missing_credentials)
        expected_error = "FirebaseConfigurationError"

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == 503
    assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
    assert "mensaje-interno" not in response.text
    assert rest.seen == []
    assert account_state.users.get_profile(NEW_UID) is None
    assert any(expected_error in record.getMessage() for record in caplog.records)


def test_register_weak_password_from_firebase_422(web_api_key, firebase, rest):
    firebase.create_error = ValueError("Password must be a string at least 6 characters long.")

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == 422
    assert response.json() == {"detail": WEAK_PASSWORD_DETAIL}
    assert rest.seen == []


@pytest.mark.parametrize(
    ("firebase_code", "status_code", "detail"),
    [
        ("INVALID_LOGIN_CREDENTIALS", 401, "Correo o contraseña incorrectos."),
        ("USER_DISABLED", 403, "La cuenta está deshabilitada."),
        (
            "TOO_MANY_ATTEMPTS_TRY_LATER",
            429,
            "Demasiados intentos. Intenta nuevamente más tarde.",
        ),
        ("OPERATION_NOT_ALLOWED", 503, auth_router.AUTH_UNAVAILABLE_DETAIL),
    ],
)
def test_register_sign_in_errors_map_like_login(
    web_api_key, firebase, rest, account_state, firebase_code, status_code, detail
):
    rest.routes[SIGN_IN_ROUTE] = _firebase_error(firebase_code)

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    # La cuenta ya se creó con su claim: el usuario puede iniciar sesión después (A1).
    assert firebase.claims == [(NEW_UID, {"role": "familiar"})]
    assert account_state.users.get_profile(NEW_UID) is not None
    assert _requests_to(rest, OOB_ROUTE) == []


# --- A4: servicio ---


@pytest.mark.asyncio
async def test_register_family_service_uses_given_client(web_api_key, firebase):
    users = InMemoryUserRepository()
    seen: list[httpx.Request] = []

    async with httpx.AsyncClient(
        transport=_firebase_transport(_register_routes(), seen)
    ) as client:
        login = await auth_service.register_family(
            RegisterRequest(**REGISTER_BODY), users, client=client
        )
        assert client.is_closed is False

    assert login.user.role is Role.FAMILIAR
    assert [(request.url.host, request.url.path) for request in seen] == [
        SIGN_IN_ROUTE,
        LOOKUP_ROUTE,
        OOB_ROUTE,
    ]
    assert users.get_profile(NEW_UID).role is Role.FAMILIAR


@pytest.mark.asyncio
async def test_register_family_service_without_web_api_key_creates_nothing(
    monkeypatch, firebase
):
    _without_web_api_key(monkeypatch)
    users = InMemoryUserRepository()

    with pytest.raises(FirebaseConfigurationError):
        await auth_service.register_family(RegisterRequest(**REGISTER_BODY), users)

    assert firebase.created == []
    assert users.get_profile(NEW_UID) is None


# --- create_firebase_user y find_firebase_user_by_email (servicio) ---


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.ADULTO_MAYOR, Role.CUIDADOR])
def test_create_firebase_user_returns_uid_and_sets_role_claim(firebase, role):
    uid = auth_service.create_firebase_user("a@example.com", "secret123", "A B", role)

    assert uid == NEW_UID
    assert firebase.events == ["create_user", "set_custom_user_claims"]
    assert firebase.created == [
        {
            "email": "a@example.com",
            "password": "secret123",
            "display_name": "A B",
            "app": FIREBASE_APP,
        }
    ]
    assert firebase.claims == [(NEW_UID, {"role": role.value})]


def test_create_firebase_user_rolls_back_when_claim_fails(firebase):
    firebase.claims_error = firebase_exceptions.UnavailableError("mensaje-interno")

    with pytest.raises(auth_service.AuthProviderUnavailableError):
        auth_service.create_firebase_user(
            "a@example.com", "secret123", "A B", Role.FAMILIAR
        )

    assert firebase.events == ["create_user", "set_custom_user_claims", "delete_user"]
    assert firebase.deleted == [NEW_UID]


def test_create_firebase_user_rollback_failure_is_logged(firebase, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.auth_service")
    firebase.claims_error = ValueError("claims inválidos")
    firebase.delete_error = firebase_exceptions.UnavailableError("mensaje-interno")

    with pytest.raises(auth_service.AuthProviderUnavailableError):
        auth_service.create_firebase_user(
            "a@example.com", "secret123", "A B", Role.CUIDADOR
        )

    assert firebase.deleted == [NEW_UID]
    assert any(
        "UnavailableError" in record.getMessage() and NEW_UID in record.getMessage()
        for record in caplog.records
    )
    assert "mensaje-interno" not in caplog.text
    assert "secret123" not in caplog.text


def test_create_firebase_user_admin_role_rejected(monkeypatch, firebase):
    def unexpected_app():
        raise AssertionError("No debe llegar a Firebase")

    monkeypatch.setattr(auth_service, "get_firebase_app", unexpected_app)

    with pytest.raises(ValueError):
        auth_service.create_firebase_user(
            "a@example.com", "secret123", "A B", Role.ADMINISTRADOR
        )

    assert firebase.created == []
    assert firebase.claims == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            firebase_auth.EmailAlreadyExistsError("mensaje-interno", None, None),
            auth_service.EmailAlreadyExistsError,
        ),
        (ValueError("Password must be at least 6"), auth_service.WeakPasswordError),
        (
            firebase_exceptions.UnavailableError("mensaje-interno"),
            auth_service.AuthProviderUnavailableError,
        ),
    ],
)
def test_create_firebase_user_maps_create_errors(firebase, error, expected):
    firebase.create_error = error

    with pytest.raises(expected) as error_info:
        auth_service.create_firebase_user(
            "a@example.com", "secret123", "A B", Role.FAMILIAR
        )

    assert "mensaje-interno" not in str(error_info.value)
    assert firebase.claims == []
    if expected is auth_service.EmailAlreadyExistsError:
        assert str(error_info.value) == EMAIL_EXISTS_DETAIL
    if expected is auth_service.WeakPasswordError:
        assert str(error_info.value) == WEAK_PASSWORD_DETAIL


def test_create_firebase_user_propagates_missing_credentials(monkeypatch, firebase):
    def missing_credentials():
        raise FirebaseConfigurationError("La ruta de credenciales no está configurada.")

    monkeypatch.setattr(auth_service, "get_firebase_app", missing_credentials)

    with pytest.raises(FirebaseConfigurationError):
        auth_service.create_firebase_user(
            "a@example.com", "secret123", "A B", Role.FAMILIAR
        )

    assert firebase.created == []


@pytest.mark.parametrize(
    ("custom_claims", "expected_role"),
    [
        ({"role": "cuidador"}, Role.CUIDADOR),
        ({"role": "familiar"}, Role.FAMILIAR),
        ({}, None),
        (None, None),
        ({"role": "superadmin"}, None),
    ],
)
def test_find_firebase_user_by_email_reads_role(firebase, custom_claims, expected_role):
    firebase.existing["luis@example.com"] = (CAREGIVER_UID, custom_claims)

    found = auth_service.find_firebase_user_by_email("luis@example.com")

    assert found == (CAREGIVER_UID, expected_role)


def test_find_firebase_user_by_email_unknown_returns_none(firebase):
    assert auth_service.find_firebase_user_by_email("nadie@example.com") is None
    assert firebase.lookups == ["nadie@example.com"]


def test_find_firebase_user_by_email_firebase_error_unavailable(firebase):
    firebase.lookup_error = firebase_exceptions.UnavailableError("mensaje-interno")

    with pytest.raises(auth_service.AuthProviderUnavailableError):
        auth_service.find_firebase_user_by_email("luis@example.com")


# --- A7: crear adulto mayor ---


def test_create_adult_links_family_and_returns_contract_fields(firebase, account_state):
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.next_uid = ADULT_UID

    response = TestClient(app).post(ADULTS_PATH, json=ADULT_BODY)

    assert response.status_code == 200
    assert response.json() == {
        "id": ADULT_UID,
        "first_name": "Rosa",
        "last_name": "Gómez",
        "email": "rosa@example.com",
        "phone": "3001234567",
        "birth_date": "1950-05-01",
        "address": "Calle 1 # 2-3",
        "photo_url": "https://example.com/rosa.png",
    }
    assert firebase.created == [
        {
            "email": "Rosa@Example.com",
            "password": "secret123",
            "display_name": "Rosa Gómez",
            "app": FIREBASE_APP,
        }
    ]
    assert firebase.claims == [(ADULT_UID, {"role": "adulto_mayor"})]
    users = account_state.users
    assert users.link_of(FAMILY_UID, ADULT_UID) == AdultLink(
        user_uid=FAMILY_UID,
        adult_uid=ADULT_UID,
        role=Role.FAMILIAR,
        permissions=None,
    )
    assert users.responsible_family_of(ADULT_UID) == FAMILY_UID
    assert users.get_profile(ADULT_UID) == UserProfile(
        uid=ADULT_UID,
        first_name="Rosa",
        last_name="Gómez",
        email="rosa@example.com",
        role=Role.ADULTO_MAYOR,
        phone="3001234567",
        birth_date=date(1950, 5, 1),
        address="Calle 1 # 2-3",
        photo_url="https://example.com/rosa.png",
    )


def test_create_adult_optional_fields_default_to_null(firebase, account_state):
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.next_uid = ADULT_UID
    body = {
        "first_name": "Rosa",
        "last_name": "Gómez",
        "email": "rosa@example.com",
        "password": "  secret123 ",
        "phone": "   ",
        "address": "",
        "photo_url": "",
    }

    response = TestClient(app).post(ADULTS_PATH, json=body)

    assert response.status_code == 200
    assert set(response.json()) == ADULT_KEYS
    assert response.json()["phone"] is None
    assert response.json()["birth_date"] is None
    assert response.json()["address"] is None
    assert response.json()["photo_url"] is None
    assert firebase.created[0]["password"] == "  secret123 "


@pytest.mark.parametrize("role", NON_FAMILY_ROLES)
def test_create_adult_by_caregiver_403(firebase, account_state, role):
    _as_user("other-uid", role)

    response = TestClient(app).post(ADULTS_PATH, json=ADULT_BODY)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert firebase.created == []
    assert account_state.users.adults_of("other-uid") == []


def test_create_adult_email_exists_422(firebase, account_state):
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.create_error = firebase_auth.EmailAlreadyExistsError(
        "mensaje-interno", None, None
    )

    response = TestClient(app).post(ADULTS_PATH, json=ADULT_BODY)

    assert response.status_code == 422
    assert response.json() == {"detail": EMAIL_EXISTS_DETAIL}
    assert account_state.users.adults_of(FAMILY_UID) == []


@pytest.mark.parametrize(
    "photo_url",
    ["javascript:alert(1)", "data:image/png;base64,AAAA", "ftp://example.com/a.png"],
)
def test_create_adult_photo_url_javascript_422(firebase, photo_url):
    _as_user(FAMILY_UID, Role.FAMILIAR)

    response = TestClient(app).post(
        ADULTS_PATH, json={**ADULT_BODY, "photo_url": photo_url}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": PHOTO_URL_INVALID_DETAIL}
    assert firebase.created == []


def test_create_adult_short_password_422(firebase):
    _as_user(FAMILY_UID, Role.FAMILIAR)

    response = TestClient(app).post(ADULTS_PATH, json={**ADULT_BODY, "password": "12345"})

    assert response.status_code == 422
    assert response.json() == {"detail": PASSWORD_TOO_SHORT_DETAIL}
    assert firebase.created == []


def test_create_adult_invalid_birth_date_422(firebase):
    _as_user(FAMILY_UID, Role.FAMILIAR)

    response = TestClient(app).post(
        ADULTS_PATH, json={**ADULT_BODY, "birth_date": "01/05/1950"}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: birth_date: no es válido"}
    assert firebase.created == []


def test_create_adult_firebase_unavailable_503(firebase, account_state):
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.create_error = firebase_exceptions.UnavailableError("mensaje-interno")

    response = TestClient(app).post(ADULTS_PATH, json=ADULT_BODY)

    assert response.status_code == 503
    assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
    assert "mensaje-interno" not in response.text
    assert account_state.users.adults_of(FAMILY_UID) == []


@pytest.mark.parametrize("role", NON_FAMILY_ROLES)
def test_create_adult_service_requires_family_role(firebase, role):
    users = InMemoryUserRepository()
    payload = AdultCreateRequest(**ADULT_BODY)

    with pytest.raises(auth_service.AccountPermissionDeniedError):
        auth_service.create_adult(AuthenticatedUser(uid="u", role=role), payload, users)

    assert firebase.created == []


# --- A8: listar adultos ---


def _seed_adult_profile(users: InMemoryUserRepository, uid: str, first_name: str) -> None:
    users.add_profile(
        UserProfile(
            uid=uid,
            first_name=first_name,
            last_name="Gómez",
            email=f"{uid}@example.com",
            role=Role.ADULTO_MAYOR,
            phone="300",
            birth_date=date(1950, 5, 1),
            address="Calle 1",
            photo_url="https://example.com/a.png",
        )
    )


def test_list_adults_family_and_caregiver(account_state):
    users = account_state.users
    _seed_adult_profile(users, "adult-1", "Rosa")
    _seed_adult_profile(users, "adult-2", "Carmen")
    users.link_adult(FAMILY_UID, "adult-1", Role.FAMILIAR)
    users.link_adult(FAMILY_UID, "adult-without-profile", Role.FAMILIAR)
    users.link_adult(FAMILY_UID, "adult-2", Role.FAMILIAR)
    users.link_adult(CAREGIVER_UID, "adult-2", Role.CUIDADOR)
    client = TestClient(app)

    _as_user(FAMILY_UID, Role.FAMILIAR)
    family_response = client.get(ADULTS_PATH)
    _as_user(CAREGIVER_UID, Role.CUIDADOR)
    caregiver_response = client.get(ADULTS_PATH)
    _as_user("lonely-family-uid", Role.FAMILIAR)
    empty_response = client.get(ADULTS_PATH)

    assert family_response.status_code == 200
    assert [adult["id"] for adult in family_response.json()] == ["adult-1", "adult-2"]
    assert family_response.json()[0] == {
        "id": "adult-1",
        "first_name": "Rosa",
        "last_name": "Gómez",
        "email": "adult-1@example.com",
        "phone": "300",
        "birth_date": "1950-05-01",
        "address": "Calle 1",
        "photo_url": "https://example.com/a.png",
    }
    assert caregiver_response.status_code == 200
    assert [adult["id"] for adult in caregiver_response.json()] == ["adult-2"]
    assert set(caregiver_response.json()[0]) == ADULT_KEYS
    assert empty_response.status_code == 200
    assert empty_response.json() == []


@pytest.mark.parametrize("role", [Role.ADULTO_MAYOR, Role.ADMINISTRADOR, None])
def test_list_adults_adult_role_403(account_state, role):
    _as_user(ADULT_UID, role)

    response = TestClient(app).get(ADULTS_PATH)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


@pytest.mark.parametrize("role", [Role.ADULTO_MAYOR, Role.ADMINISTRADOR, None])
def test_list_adults_service_requires_role(role):
    with pytest.raises(auth_service.AccountPermissionDeniedError):
        auth_service.list_adults(
            AuthenticatedUser(uid="u", role=role), InMemoryUserRepository()
        )


# --- A9: invitar cuidador ---


def test_create_caregiver_new_user_sends_reset_email_and_links_with_permissions_201(
    web_api_key, firebase, rest, account_state
):
    _seed_family_link(account_state.users)
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.next_uid = CAREGIVER_UID
    rest.routes[OOB_ROUTE] = _oob_ok

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 201
    assert response.json() == {
        "id": CAREGIVER_UID,
        "first_name": "Luis",
        "last_name": "Pérez",
        "email": "luis@example.com",
        "permissions": CAREGIVER_PERMISSIONS,
    }
    assert firebase.lookups == ["luis@example.com"]
    assert len(firebase.created) == 1
    created = firebase.created[0]
    assert created["email"] == "luis@example.com"
    assert created["display_name"] == "Luis Pérez"
    assert len(created["password"]) >= 43
    assert firebase.claims == [(CAREGIVER_UID, {"role": "cuidador"})]
    oob_requests = _requests_to(rest, OOB_ROUTE)
    assert len(oob_requests) == 1
    assert json.loads(oob_requests[0].content) == {
        "requestType": "PASSWORD_RESET",
        "email": "luis@example.com",
    }
    users = account_state.users
    assert users.link_of(CAREGIVER_UID, ADULT_UID) == AdultLink(
        user_uid=CAREGIVER_UID,
        adult_uid=ADULT_UID,
        role=Role.CUIDADOR,
        permissions=CaregiverPermissions(**CAREGIVER_PERMISSIONS),
    )
    assert users.get_profile(CAREGIVER_UID) == UserProfile(
        uid=CAREGIVER_UID,
        first_name="Luis",
        last_name="Pérez",
        email="luis@example.com",
        role=Role.CUIDADOR,
    )
    assert all(client.is_closed for client in rest.clients)


@pytest.mark.parametrize("with_profile", [True, False], ids=["with_profile", "no_profile"])
def test_create_caregiver_existing_caregiver_only_links_no_email(
    web_api_key, firebase, rest, account_state, with_profile
):
    users = account_state.users
    _seed_family_link(users)
    # Vínculo previo con otros permisos: A9 lo sobrescribe con los del payload.
    users.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)
    if with_profile:
        users.add_profile(
            UserProfile(
                uid=CAREGIVER_UID,
                first_name="Luis Alberto",
                last_name="Pérez Ruiz",
                email="Luis@Example.com",
                role=Role.CUIDADOR,
            )
        )
    firebase.existing["luis@example.com"] = (CAREGIVER_UID, {"role": "cuidador"})
    _as_user(FAMILY_UID, Role.FAMILIAR)

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 201
    expected_names = ("Luis Alberto", "Pérez Ruiz") if with_profile else ("Luis", "Pérez")
    assert response.json() == {
        "id": CAREGIVER_UID,
        "first_name": expected_names[0],
        "last_name": expected_names[1],
        "email": "luis@example.com",
        "permissions": CAREGIVER_PERMISSIONS,
    }
    assert firebase.created == []
    assert firebase.claims == []
    assert rest.seen == []
    link = users.link_of(CAREGIVER_UID, ADULT_UID)
    assert link.permissions == CaregiverPermissions(**CAREGIVER_PERMISSIONS)
    profile = users.get_profile(CAREGIVER_UID)
    assert (profile.first_name, profile.last_name) == expected_names


@pytest.mark.parametrize(
    "custom_claims",
    [
        {"role": "familiar"},
        {"role": "adulto_mayor"},
        {"role": "administrador"},
        {"role": "superadmin"},
        {},
        None,
    ],
)
def test_create_caregiver_existing_other_role_or_no_claim_422(
    web_api_key, firebase, rest, account_state, custom_claims
):
    _seed_family_link(account_state.users)
    firebase.existing["luis@example.com"] = ("existing-uid", custom_claims)
    _as_user(FAMILY_UID, Role.FAMILIAR)

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 422
    assert response.json() == {"detail": EMAIL_EXISTS_DETAIL}
    assert firebase.created == []
    assert rest.seen == []
    assert account_state.users.link_of("existing-uid", ADULT_UID) is None
    assert account_state.users.get_profile("existing-uid") is None


@pytest.mark.parametrize("setup", ["not_linked", "caregiver_link", "other_adult"])
def test_create_caregiver_unlinked_adult_404(firebase, rest, account_state, setup):
    users = account_state.users
    if setup == "caregiver_link":
        users.link_adult(FAMILY_UID, ADULT_UID, Role.CUIDADOR)
    elif setup == "other_adult":
        users.link_adult(FAMILY_UID, OTHER_ADULT_UID, Role.FAMILIAR)
    _as_user(FAMILY_UID, Role.FAMILIAR)

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 404
    assert response.json() == {"detail": ADULT_NOT_FOUND_DETAIL}
    assert firebase.lookups == []
    assert firebase.created == []
    assert rest.seen == []


@pytest.mark.parametrize("role", NON_FAMILY_ROLES)
def test_create_caregiver_by_caregiver_403(firebase, rest, account_state, role):
    users = account_state.users
    users.link_adult("other-uid", ADULT_UID, Role.CUIDADOR)
    _as_user("other-uid", role)

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert firebase.lookups == []
    assert firebase.created == []


def test_create_caregiver_temp_password_not_logged_nor_returned(
    monkeypatch, web_api_key, firebase, rest, account_state, caplog
):
    caplog.set_level(logging.DEBUG)
    real_token_urlsafe = auth_service.secrets.token_urlsafe
    generated: list[tuple[int, str]] = []

    def spy_token_urlsafe(nbytes=None):
        value = real_token_urlsafe(nbytes)
        generated.append((nbytes, value))
        return value

    monkeypatch.setattr(auth_service.secrets, "token_urlsafe", spy_token_urlsafe)
    _seed_family_link(account_state.users)
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.next_uid = CAREGIVER_UID
    rest.routes[OOB_ROUTE] = _oob_ok

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 201
    assert len(generated) == 1
    nbytes, password = generated[0]
    assert nbytes == 32
    assert firebase.created[0]["password"] == password
    assert password not in response.text
    assert password not in caplog.text
    assert password not in repr(account_state.users.get_profile(CAREGIVER_UID))
    assert all(password.encode() not in request.content for request in rest.seen)


@pytest.mark.parametrize("failure", ["network_error", "missing_web_api_key"])
def test_create_caregiver_reset_email_failure_still_201(
    monkeypatch, web_api_key, firebase, rest, account_state, caplog, failure
):
    caplog.set_level(logging.WARNING, logger="app.services.auth_service")
    if failure == "network_error":
        rest.routes[OOB_ROUTE] = _oob_network_error
        expected_error = "AuthProviderUnavailableError"
    else:
        _without_web_api_key(monkeypatch)
        expected_error = "FirebaseConfigurationError"
    _seed_family_link(account_state.users)
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.next_uid = CAREGIVER_UID

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 201
    assert response.json()["id"] == CAREGIVER_UID
    assert account_state.users.link_of(CAREGIVER_UID, ADULT_UID) is not None
    assert any(
        record.levelno == logging.WARNING and expected_error in record.getMessage()
        for record in caplog.records
    )
    assert firebase.created[0]["password"] not in caplog.text


def test_create_caregiver_firebase_unavailable_503(firebase, rest, account_state):
    _seed_family_link(account_state.users)
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.lookup_error = firebase_exceptions.UnavailableError("mensaje-interno")

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 503
    assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
    assert "mensaje-interno" not in response.text
    assert firebase.created == []
    assert account_state.users.caregivers_of(ADULT_UID) == []


def test_create_caregiver_invalid_permissions_422(firebase, account_state):
    _seed_family_link(account_state.users)
    _as_user(FAMILY_UID, Role.FAMILIAR)
    body = {**CAREGIVER_BODY, "permissions": {"manage_contacts": True}}

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=body
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": (
            "Datos inválidos: permissions.manage_reminders: es obligatorio; "
            "permissions.receive_alerts: es obligatorio"
        )
    }
    assert firebase.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", NON_FAMILY_ROLES)
async def test_create_caregiver_service_requires_family_role(firebase, role):
    users = InMemoryUserRepository()
    users.link_adult("u", ADULT_UID, Role.FAMILIAR)

    with pytest.raises(auth_service.AccountPermissionDeniedError):
        await auth_service.create_caregiver(
            AuthenticatedUser(uid="u", role=role),
            ADULT_UID,
            CaregiverCreateRequest(**CAREGIVER_BODY),
            users,
        )

    assert firebase.lookups == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", ADULTS_PATH, ADULT_BODY),
        ("GET", ADULTS_PATH, None),
        ("POST", CAREGIVERS_PATH.format(adult_id=ADULT_UID), CAREGIVER_BODY),
    ],
)
def test_adults_and_caregivers_routes_require_bearer_401(firebase, method, path, body):
    response = TestClient(app).request(method, path, json=body)

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    assert firebase.created == []
    assert firebase.lookups == []


# --- A5: recuperar contraseña ---


def test_password_reset_returns_204_for_unknown_email(web_api_key, rest):
    rest.routes[OOB_ROUTE] = _firebase_error("EMAIL_NOT_FOUND")

    response = TestClient(app).post(
        PASSWORD_RESET_PATH, json={"email": " nadie@example.com "}
    )

    assert response.status_code == 204
    assert response.content == b""
    oob_requests = _requests_to(rest, OOB_ROUTE)
    assert len(oob_requests) == 1
    assert oob_requests[0].url.params["key"] == "test-api-key"
    assert json.loads(oob_requests[0].content) == {
        "requestType": "PASSWORD_RESET",
        "email": "nadie@example.com",
    }
    assert all(client.is_closed for client in rest.clients)


def test_password_reset_accepted_204(web_api_key, rest):
    rest.routes[OOB_ROUTE] = _oob_ok

    response = TestClient(app).post(PASSWORD_RESET_PATH, json={"email": "ana@example.com"})

    assert response.status_code == 204
    assert response.content == b""
    assert len(_requests_to(rest, OOB_ROUTE)) == 1


@pytest.mark.parametrize(
    "oob_handler",
    [
        _firebase_error("EMAIL_NOT_FOUND"),
        _firebase_error("RESET_PASSWORD_EXCEED_LIMIT"),
        _firebase_error("TOO_MANY_ATTEMPTS_TRY_LATER"),
        _oob_server_error_not_json,
    ],
    ids=[
        "EMAIL_NOT_FOUND",
        "RESET_PASSWORD_EXCEED_LIMIT",
        "TOO_MANY_ATTEMPTS_TRY_LATER",
        "500_not_json",
    ],
)
def test_password_reset_any_firebase_error_204(web_api_key, rest, oob_handler):
    rest.routes[OOB_ROUTE] = oob_handler

    response = TestClient(app).post(PASSWORD_RESET_PATH, json={"email": "ana@example.com"})

    # Nunca 429 ni otro código derivado de Firebase: revelaría si el correo existe (P14).
    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"email": "no-es-correo"}, EMAIL_INVALID_DETAIL),
        ({"email": "ana@example"}, EMAIL_INVALID_DETAIL),
        ({"email": "  "}, "Datos inválidos: email: es demasiado corto"),
        ({}, "Datos inválidos: email: es obligatorio"),
    ],
)
def test_password_reset_invalid_email_422(web_api_key, rest, body, detail):
    response = TestClient(app).post(PASSWORD_RESET_PATH, json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert rest.seen == []


@pytest.mark.parametrize("oob_handler", [_oob_network_error, _oob_timeout])
def test_password_reset_network_error_503(web_api_key, rest, caplog, oob_handler):
    caplog.set_level(logging.ERROR, logger="app.api.auth_router")
    rest.routes[OOB_ROUTE] = oob_handler

    response = TestClient(app).post(PASSWORD_RESET_PATH, json={"email": "ana@example.com"})

    assert response.status_code == 503
    assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
    assert any(
        "AuthProviderUnavailableError" in record.getMessage() for record in caplog.records
    )
    assert "ana@example.com" not in caplog.text
    assert all(client.is_closed for client in rest.clients)


def test_password_reset_missing_web_api_key_503(monkeypatch, rest):
    _without_web_api_key(monkeypatch)

    response = TestClient(app).post(PASSWORD_RESET_PATH, json={"email": "ana@example.com"})

    assert response.status_code == 503
    assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
    assert rest.seen == []


@pytest.mark.parametrize(
    ("firebase_message", "expected_code"),
    [
        ("RESET_PASSWORD_EXCEED_LIMIT : nadie@example.com", "RESET_PASSWORD_EXCEED_LIMIT"),
        ("EMAIL_NOT_FOUND", "EMAIL_NOT_FOUND"),
        ("Error para nadie@example.com", "desconocido"),
    ],
)
def test_password_reset_logs_only_code(
    web_api_key, rest, caplog, firebase_message, expected_code
):
    caplog.set_level(logging.DEBUG)
    rest.routes[OOB_ROUTE] = _firebase_error(firebase_message)

    response = TestClient(app).post(
        PASSWORD_RESET_PATH, json={"email": "nadie@example.com"}
    )

    assert response.status_code == 204
    service_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.services.auth_service"
    ]
    assert any(expected_code in message for message in service_messages)
    assert "nadie@example.com" not in caplog.text
    assert "test-api-key" not in caplog.text


@pytest.mark.asyncio
async def test_send_oob_code_requires_web_api_key_before_request(monkeypatch):
    _without_web_api_key(monkeypatch)
    seen: list[httpx.Request] = []

    async with httpx.AsyncClient(transport=_firebase_transport({}, seen)) as client:
        with pytest.raises(FirebaseConfigurationError):
            await auth_service.send_password_reset_email("ana@example.com", client=client)

    assert seen == []


@pytest.mark.asyncio
async def test_send_oob_code_with_given_client_keeps_it_open(web_api_key):
    seen: list[httpx.Request] = []

    async with httpx.AsyncClient(
        transport=_firebase_transport({OOB_ROUTE: _oob_ok}, seen)
    ) as client:
        await auth_service.send_oob_code("VERIFY_EMAIL", id_token="id-token", client=client)
        assert client.is_closed is False

    assert json.loads(seen[0].content) == {"requestType": "VERIFY_EMAIL", "idToken": "id-token"}


# --- A6: actualizar perfil ---


def _seed_family_profile(users: InMemoryUserRepository) -> None:
    users.add_profile(
        UserProfile(
            uid=FAMILY_UID,
            first_name="Ana",
            last_name="López",
            email="ana@example.com",
            role=Role.FAMILIAR,
            phone="111",
            photo_url="https://example.com/old.png",
        )
    )


def _family_user(**overrides) -> AuthenticatedUser:
    fields = {
        "email": "ana@example.com",
        "email_verified": True,
        "display_name": "Ana López",
        "photo_url": "https://example.com/old.png",
    }
    fields.update(overrides)
    return _as_user(FAMILY_UID, fields.pop("role", Role.FAMILIAR), **fields)


def test_update_profile_changes_display_name_and_photo(firebase, account_state):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(
        ME_PATH,
        json={"first_name": " Ana María ", "photo_url": "https://example.com/new.png"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "uid": FAMILY_UID,
        "email": "ana@example.com",
        "email_verified": True,
        "display_name": "Ana María López",
        "photo_url": "https://example.com/new.png",
        "role": "familiar",
    }
    assert firebase.updated == [
        (
            FAMILY_UID,
            {
                "app": FIREBASE_APP,
                "display_name": "Ana María López",
                "photo_url": "https://example.com/new.png",
            },
        )
    ]
    profile = account_state.users.get_profile(FAMILY_UID)
    assert (profile.first_name, profile.last_name) == ("Ana María", "López")
    assert profile.photo_url == "https://example.com/new.png"
    assert profile.phone == "111"


def test_update_profile_phone_not_in_response(firebase, account_state):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(ME_PATH, json={"phone": " 3009876543 "})

    assert response.status_code == 200
    assert set(response.json()) == USER_KEYS
    assert "3009876543" not in response.text
    assert response.json()["display_name"] == "Ana López"
    assert account_state.users.get_profile(FAMILY_UID).phone == "3009876543"
    # Nada que cambiar en Firebase.
    assert firebase.updated == []


@pytest.mark.parametrize("phone", [None, "", "   "])
def test_update_profile_clears_phone(firebase, account_state, phone):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(ME_PATH, json={"phone": phone})

    assert response.status_code == 200
    assert account_state.users.get_profile(FAMILY_UID).phone is None
    assert firebase.updated == []


@pytest.mark.parametrize(
    "photo_url",
    ["data:image/png;base64,AAAA", "javascript:alert(1)", "ftp://example.com/a.png"],
)
def test_update_profile_photo_url_data_scheme_422(firebase, account_state, photo_url):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(ME_PATH, json={"photo_url": photo_url})

    assert response.status_code == 422
    assert response.json() == {"detail": PHOTO_URL_INVALID_DETAIL}
    assert firebase.updated == []
    assert account_state.users.get_profile(FAMILY_UID).photo_url == (
        "https://example.com/old.png"
    )


@pytest.mark.parametrize("photo_url", ["https://", "https:///x"])
def test_update_profile_photo_url_without_host_422(firebase, account_state, photo_url):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(ME_PATH, json={"photo_url": photo_url})

    assert response.status_code == 422
    assert response.json() == {"detail": PHOTO_URL_INVALID_DETAIL}
    assert firebase.updated == []


def test_update_profile_null_photo_url_deletes_attribute(firebase, account_state):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(ME_PATH, json={"photo_url": None})

    assert response.status_code == 200
    assert response.json()["photo_url"] is None
    assert len(firebase.updated) == 1
    uid, changes = firebase.updated[0]
    assert uid == FAMILY_UID
    assert changes["photo_url"] is firebase_auth.DELETE_ATTRIBUTE
    assert "display_name" not in changes
    assert account_state.users.get_profile(FAMILY_UID).photo_url is None


def test_update_profile_null_first_name_ignored(firebase, account_state):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(
        ME_PATH, json={"first_name": None, "last_name": "Pérez"}
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Ana Pérez"
    assert firebase.updated == [
        (FAMILY_UID, {"app": FIREBASE_APP, "display_name": "Ana Pérez"})
    ]
    profile = account_state.users.get_profile(FAMILY_UID)
    assert (profile.first_name, profile.last_name) == ("Ana", "Pérez")


def test_update_profile_only_null_names_changes_nothing(firebase, account_state):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(
        ME_PATH, json={"first_name": None, "last_name": None}
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Ana López"
    assert firebase.updated == []
    profile = account_state.users.get_profile(FAMILY_UID)
    assert (profile.first_name, profile.last_name) == ("Ana", "López")


def test_update_profile_blank_first_name_422(firebase, account_state):
    _seed_family_profile(account_state.users)
    _family_user()

    response = TestClient(app).patch(ME_PATH, json={"first_name": "   "})

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: first_name: es demasiado corto"}
    assert firebase.updated == []


def test_update_profile_creates_minimal_profile(firebase, account_state):
    _family_user(email="Ana@Example.com", display_name="Ana María López")

    response = TestClient(app).patch(
        ME_PATH, json={"last_name": "Gómez", "phone": "300"}
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Ana Gómez"
    assert firebase.updated == [
        (FAMILY_UID, {"app": FIREBASE_APP, "display_name": "Ana Gómez"})
    ]
    assert account_state.users.get_profile(FAMILY_UID) == UserProfile(
        uid=FAMILY_UID,
        first_name="Ana",
        last_name="Gómez",
        email="ana@example.com",
        role=Role.FAMILIAR,
        phone="300",
        photo_url="https://example.com/old.png",
    )


def test_update_profile_role_none_updates_firebase_only(firebase, account_state):
    _family_user(role=None, display_name=None)

    response = TestClient(app).patch(ME_PATH, json={"first_name": "Ana", "phone": "300"})

    assert response.status_code == 200
    assert response.json()["role"] is None
    assert response.json()["display_name"] == "Ana"
    assert firebase.updated == [(FAMILY_UID, {"app": FIREBASE_APP, "display_name": "Ana"})]
    assert account_state.users.get_profile(FAMILY_UID) is None


@pytest.mark.parametrize(
    "role", [Role.ADULTO_MAYOR, Role.FAMILIAR, Role.CUIDADOR, Role.ADMINISTRADOR, None]
)
def test_update_profile_allowed_for_every_role(firebase, account_state, role):
    _family_user(role=role)

    response = TestClient(app).patch(ME_PATH, json={"phone": "300"})

    assert response.status_code == 200
    assert response.json()["uid"] == FAMILY_UID


def test_update_profile_user_not_found_404(firebase, account_state):
    _seed_family_profile(account_state.users)
    _family_user()
    firebase.update_error = firebase_auth.UserNotFoundError("mensaje-interno")

    response = TestClient(app).patch(ME_PATH, json={"first_name": "Otra"})

    assert response.status_code == 404
    assert response.json() == {"detail": USER_NOT_FOUND_DETAIL}
    assert account_state.users.get_profile(FAMILY_UID).first_name == "Ana"


def test_update_profile_firebase_value_error_422(firebase, account_state):
    _seed_family_profile(account_state.users)
    _family_user()
    firebase.update_error = ValueError("mensaje-interno")

    response = TestClient(app).patch(
        ME_PATH, json={"photo_url": "https://example.com/new.png"}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": PHOTO_URL_INVALID_DETAIL}
    assert account_state.users.get_profile(FAMILY_UID).photo_url == (
        "https://example.com/old.png"
    )


@pytest.mark.parametrize("failure", ["firebase_error", "missing_credentials"])
def test_update_profile_firebase_unavailable_503(
    monkeypatch, firebase, account_state, failure
):
    _seed_family_profile(account_state.users)
    _family_user()
    if failure == "firebase_error":
        firebase.update_error = firebase_exceptions.UnavailableError("mensaje-interno")
    else:

        def missing_credentials():
            raise FirebaseConfigurationError("mensaje-interno")

        monkeypatch.setattr(auth_service, "get_firebase_app", missing_credentials)

    response = TestClient(app).patch(ME_PATH, json={"first_name": "Otra"})

    assert response.status_code == 503
    assert response.json() == {"detail": auth_router.AUTH_UNAVAILABLE_DETAIL}
    assert "mensaje-interno" not in response.text
    assert account_state.users.get_profile(FAMILY_UID).first_name == "Ana"


def test_update_profile_requires_bearer_401(firebase):
    response = TestClient(app).patch(ME_PATH, json={"first_name": "Ana"})

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    assert firebase.updated == []


def test_update_profile_service_returns_contract_user(firebase):
    users = InMemoryUserRepository()
    _seed_family_profile(users)
    user = AuthenticatedUser(
        uid=FAMILY_UID,
        email="ana@example.com",
        email_verified=False,
        display_name="Ana López",
        role=Role.FAMILIAR,
    )

    updated = auth_service.update_profile(
        user, ProfileUpdateRequest(last_name="Ruiz", phone="555"), users
    )

    assert updated == AuthenticatedUser(
        uid=FAMILY_UID,
        email="ana@example.com",
        email_verified=False,
        display_name="Ana Ruiz",
        photo_url=None,
        role=Role.FAMILIAR,
    )
    assert users.get_profile(FAMILY_UID).phone == "555"


# --- A10: registrar dispositivo ---


def test_register_device_204_and_marks_verified_email(account_state):
    _as_user(FAMILY_UID, Role.FAMILIAR, email="Ana@Example.com", email_verified=True)

    response = TestClient(app).post(DEVICES_PATH, json={"fcm_token": " token-1 "})

    assert response.status_code == 204
    assert response.content == b""
    assert account_state.devices.tokens_of([FAMILY_UID]) == ["token-1"]
    assert account_state.users.find_uid_by_verified_email("ana@example.com") == FAMILY_UID


@pytest.mark.parametrize(
    ("email", "email_verified"),
    [("ana@example.com", False), ("ana@example.com", None), (None, True)],
)
def test_register_device_unverified_email_not_linked(account_state, email, email_verified):
    _as_user(FAMILY_UID, Role.FAMILIAR, email=email, email_verified=email_verified)

    response = TestClient(app).post(DEVICES_PATH, json={"fcm_token": "token-1"})

    assert response.status_code == 204
    assert account_state.devices.has_device(FAMILY_UID) is True
    assert account_state.users.find_uid_by_verified_email("ana@example.com") is None


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"fcm_token": "   "}, "Datos inválidos: fcm_token: es demasiado corto"),
        ({"fcm_token": ""}, "Datos inválidos: fcm_token: es demasiado corto"),
        ({}, "Datos inválidos: fcm_token: es obligatorio"),
    ],
)
def test_register_device_empty_token_422(account_state, body, detail):
    _as_user(FAMILY_UID, Role.FAMILIAR, email="ana@example.com", email_verified=True)

    response = TestClient(app).post(DEVICES_PATH, json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert account_state.devices.has_device(FAMILY_UID) is False
    assert account_state.users.find_uid_by_verified_email("ana@example.com") is None


def test_register_device_requires_bearer_401(account_state):
    response = TestClient(app).post(DEVICES_PATH, json={"fcm_token": "token-1"})

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    assert account_state.devices.tokens_of(["token-1"]) == []


@pytest.mark.parametrize(
    "role", [Role.ADULTO_MAYOR, Role.FAMILIAR, Role.CUIDADOR, Role.ADMINISTRADOR, None]
)
def test_register_device_allowed_for_every_role(account_state, role):
    _as_user("any-uid", role)

    response = TestClient(app).post(DEVICES_PATH, json={"fcm_token": "token-1"})

    assert response.status_code == 204
    assert account_state.devices.tokens_of(["any-uid"]) == ["token-1"]


def test_register_device_token_moves_to_last_user(account_state):
    client = TestClient(app)

    _as_user("first-uid", Role.FAMILIAR)
    client.post(DEVICES_PATH, json={"fcm_token": "shared-token"})
    _as_user("second-uid", Role.CUIDADOR)
    response = client.post(DEVICES_PATH, json={"fcm_token": "shared-token"})

    assert response.status_code == 204
    assert account_state.devices.tokens_of(["first-uid"]) == []
    assert account_state.devices.tokens_of(["second-uid"]) == ["shared-token"]


def test_register_device_service_marks_only_verified_email():
    users = InMemoryUserRepository()
    devices = InMemoryDeviceRepository()

    auth_service.register_device(
        AuthenticatedUser(uid="u1", email="a@example.com", email_verified=True),
        DeviceRegisterRequest(fcm_token="t1"),
        users,
        devices,
    )
    auth_service.register_device(
        AuthenticatedUser(uid="u2", email="b@example.com", email_verified=False),
        DeviceRegisterRequest(fcm_token="t2"),
        users,
        devices,
    )

    assert devices.tokens_of(["u1", "u2"]) == ["t1", "t2"]
    assert users.find_uid_by_verified_email("a@example.com") == "u1"
    assert users.find_uid_by_verified_email("b@example.com") is None


# --- OpenAPI ---


def test_openapi_lists_account_routes():
    paths = app.openapi()["paths"]

    assert set(paths["/api/auth/register"]) == {"post"}
    assert set(paths["/api/auth/password-reset"]) == {"post"}
    assert set(paths["/api/auth/me"]) == {"get", "patch"}
    assert set(paths["/api/auth/adults"]) == {"get", "post"}
    assert set(paths["/api/auth/adults/{adult_id}/caregivers"]) == {"post"}
    assert set(paths["/api/auth/devices"]) == {"post"}
    assert "201" in paths["/api/auth/adults/{adult_id}/caregivers"]["post"]["responses"]
    assert "204" in paths["/api/auth/password-reset"]["post"]["responses"]
    assert "204" in paths["/api/auth/devices"]["post"]["responses"]


# --- Ronda 3 (D-R3-15): compensación en Firebase si falla la persistencia ---


DATABASE_UNAVAILABLE_DETAIL = "El servicio no está disponible. Intenta más tarde."


def _raise_database_unavailable(*args, **kwargs):
    raise OperationalError("x", {}, Exception("x"))


@pytest.mark.parametrize("delete_fails", [False, True], ids=["deleted", "delete_fails"])
def test_register_profile_database_error_503_deletes_new_account(
    web_api_key, firebase, rest, account_state, monkeypatch, delete_fails
):
    if delete_fails:
        # Aunque la compensación falle, sale la excepción original (503 de la base).
        firebase.delete_error = firebase_exceptions.UnavailableError("mensaje-interno")
    monkeypatch.setattr(
        account_state.users, "add_profile", _raise_database_unavailable
    )

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_DETAIL}
    assert firebase.deleted == [NEW_UID]
    assert firebase.events == ["create_user", "set_custom_user_claims", "delete_user"]
    # Ni inicio de sesión ni correo de verificación.
    assert rest.seen == []
    assert account_state.users.get_profile(NEW_UID) is None


@pytest.mark.parametrize("failing_method", ["add_profile", "link_adult"])
def test_create_adult_database_error_503_deletes_new_account(
    firebase, account_state, monkeypatch, failing_method
):
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.next_uid = ADULT_UID
    monkeypatch.setattr(
        account_state.users, failing_method, _raise_database_unavailable
    )

    response = TestClient(app).post(ADULTS_PATH, json=ADULT_BODY)

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_DETAIL}
    assert firebase.deleted == [ADULT_UID]
    assert firebase.events == ["create_user", "set_custom_user_claims", "delete_user"]


@pytest.mark.parametrize("failing_method", ["add_profile", "link_adult"])
def test_create_caregiver_new_database_error_503_deletes_new_account(
    web_api_key, firebase, rest, account_state, monkeypatch, failing_method
):
    _seed_family_link(account_state.users)
    _as_user(FAMILY_UID, Role.FAMILIAR)
    firebase.next_uid = CAREGIVER_UID
    rest.routes[OOB_ROUTE] = _oob_ok
    monkeypatch.setattr(
        account_state.users, failing_method, _raise_database_unavailable
    )

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_DETAIL}
    assert firebase.deleted == [CAREGIVER_UID]
    assert firebase.events == [
        "get_user_by_email",
        "create_user",
        "set_custom_user_claims",
        "delete_user",
    ]
    # Sin cuenta no hay invitación.
    assert rest.seen == []


def test_create_caregiver_existing_database_error_503_never_deletes_account(
    web_api_key, firebase, rest, account_state, monkeypatch
):
    _seed_family_link(account_state.users)
    firebase.existing["luis@example.com"] = (CAREGIVER_UID, {"role": "cuidador"})
    _as_user(FAMILY_UID, Role.FAMILIAR)
    monkeypatch.setattr(
        account_state.users, "link_adult", _raise_database_unavailable
    )

    response = TestClient(app).post(
        CAREGIVERS_PATH.format(adult_id=ADULT_UID), json=CAREGIVER_BODY
    )

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_DETAIL}
    # La cuenta del cuidador ya existía: nunca se borra.
    assert firebase.deleted == []
    assert "delete_user" not in firebase.events
    assert firebase.created == []
    assert rest.seen == []


# =====================================================================================
# Correo verificado obligatorio (D-LL-4) y marca en la base al iniciar sesión (D-LL-5).
# =====================================================================================


LOGIN_PATH = "/api/auth/login"
REFRESH_PATH = "/api/auth/refresh"
LOGIN_BODY = {"email": "user@example.com", "password": "secret123"}
LOOKUP_EMAIL = "user@example.com"
LOOKUP_UID = "firebase-uid"


def _refresh_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id_token": "id-token",
            "refresh_token": "new-refresh-token",
            "expires_in": "3600",
            "user_id": LOOKUP_UID,
        },
    )


def _failing_mark(uid, email):
    raise OperationalError("x", {}, Exception("x"))


async def _sign_in_service(
    routes: dict[tuple[str, str], Handler],
    seen: list[httpx.Request],
    users: InMemoryUserRepository | None = None,
) -> LoginResponse:
    async with httpx.AsyncClient(transport=_firebase_transport(routes, seen)) as client:
        return await auth_service.sign_in_with_email_password(
            LoginRequest(email=LOOKUP_EMAIL, password="secret123"),
            client=client,
            users=users,
        )


async def _refresh_service(
    routes: dict[tuple[str, str], Handler],
    seen: list[httpx.Request],
    users: InMemoryUserRepository | None = None,
) -> LoginResponse:
    async with httpx.AsyncClient(transport=_firebase_transport(routes, seen)) as client:
        return await auth_service.refresh_session(
            RefreshRequest(refresh_token="old-refresh-token"),
            client=client,
            users=users,
        )


def _routes_of(seen: list[httpx.Request]) -> list[tuple[str, str]]:
    return [(request.url.host, request.url.path) for request in seen]


def test_email_not_verified_details_are_fixed_texts():
    assert auth_service.EMAIL_NOT_VERIFIED_LOGIN_DETAIL == (
        "Debes verificar tu correo electrónico. Te enviamos un enlace a tu correo: "
        "ábrelo y después inicia sesión de nuevo."
    )
    assert auth_service.EMAIL_NOT_VERIFIED_DETAIL == (
        "Debes verificar tu correo electrónico antes de continuar. "
        "Inicia sesión de nuevo para recibir el enlace."
    )
    # core.security usa la misma constante (auth_service no importa core.security).
    assert security.EMAIL_NOT_VERIFIED_DETAIL is auth_service.EMAIL_NOT_VERIFIED_DETAIL
    assert issubclass(auth_service.EmailNotVerifiedError, Exception)


# --- A1 (servicio) ---


@pytest.mark.asyncio
@pytest.mark.parametrize("email_verified", [False, None], ids=["false", "absent"])
async def test_sign_in_unverified_email_requests_verify_email_and_raises(
    web_api_key, email_verified, caplog
):
    caplog.set_level(logging.DEBUG, logger="app.services.auth_service")
    seen: list[httpx.Request] = []
    users = InMemoryUserRepository()
    routes = {
        SIGN_IN_ROUTE: _sign_in_response,
        LOOKUP_ROUTE: _lookup_handler(email_verified=email_verified),
        OOB_ROUTE: _oob_ok,
    }

    with pytest.raises(auth_service.EmailNotVerifiedError) as error_info:
        await _sign_in_service(routes, seen, users)

    assert str(error_info.value) == auth_service.EMAIL_NOT_VERIFIED_LOGIN_DETAIL
    assert _routes_of(seen) == [SIGN_IN_ROUTE, LOOKUP_ROUTE, OOB_ROUTE]
    oob_request = seen[2]
    assert oob_request.url.params["key"] == "test-api-key"
    assert json.loads(oob_request.content) == {
        "requestType": "VERIFY_EMAIL",
        "idToken": "id-token",
    }
    # Sin correo verificado no hay marca en la base.
    assert users.find_uid_by_verified_email(LOOKUP_EMAIL) is None
    assert "id-token" not in caplog.text
    assert LOOKUP_EMAIL not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("oob_handler", "logged_error"),
    [
        (_oob_network_error, "AuthProviderUnavailableError"),
        (_firebase_error("TOO_MANY_ATTEMPTS_TRY_LATER"), None),
    ],
    ids=["network_error", "firebase_error"],
)
async def test_sign_in_unverified_email_still_403_when_verify_email_fails(
    web_api_key, caplog, oob_handler, logged_error
):
    caplog.set_level(logging.DEBUG, logger="app.services.auth_service")
    seen: list[httpx.Request] = []
    routes = {
        SIGN_IN_ROUTE: _sign_in_response,
        LOOKUP_ROUTE: _lookup_handler(email_verified=False),
        OOB_ROUTE: oob_handler,
    }

    with pytest.raises(auth_service.EmailNotVerifiedError) as error_info:
        await _sign_in_service(routes, seen)

    assert str(error_info.value) == auth_service.EMAIL_NOT_VERIFIED_LOGIN_DETAIL
    if logged_error is not None:
        assert any(
            record.levelno == logging.WARNING and logged_error in record.getMessage()
            for record in caplog.records
        )
    assert "id-token" not in caplog.text


@pytest.mark.asyncio
async def test_sign_in_unverified_email_closes_own_client(web_api_key, monkeypatch):
    real_async_client = httpx.AsyncClient
    created: list[httpx.AsyncClient] = []
    seen: list[httpx.Request] = []
    routes = {
        SIGN_IN_ROUTE: _sign_in_response,
        LOOKUP_ROUTE: _lookup_handler(email_verified=False),
        OOB_ROUTE: _oob_ok,
    }

    def factory(**kwargs) -> httpx.AsyncClient:
        client = real_async_client(transport=_firebase_transport(routes, seen), **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(auth_service.httpx, "AsyncClient", factory)

    with pytest.raises(auth_service.EmailNotVerifiedError):
        await auth_service.sign_in_with_email_password(
            LoginRequest(email=LOOKUP_EMAIL, password="secret123")
        )

    # El correo de verificación sale por el mismo cliente, que se cierra al final.
    assert _routes_of(seen) == [SIGN_IN_ROUTE, LOOKUP_ROUTE, OOB_ROUTE]
    assert len(created) == 1
    assert created[0].is_closed is True


@pytest.mark.asyncio
async def test_sign_in_verified_email_marks_repository(web_api_key):
    seen: list[httpx.Request] = []
    users = InMemoryUserRepository()
    routes = {
        SIGN_IN_ROUTE: _sign_in_response,
        LOOKUP_ROUTE: _lookup_handler('{"role": "familiar"}'),
    }

    response = await _sign_in_service(routes, seen, users)

    assert response.id_token == "id-token"
    assert response.user.email_verified is True
    # Sin correo de verificación y con la marca hecha (D-LL-5): el contacto con este
    # correo queda vinculado a la cuenta desde el primer inicio de sesión.
    assert _routes_of(seen) == [SIGN_IN_ROUTE, LOOKUP_ROUTE]
    assert users.find_uid_by_verified_email(LOOKUP_EMAIL) == LOOKUP_UID


@pytest.mark.asyncio
async def test_sign_in_verified_email_mark_failure_still_returns_tokens(
    web_api_key, caplog
):
    caplog.set_level(logging.DEBUG, logger="app.services.auth_service")
    users = InMemoryUserRepository()
    users.mark_email_verified = _failing_mark
    routes = {
        SIGN_IN_ROUTE: _sign_in_response,
        LOOKUP_ROUTE: _lookup_handler('{"role": "familiar"}'),
    }

    response = await _sign_in_service(routes, [], users)

    assert response.id_token == "id-token"
    assert response.user.uid == LOOKUP_UID
    assert any(
        record.levelno == logging.ERROR and "OperationalError" in record.getMessage()
        for record in caplog.records
    )
    assert LOOKUP_EMAIL not in caplog.text


@pytest.mark.asyncio
async def test_sign_in_verified_email_without_repository_still_returns_tokens(
    web_api_key,
):
    response = await _sign_in_service(
        {SIGN_IN_ROUTE: _sign_in_response, LOOKUP_ROUTE: _lookup_handler()}, []
    )

    assert response.user.email_verified is True
    assert response.id_token == "id-token"


# --- A3 (servicio) ---


@pytest.mark.asyncio
@pytest.mark.parametrize("email_verified", [False, None], ids=["false", "absent"])
async def test_refresh_session_unverified_email_raises_without_sending_email(
    web_api_key, email_verified
):
    seen: list[httpx.Request] = []
    users = InMemoryUserRepository()
    # Sin ruta sendOobCode: cualquier petición a ella haría fallar el transporte.
    routes = {
        REFRESH_ROUTE: _refresh_ok,
        LOOKUP_ROUTE: _lookup_handler(email_verified=email_verified),
    }

    with pytest.raises(auth_service.EmailNotVerifiedError) as error_info:
        await _refresh_service(routes, seen, users)

    assert str(error_info.value) == auth_service.EMAIL_NOT_VERIFIED_DETAIL
    assert _routes_of(seen) == [REFRESH_ROUTE, LOOKUP_ROUTE]
    assert users.find_uid_by_verified_email(LOOKUP_EMAIL) is None


@pytest.mark.asyncio
async def test_refresh_session_verified_email_marks_repository(web_api_key):
    users = InMemoryUserRepository()
    routes = {
        REFRESH_ROUTE: _refresh_ok,
        LOOKUP_ROUTE: _lookup_handler('{"role": "cuidador"}'),
    }

    response = await _refresh_service(routes, [], users)

    assert response.refresh_token == "new-refresh-token"
    assert users.find_uid_by_verified_email(LOOKUP_EMAIL) == LOOKUP_UID


@pytest.mark.asyncio
async def test_refresh_session_mark_failure_still_returns_tokens(web_api_key, caplog):
    caplog.set_level(logging.DEBUG, logger="app.services.auth_service")
    users = InMemoryUserRepository()
    users.mark_email_verified = _failing_mark
    routes = {
        REFRESH_ROUTE: _refresh_ok,
        LOOKUP_ROUTE: _lookup_handler('{"role": "cuidador"}'),
    }

    response = await _refresh_service(routes, [], users)

    assert response.id_token == "id-token"
    assert "OperationalError" in caplog.text
    assert LOOKUP_EMAIL not in caplog.text


# --- A1 y A3 (rutas) ---


def test_login_route_unverified_email_403_and_requests_verify_email(
    web_api_key, rest, account_state
):
    rest.routes.update(
        {
            SIGN_IN_ROUTE: _sign_in_response,
            LOOKUP_ROUTE: _lookup_handler(email_verified=False),
            OOB_ROUTE: _oob_ok,
        }
    )

    response = TestClient(app).post(LOGIN_PATH, json=LOGIN_BODY)

    assert response.status_code == 403
    assert response.json() == {"detail": auth_service.EMAIL_NOT_VERIFIED_LOGIN_DETAIL}
    assert "id-token" not in response.text
    oob_requests = _requests_to(rest, OOB_ROUTE)
    assert len(oob_requests) == 1
    assert json.loads(oob_requests[0].content) == {
        "requestType": "VERIFY_EMAIL",
        "idToken": "id-token",
    }
    assert account_state.users.find_uid_by_verified_email(LOOKUP_EMAIL) is None
    assert all(client.is_closed for client in rest.clients)


def test_login_route_verified_email_200_and_marks_repository(
    web_api_key, rest, account_state
):
    rest.routes.update(
        {
            SIGN_IN_ROUTE: _sign_in_response,
            LOOKUP_ROUTE: _lookup_handler('{"role": "familiar"}'),
        }
    )

    response = TestClient(app).post(LOGIN_PATH, json=LOGIN_BODY)

    assert response.status_code == 200
    assert response.json()["user"]["email_verified"] is True
    assert response.json()["user"]["role"] == "familiar"
    assert _requests_to(rest, OOB_ROUTE) == []
    assert account_state.users.find_uid_by_verified_email(LOOKUP_EMAIL) == LOOKUP_UID


def test_login_route_mark_failure_still_200(
    web_api_key, rest, account_state, monkeypatch
):
    rest.routes.update(
        {
            SIGN_IN_ROUTE: _sign_in_response,
            LOOKUP_ROUTE: _lookup_handler('{"role": "familiar"}'),
        }
    )
    monkeypatch.setattr(account_state.users, "mark_email_verified", _failing_mark)

    response = TestClient(app).post(LOGIN_PATH, json=LOGIN_BODY)

    assert response.status_code == 200
    assert response.json()["id_token"] == "id-token"


def test_login_route_passes_user_repository(account_state, monkeypatch):
    received: list[dict] = []

    async def fake_sign_in(payload: LoginRequest, **kwargs) -> LoginResponse:
        received.append(kwargs)
        return _login_response(Role.FAMILIAR)

    monkeypatch.setattr(auth_router, "sign_in_with_email_password", fake_sign_in)

    response = TestClient(app).post(LOGIN_PATH, json=LOGIN_BODY)

    assert response.status_code == 200
    assert len(received) == 1
    assert received[0]["users"] is account_state.users


def test_login_route_maps_email_not_verified_to_403(monkeypatch):
    async def fake_sign_in(payload: LoginRequest, **kwargs) -> LoginResponse:
        raise auth_service.EmailNotVerifiedError(
            auth_service.EMAIL_NOT_VERIFIED_LOGIN_DETAIL
        )

    monkeypatch.setattr(auth_router, "sign_in_with_email_password", fake_sign_in)

    response = TestClient(app).post(LOGIN_PATH, json=LOGIN_BODY)

    assert response.status_code == 403
    assert response.json() == {"detail": auth_service.EMAIL_NOT_VERIFIED_LOGIN_DETAIL}


def test_refresh_route_unverified_email_403_without_sending_email(
    web_api_key, rest, account_state
):
    rest.routes.update(
        {
            REFRESH_ROUTE: _refresh_ok,
            LOOKUP_ROUTE: _lookup_handler(email_verified=False),
        }
    )

    response = TestClient(app).post(
        REFRESH_PATH, json={"refresh_token": "old-refresh-token"}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": auth_service.EMAIL_NOT_VERIFIED_DETAIL}
    assert "id-token" not in response.text
    assert _requests_to(rest, OOB_ROUTE) == []
    assert account_state.users.find_uid_by_verified_email(LOOKUP_EMAIL) is None


def test_refresh_route_verified_email_200_and_marks_repository(
    web_api_key, rest, account_state
):
    rest.routes.update(
        {
            REFRESH_ROUTE: _refresh_ok,
            LOOKUP_ROUTE: _lookup_handler('{"role": "cuidador"}'),
        }
    )

    response = TestClient(app).post(
        REFRESH_PATH, json={"refresh_token": "old-refresh-token"}
    )

    assert response.status_code == 200
    assert response.json()["user"]["role"] == "cuidador"
    assert account_state.users.find_uid_by_verified_email(LOOKUP_EMAIL) == LOOKUP_UID


def test_refresh_route_passes_user_repository(account_state, monkeypatch):
    received: list[dict] = []

    async def fake(payload, **kwargs):
        received.append(kwargs)
        return _login_response(Role.FAMILIAR)

    monkeypatch.setattr(auth_router, "refresh_session", fake)

    response = TestClient(app).post(
        REFRESH_PATH, json={"refresh_token": "refresh-token"}
    )

    assert response.status_code == 200
    assert received[0]["users"] is account_state.users


def test_refresh_route_maps_email_not_verified_to_403(monkeypatch):
    async def fake(payload, **kwargs):
        raise auth_service.EmailNotVerifiedError(auth_service.EMAIL_NOT_VERIFIED_DETAIL)

    monkeypatch.setattr(auth_router, "refresh_session", fake)

    response = TestClient(app).post(
        REFRESH_PATH, json={"refresh_token": "refresh-token"}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": auth_service.EMAIL_NOT_VERIFIED_DETAIL}


# --- A4 no cambia: la cuenta nueva sin verificar recibe sus tokens ---


@pytest.mark.parametrize("email_verified", [False, None], ids=["false", "absent"])
def test_register_unverified_new_account_still_200_and_one_verify_email(
    web_api_key, firebase, rest, account_state, email_verified
):
    rest.routes.update(_register_routes())
    rest.routes[LOOKUP_ROUTE] = _lookup_handler(
        '{"role": "familiar"}', email_verified=email_verified
    )

    response = TestClient(app).post(REGISTER_PATH, json=REGISTER_BODY)

    assert response.status_code == 200
    assert response.json()["id_token"] == "id-token"
    assert response.json()["user"]["email_verified"] is email_verified
    assert response.json()["user"]["role"] == "familiar"
    # Un solo correo de verificación: el de A4, no el de A1.
    oob_requests = _requests_to(rest, OOB_ROUTE)
    assert len(oob_requests) == 1
    assert json.loads(oob_requests[0].content) == {
        "requestType": "VERIFY_EMAIL",
        "idToken": "id-token",
    }
    assert firebase.events == [
        "create_user",
        "set_custom_user_claims",
        SIGN_IN_ROUTE[1],
        LOOKUP_ROUTE[1],
        OOB_ROUTE[1],
    ]
    assert account_state.users.find_uid_by_verified_email("ana@example.com") is None
    assert account_state.users.find_uid_by_verified_email(LOOKUP_EMAIL) is None


# --- A2 y rutas con Bearer: get_current_user exige el correo verificado ---


@pytest.mark.parametrize("email_verified", [False, None], ids=["false", "absent"])
def test_me_route_unverified_email_403(monkeypatch, email_verified):
    claims = {"uid": "firebase-uid", "role": "familiar", "email": LOOKUP_EMAIL}
    if email_verified is not None:
        claims["email_verified"] = email_verified
    monkeypatch.setattr(auth_service, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth_service.firebase_auth,
        "verify_id_token",
        lambda token, app, check_revoked: claims,
    )

    response = TestClient(app).get(
        ME_PATH, headers={"Authorization": "Bearer valid-token"}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": auth_service.EMAIL_NOT_VERIFIED_DETAIL}


def test_register_device_unverified_email_403_and_no_token_saved(
    monkeypatch, account_state
):
    monkeypatch.setattr(
        security,
        "verify_firebase_id_token",
        lambda token: AuthenticatedUser(
            uid=FAMILY_UID,
            email=LOOKUP_EMAIL,
            email_verified=False,
            role=Role.FAMILIAR,
        ),
    )

    response = TestClient(app).post(
        DEVICES_PATH,
        json={"fcm_token": "fcm-token"},
        headers={"Authorization": "Bearer valid-token"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": auth_service.EMAIL_NOT_VERIFIED_DETAIL}
    assert account_state.devices.has_device(FAMILY_UID) is False
