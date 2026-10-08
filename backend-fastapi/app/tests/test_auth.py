import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import auth_router
from app.main import app
from app.models.auth import AuthenticatedUser, LoginRequest, LoginResponse
from app.services import auth_service


@pytest.mark.asyncio
async def test_sign_in_with_email_password_returns_firebase_tokens(monkeypatch):
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(firebase_web_api_key="test-api-key"),
    )

    def firebase_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "test-api-key"
        assert json.loads(request.content) == {
            "email": "user@example.com",
            "password": "secret123",
            "returnSecureToken": True,
        }
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

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(firebase_handler)
    ) as client:
        response = await auth_service.sign_in_with_email_password(
            LoginRequest(email="user@example.com", password="secret123"),
            client=client,
        )

    assert response.id_token == "id-token"
    assert response.refresh_token == "refresh-token"
    assert response.expires_in == 3600
    assert response.user.uid == "firebase-uid"


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
    async def fake_sign_in(payload: LoginRequest) -> LoginResponse:
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
    monkeypatch.setattr(
        auth_router,
        "verify_firebase_id_token",
        lambda token: AuthenticatedUser(uid="firebase-uid", email="user@example.com"),
    )

    response = TestClient(app).get(
        "/api/auth/me",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert response.status_code == 200
    assert response.json()["uid"] == "firebase-uid"
