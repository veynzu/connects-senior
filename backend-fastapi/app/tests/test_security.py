import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from app.api import auth_router
from app.core import security
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.repositories.user_repository import InMemoryUserRepository


def _build_app() -> FastAPI:
    test_app = FastAPI()

    @test_app.get("/familiar", response_model=AuthenticatedUser)
    def familiar_route(
        user: AuthenticatedUser = Depends(security.require_roles(Role.FAMILIAR)),
    ) -> AuthenticatedUser:
        return user

    @test_app.get("/familiar-o-cuidador", response_model=AuthenticatedUser)
    def familiar_or_caregiver_route(
        user: AuthenticatedUser = Depends(
            security.require_roles(Role.FAMILIAR, Role.CUIDADOR)
        ),
    ) -> AuthenticatedUser:
        return user

    return test_app


@pytest.fixture
def security_app():
    test_app = _build_app()
    try:
        yield test_app
    finally:
        test_app.dependency_overrides.clear()


def _as_user(test_app: FastAPI, role: Role | None) -> None:
    test_app.dependency_overrides[security.get_current_user] = lambda: AuthenticatedUser(
        uid="firebase-uid",
        email="user@example.com",
        role=role,
    )


def test_require_roles_allows_listed_role(security_app):
    _as_user(security_app, Role.FAMILIAR)

    response = TestClient(security_app).get("/familiar")

    assert response.status_code == 200
    assert response.json()["uid"] == "firebase-uid"
    assert response.json()["role"] == "familiar"


def test_require_roles_allows_any_listed_role(security_app):
    client = TestClient(security_app)

    for role in (Role.CUIDADOR, Role.FAMILIAR):
        _as_user(security_app, role)

        response = client.get("/familiar-o-cuidador")

        assert response.status_code == 200
        assert response.json()["role"] == role.value


@pytest.mark.parametrize(
    "role",
    [Role.ADULTO_MAYOR, Role.CUIDADOR, Role.ADMINISTRADOR],
)
def test_require_roles_rejects_other_role(security_app, role):
    _as_user(security_app, role)

    response = TestClient(security_app).get("/familiar")

    assert response.status_code == 403
    assert response.json() == {"detail": security.FORBIDDEN_DETAIL}
    assert security.FORBIDDEN_DETAIL == "No tienes permiso para esta acción."


def test_require_roles_rejects_missing_role(security_app):
    _as_user(security_app, None)
    client = TestClient(security_app)

    for path in ("/familiar", "/familiar-o-cuidador"):
        response = client.get(path)

        assert response.status_code == 403
        assert response.json() == {"detail": security.FORBIDDEN_DETAIL}


def test_require_roles_requires_bearer(security_app):
    response = TestClient(security_app).get("/familiar")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Debes enviar un token Bearer."}


def test_auth_router_reexports_security_dependency():
    assert auth_router.get_current_user is security.get_current_user
    assert auth_router.AUTH_UNAVAILABLE_DETAIL is security.AUTH_UNAVAILABLE_DETAIL


ADULT_UID = "adult-uid"
FOREIGN_ADULT_UID = "foreign-adult-uid"
ADULT_ID_REQUIRED_DETAIL = "Datos inválidos: adult_id: es obligatorio"


def _user(uid: str, role: Role | None) -> AuthenticatedUser:
    return AuthenticatedUser(uid=uid, email=f"{uid}@example.com", role=role)


@pytest.mark.parametrize("adult_id", [None, "", "   "])
def test_resolve_adult_scope_adult_without_adult_id(adult_id):
    adult = _user(ADULT_UID, Role.ADULTO_MAYOR)

    scope = security.resolve_adult_scope(adult, adult_id, InMemoryUserRepository())

    assert scope == ADULT_UID


@pytest.mark.parametrize("adult_id", [ADULT_UID, f"  {ADULT_UID}  "])
def test_resolve_adult_scope_adult_with_own_adult_id(adult_id):
    adult = _user(ADULT_UID, Role.ADULTO_MAYOR)

    scope = security.resolve_adult_scope(adult, adult_id, InMemoryUserRepository())

    assert scope == ADULT_UID


def test_resolve_adult_scope_adult_with_foreign_adult_id_is_403():
    adult = _user(ADULT_UID, Role.ADULTO_MAYOR)

    with pytest.raises(HTTPException) as exc_info:
        security.resolve_adult_scope(adult, FOREIGN_ADULT_UID, InMemoryUserRepository())

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == security.FORBIDDEN_DETAIL


@pytest.mark.parametrize("adult_id", [None, "", "   "])
@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_resolve_adult_scope_requires_adult_id_for_family(role, adult_id):
    users = InMemoryUserRepository()
    users.link_adult("linked-uid", ADULT_UID, role)

    with pytest.raises(HTTPException) as exc_info:
        security.resolve_adult_scope(_user("linked-uid", role), adult_id, users)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == ADULT_ID_REQUIRED_DETAIL
    assert security.ADULT_ID_REQUIRED_DETAIL == ADULT_ID_REQUIRED_DETAIL


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_resolve_adult_scope_linked_family_and_caregiver(role):
    users = InMemoryUserRepository()
    users.link_adult("linked-uid", ADULT_UID, role)
    user = _user("linked-uid", role)

    assert security.resolve_adult_scope(user, ADULT_UID, users) == ADULT_UID
    assert security.resolve_adult_scope(user, f"  {ADULT_UID} ", users) == ADULT_UID


@pytest.mark.parametrize(
    ("user_uid", "adult_id"),
    [
        ("linked-uid", FOREIGN_ADULT_UID),
        ("stranger-uid", ADULT_UID),
    ],
)
@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_resolve_adult_scope_unlinked_is_403(role, user_uid, adult_id):
    users = InMemoryUserRepository()
    users.link_adult("linked-uid", ADULT_UID, role)
    users.link_adult("other-uid", FOREIGN_ADULT_UID, role)

    with pytest.raises(HTTPException) as exc_info:
        security.resolve_adult_scope(_user(user_uid, role), adult_id, users)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == security.FORBIDDEN_DETAIL


@pytest.mark.parametrize("adult_id", [None, ADULT_UID])
@pytest.mark.parametrize("role", [Role.ADMINISTRADOR, None])
def test_resolve_adult_scope_rejects_admin_and_missing_role(role, adult_id):
    users = InMemoryUserRepository()
    users.link_adult("someone-uid", ADULT_UID, Role.FAMILIAR)

    with pytest.raises(HTTPException) as exc_info:
        security.resolve_adult_scope(_user("someone-uid", role), adult_id, users)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == security.FORBIDDEN_DETAIL


# --- can_access_adult (D-R2-7, paso 1) ---


def test_can_access_adult_adult_only_self():
    users = InMemoryUserRepository()
    users.link_adult(ADULT_UID, FOREIGN_ADULT_UID, Role.FAMILIAR)
    adult = _user(ADULT_UID, Role.ADULTO_MAYOR)

    assert security.can_access_adult(adult, ADULT_UID, users) is True
    assert security.can_access_adult(adult, FOREIGN_ADULT_UID, users) is False


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_can_access_adult_family_and_caregiver_only_linked(role):
    users = InMemoryUserRepository()
    users.link_adult("linked-uid", ADULT_UID, role)
    users.link_adult("other-uid", FOREIGN_ADULT_UID, role)
    linked = _user("linked-uid", role)

    assert security.can_access_adult(linked, ADULT_UID, users) is True
    assert security.can_access_adult(linked, FOREIGN_ADULT_UID, users) is False
    assert security.can_access_adult(linked, "linked-uid", users) is False
    assert (
        security.can_access_adult(_user("stranger-uid", role), ADULT_UID, users)
        is False
    )


def test_can_access_adult_caregiver_without_permissions_is_still_linked():
    users = InMemoryUserRepository()
    users.link_adult("caregiver-uid", ADULT_UID, Role.CUIDADOR)

    caregiver = _user("caregiver-uid", Role.CUIDADOR)

    assert security.can_access_adult(caregiver, ADULT_UID, users) is True


@pytest.mark.parametrize("role", [Role.ADMINISTRADOR, None])
def test_can_access_adult_rejects_admin_and_missing_role(role):
    users = InMemoryUserRepository()
    users.link_adult("someone-uid", ADULT_UID, Role.FAMILIAR)

    assert (
        security.can_access_adult(_user("someone-uid", role), ADULT_UID, users)
        is False
    )
    assert security.can_access_adult(_user(ADULT_UID, role), ADULT_UID, users) is False


# --- get_current_user: correo verificado obligatorio (D-LL-4) ---


def _bearer(token: str = "valid-token") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _verified_user(email_verified: bool | None) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid="firebase-uid",
        email="user@example.com",
        email_verified=email_verified,
        role=Role.FAMILIAR,
    )


def test_email_not_verified_detail_is_fixed_text():
    assert security.EMAIL_NOT_VERIFIED_DETAIL == (
        "Debes verificar tu correo electrónico antes de continuar. "
        "Inicia sesión de nuevo para recibir el enlace."
    )


@pytest.mark.parametrize("email_verified", [False, None], ids=["false", "absent"])
def test_get_current_user_rejects_unverified_email(monkeypatch, email_verified):
    monkeypatch.setattr(
        security,
        "verify_firebase_id_token",
        lambda token: _verified_user(email_verified),
    )

    with pytest.raises(HTTPException) as exc_info:
        security.get_current_user(_bearer())

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == security.EMAIL_NOT_VERIFIED_DETAIL


def test_get_current_user_accepts_verified_email(monkeypatch):
    received: list[str] = []

    def fake_verify(token: str) -> AuthenticatedUser:
        received.append(token)
        return _verified_user(True)

    monkeypatch.setattr(security, "verify_firebase_id_token", fake_verify)

    user = security.get_current_user(_bearer("token-ok"))

    assert user == _verified_user(True)
    assert received == ["token-ok"]


@pytest.mark.parametrize("email_verified", [False, None], ids=["false", "absent"])
def test_require_roles_route_unverified_email_403_before_role_check(
    security_app, monkeypatch, email_verified
):
    monkeypatch.setattr(
        security,
        "verify_firebase_id_token",
        lambda token: _verified_user(email_verified),
    )

    response = TestClient(security_app).get(
        "/familiar", headers={"Authorization": "Bearer valid-token"}
    )

    # El rol es correcto, pero sin correo verificado no se entra (no es el 403 de rol).
    assert response.status_code == 403
    assert response.json() == {"detail": security.EMAIL_NOT_VERIFIED_DETAIL}


def test_require_roles_route_verified_email_200(security_app, monkeypatch):
    monkeypatch.setattr(
        security,
        "verify_firebase_id_token",
        lambda token: _verified_user(True),
    )

    response = TestClient(security_app).get(
        "/familiar", headers={"Authorization": "Bearer valid-token"}
    )

    assert response.status_code == 200
    assert response.json()["email_verified"] is True
