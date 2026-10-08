from collections.abc import Mapping
from typing import Any

import httpx
from firebase_admin import auth as firebase_auth
from firebase_admin.exceptions import FirebaseError

from app.core.config import get_settings
from app.core.firebase import FirebaseConfigurationError, get_firebase_app
from app.models.auth import AuthenticatedUser, LoginRequest, LoginResponse


FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)


class AuthServiceError(Exception):
    pass


class InvalidCredentialsError(AuthServiceError):
    pass


class InvalidTokenError(AuthServiceError):
    pass


class UserDisabledError(AuthServiceError):
    pass


class TooManyAttemptsError(AuthServiceError):
    pass


class AuthMethodDisabledError(AuthServiceError):
    pass


class AuthProviderUnavailableError(AuthServiceError):
    pass


async def sign_in_with_email_password(
    login: LoginRequest,
    client: httpx.AsyncClient | None = None,
) -> LoginResponse:
    web_api_key = get_settings().firebase_web_api_key.strip()
    if not web_api_key:
        raise FirebaseConfigurationError("FIREBASE_WEB_API_KEY no está configurado.")

    request_client = client or httpx.AsyncClient(timeout=10.0)
    should_close_client = client is None

    try:
        response = await request_client.post(
            FIREBASE_SIGN_IN_URL,
            params={"key": web_api_key},
            json={
                "email": login.email,
                "password": login.password,
                "returnSecureToken": True,
            },
        )
    except httpx.RequestError as error:
        raise AuthProviderUnavailableError(
            "Firebase Authentication no está disponible temporalmente."
        ) from error
    finally:
        if should_close_client:
            await request_client.aclose()

    response_data = _read_json(response)
    if response.is_error:
        _raise_firebase_sign_in_error(response_data)

    try:
        return LoginResponse(
            id_token=str(response_data["idToken"]),
            refresh_token=str(response_data["refreshToken"]),
            expires_in=int(response_data["expiresIn"]),
            user=AuthenticatedUser(
                uid=str(response_data["localId"]),
                email=response_data.get("email"),
                email_verified=response_data.get("emailVerified"),
                display_name=response_data.get("displayName"),
                photo_url=response_data.get("photoUrl"),
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AuthProviderUnavailableError(
            "Firebase devolvió una respuesta de autenticación inválida."
        ) from error


def verify_firebase_id_token(id_token: str) -> AuthenticatedUser:
    try:
        decoded_token = firebase_auth.verify_id_token(
            id_token,
            app=get_firebase_app(),
            check_revoked=True,
        )
    except firebase_auth.UserDisabledError as error:
        raise UserDisabledError("La cuenta está deshabilitada.") from error
    except (
        firebase_auth.ExpiredIdTokenError,
        firebase_auth.RevokedIdTokenError,
        firebase_auth.InvalidIdTokenError,
    ) as error:
        raise InvalidTokenError("El token de autenticación no es válido.") from error
    except FirebaseError as error:
        raise AuthProviderUnavailableError(
            "No fue posible validar el token con Firebase."
        ) from error

    uid = decoded_token.get("uid") or decoded_token.get("sub")
    if not isinstance(uid, str) or not uid:
        raise InvalidTokenError("El token de autenticación no contiene un usuario válido.")

    return AuthenticatedUser(
        uid=uid,
        email=_optional_string(decoded_token, "email"),
        email_verified=_optional_bool(decoded_token, "email_verified"),
        display_name=_optional_string(decoded_token, "name"),
        photo_url=_optional_string(decoded_token, "picture"),
    )


def _read_json(response: httpx.Response) -> dict[str, Any]:
    try:
        response_data = response.json()
    except ValueError as error:
        raise AuthProviderUnavailableError(
            "Firebase devolvió una respuesta inválida."
        ) from error

    if not isinstance(response_data, dict):
        raise AuthProviderUnavailableError("Firebase devolvió una respuesta inválida.")
    return response_data


def _raise_firebase_sign_in_error(response_data: Mapping[str, Any]) -> None:
    error_data = response_data.get("error")
    error_message = error_data.get("message", "") if isinstance(error_data, dict) else ""
    error_code = str(error_message).split(":", maxsplit=1)[0].strip()

    if error_code in {
        "EMAIL_NOT_FOUND",
        "INVALID_EMAIL",
        "INVALID_LOGIN_CREDENTIALS",
        "INVALID_PASSWORD",
        "MISSING_PASSWORD",
    }:
        raise InvalidCredentialsError("Correo o contraseña incorrectos.")
    if error_code == "USER_DISABLED":
        raise UserDisabledError("La cuenta está deshabilitada.")
    if error_code == "TOO_MANY_ATTEMPTS_TRY_LATER":
        raise TooManyAttemptsError(
            "Demasiados intentos. Intenta nuevamente más tarde."
        )
    if error_code == "OPERATION_NOT_ALLOWED":
        raise AuthMethodDisabledError(
            "El inicio de sesión con correo y contraseña no está habilitado en Firebase."
        )

    raise AuthProviderUnavailableError(
        "Firebase Authentication no pudo procesar el inicio de sesión."
    )


def _optional_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _optional_bool(data: Mapping[str, Any], key: str) -> bool | None:
    value = data.get(key)
    return value if isinstance(value, bool) else None
