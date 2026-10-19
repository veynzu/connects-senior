import json
import logging
import re
import secrets
from collections.abc import Mapping
from typing import Any, NoReturn

import httpx
from firebase_admin import auth as firebase_auth
from firebase_admin.exceptions import FirebaseError
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.firebase import FirebaseConfigurationError, get_firebase_app
from app.models.auth import (
    AdultCreateRequest,
    AdultResponse,
    AuthenticatedUser,
    CaregiverCreateRequest,
    CaregiverResponse,
    DeviceRegisterRequest,
    LoginRequest,
    LoginResponse,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
)
from app.models.enums import Role
from app.models.users import UserProfile, normalize_email
from app.repositories.device_repository import DeviceRepository
from app.repositories.user_repository import UserRepository


FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)
FIREBASE_LOOKUP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:lookup"
FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
FIREBASE_OOB_URL = "https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode"

# Tamaño en bytes de la contraseña temporal del cuidador invitado (A9, P5): nunca se
# guarda, se registra en el log ni se devuelve.
TEMPORARY_PASSWORD_BYTES = 32
_UNKNOWN_FIREBASE_CODE = "desconocido"
# Un código de error de Firebase solo se registra si tiene la forma EMAIL_NOT_FOUND.
_FIREBASE_CODE_PATTERN = re.compile(r"[A-Z0-9_]{1,100}")
_PROFILE_NAME_FIELDS = ("first_name", "last_name")
_PROFILE_REPOSITORY_FIELDS = ("first_name", "last_name", "phone", "photo_url")

logger = logging.getLogger(__name__)


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


class EmailAlreadyExistsError(AuthServiceError):
    pass


class WeakPasswordError(AuthServiceError):
    pass


class AdultNotFoundError(AuthServiceError):
    pass


class UserProfileNotFoundError(AuthServiceError):
    pass


class InvalidProfileDataError(AuthServiceError):
    pass


class AccountPermissionDeniedError(AuthServiceError):
    """El rol del usuario no permite la operación de cuentas (defensa en profundidad)."""


async def sign_in_with_email_password(
    login: LoginRequest,
    client: httpx.AsyncClient | None = None,
) -> LoginResponse:
    web_api_key = _get_web_api_key()

    request_client = client if client is not None else httpx.AsyncClient(timeout=10.0)
    should_close_client = client is None

    try:
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

        response_data = _read_json(response)
        if response.is_error:
            _raise_firebase_sign_in_error(response_data)

        try:
            id_token = str(response_data["idToken"])
            refresh_token = str(response_data["refreshToken"])
            expires_in = int(response_data["expiresIn"])
            local_id = str(response_data["localId"])
        except (KeyError, TypeError, ValueError) as error:
            raise AuthProviderUnavailableError(
                "Firebase devolvió una respuesta de autenticación inválida."
            ) from error

        user = await _lookup_user(id_token, request_client)
    finally:
        if should_close_client:
            await request_client.aclose()

    if user.uid != local_id:
        raise AuthProviderUnavailableError(
            "Firebase devolvió una respuesta de usuario inválida."
        )

    return LoginResponse(
        id_token=id_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=user,
    )


async def refresh_session(
    payload: RefreshRequest,
    client: httpx.AsyncClient | None = None,
) -> LoginResponse:
    web_api_key = _get_web_api_key()

    request_client = client if client is not None else httpx.AsyncClient(timeout=10.0)
    should_close_client = client is None

    try:
        try:
            response = await request_client.post(
                FIREBASE_REFRESH_URL,
                params={"key": web_api_key},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": payload.refresh_token,
                },
            )
        except httpx.RequestError as error:
            raise AuthProviderUnavailableError(
                "Firebase Authentication no está disponible temporalmente."
            ) from error

        response_data = _read_json(response)
        if response.is_error:
            _raise_firebase_refresh_error(response_data)

        try:
            id_token = _required_string(response_data, "id_token")
            refresh_token = _required_string(response_data, "refresh_token")
            expires_in = int(response_data["expires_in"])
        except (KeyError, TypeError, ValueError) as error:
            raise AuthProviderUnavailableError(
                "Firebase devolvió una respuesta de autenticación inválida."
            ) from error

        user = await _lookup_user(id_token, request_client)
    finally:
        if should_close_client:
            await request_client.aclose()

    if response_data.get("user_id") != user.uid:
        raise AuthProviderUnavailableError(
            "Firebase devolvió una respuesta de usuario inválida."
        )

    return LoginResponse(
        id_token=id_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=user,
    )


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
        role=_parse_role(decoded_token.get("role"), uid),
    )


async def _lookup_user(id_token: str, client: httpx.AsyncClient) -> AuthenticatedUser:
    try:
        response = await client.post(
            FIREBASE_LOOKUP_URL,
            params={"key": _get_web_api_key()},
            json={"idToken": id_token},
        )
    except httpx.RequestError as error:
        raise AuthProviderUnavailableError(
            "Firebase Authentication no está disponible temporalmente."
        ) from error

    response_data = _read_json(response)
    users = response_data.get("users")
    user_data = users[0] if isinstance(users, list) and users else None
    local_id = user_data.get("localId") if isinstance(user_data, dict) else None

    if response.is_error or not isinstance(local_id, str) or not local_id:
        raise AuthProviderUnavailableError(
            "Firebase devolvió una respuesta de usuario inválida."
        )

    return AuthenticatedUser(
        uid=local_id,
        email=_optional_string(user_data, "email"),
        email_verified=_optional_bool(user_data, "emailVerified"),
        display_name=_optional_string(user_data, "displayName"),
        photo_url=_optional_string(user_data, "photoUrl"),
        role=_role_from_custom_attributes(user_data.get("customAttributes"), local_id),
    )


def _parse_role(value: object, uid: str) -> Role | None:
    if value is None:
        return None
    if isinstance(value, str) and value in Role:
        return Role(value)

    logger.warning("Claim role no reconocido para el usuario %s", uid)
    return None


def _role_from_custom_attributes(custom_attributes: object, uid: str) -> Role | None:
    if custom_attributes is None or custom_attributes == "":
        return None

    data: object = None
    if isinstance(custom_attributes, str):
        try:
            data = json.loads(custom_attributes)
        except ValueError:
            data = None

    if not isinstance(data, dict):
        logger.warning("customAttributes ilegible para el usuario %s", uid)
        return None
    return _parse_role(data.get("role"), uid)


def _get_web_api_key() -> str:
    web_api_key = get_settings().firebase_web_api_key.strip()
    if not web_api_key:
        raise FirebaseConfigurationError("La clave web de Firebase no está configurada.")
    return web_api_key


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


def _raise_firebase_refresh_error(response_data: Mapping[str, Any]) -> NoReturn:
    error_data = response_data.get("error")
    error_message = error_data.get("message", "") if isinstance(error_data, dict) else ""
    error_code = str(error_message).split(":", 1)[0].strip()

    if error_code in {
        "TOKEN_EXPIRED",
        "INVALID_REFRESH_TOKEN",
        "USER_NOT_FOUND",
        "MISSING_REFRESH_TOKEN",
    }:
        raise InvalidTokenError("La sesión expiró. Inicia sesión de nuevo.")
    if error_code == "USER_DISABLED":
        raise UserDisabledError("La cuenta está deshabilitada.")

    raise AuthProviderUnavailableError(
        "Firebase Authentication no pudo renovar la sesión."
    )


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ValueError(key)
    return value


def _optional_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _optional_bool(data: Mapping[str, Any], key: str) -> bool | None:
    value = data.get(key)
    return value if isinstance(value, bool) else None


# --- Tareas 5 y 8: cuentas (A4, A5, A6, A7, A8, A9, A10) ---


def create_firebase_user(email: str, password: str, display_name: str, role: Role) -> str:
    """Crea la cuenta en Firebase con su claim role y devuelve el uid (D-R2-14, D-02.3).

    El claim se fija antes de cualquier inicio de sesión para que el primer id_token ya lo
    traiga. Si el claim falla, se intenta borrar la cuenta: nunca queda una cuenta sin rol.
    administrador nunca se asigna por API (D-02.4). Síncrona y con red: desde código async
    se llama con run_in_threadpool (regla 4.3).
    """
    account_role = Role(role)
    if account_role == Role.ADMINISTRADOR:
        raise ValueError("El rol administrador no se asigna por API.")

    firebase_app = get_firebase_app()
    try:
        user_record = firebase_auth.create_user(
            email=email,
            password=password,
            display_name=display_name,
            app=firebase_app,
        )
    except firebase_auth.EmailAlreadyExistsError as error:
        raise EmailAlreadyExistsError("El correo ya está registrado.") from error
    except ValueError as error:
        raise WeakPasswordError(
            "La contraseña debe tener al menos 6 caracteres."
        ) from error
    except FirebaseError as error:
        raise AuthProviderUnavailableError("Firebase no pudo crear la cuenta.") from error

    uid = str(user_record.uid)
    try:
        firebase_auth.set_custom_user_claims(
            uid,
            {"role": account_role.value},
            app=firebase_app,
        )
    except (FirebaseError, ValueError) as error:
        _delete_firebase_user_quietly(uid, firebase_app)
        raise AuthProviderUnavailableError(
            "Firebase no pudo asignar el rol a la cuenta."
        ) from error
    return uid


def find_firebase_user_by_email(email: str) -> tuple[str, Role | None] | None:
    """Busca una cuenta por correo: (uid, rol del claim) o None si no existe."""
    firebase_app = get_firebase_app()
    try:
        user_record = firebase_auth.get_user_by_email(email, app=firebase_app)
    except firebase_auth.UserNotFoundError:
        return None
    except (FirebaseError, ValueError) as error:
        raise AuthProviderUnavailableError(
            "Firebase no pudo consultar la cuenta."
        ) from error

    uid = str(user_record.uid)
    custom_claims = user_record.custom_claims
    role_claim = custom_claims.get("role") if isinstance(custom_claims, dict) else None
    return uid, _parse_role(role_claim, uid)


async def send_oob_code(
    request_type: str,
    *,
    email: str | None = None,
    id_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Pide a Firebase un correo de acción (PASSWORD_RESET o VERIFY_EMAIL).

    Cualquier respuesta de Firebase, de error o no, termina sin excepción: solo se registra
    el código (nunca el correo ni el token). Así A5 responde 204 exista o no la cuenta y
    RESET_PASSWORD_EXCEED_LIMIT no revela nada (P14). Sin clave web o sin red: excepción.
    """
    web_api_key = _get_web_api_key()
    body: dict[str, str] = {"requestType": request_type}
    if email is not None:
        body["email"] = email
    if id_token is not None:
        body["idToken"] = id_token

    request_client = client if client is not None else httpx.AsyncClient(timeout=10.0)
    should_close_client = client is None

    try:
        try:
            response = await request_client.post(
                FIREBASE_OOB_URL,
                params={"key": web_api_key},
                json=body,
            )
        except httpx.RequestError as error:
            raise AuthProviderUnavailableError(
                "Firebase Authentication no está disponible temporalmente."
            ) from error

        if response.is_error:
            logger.info(
                "Firebase no envió el correo %s (código %s)",
                request_type,
                _firebase_error_code(response),
            )
        else:
            logger.info("Firebase aceptó el envío del correo %s", request_type)
    finally:
        if should_close_client:
            await request_client.aclose()


async def send_password_reset_email(
    email: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    """A5: correo de restablecimiento con la plantilla de Firebase (D-01)."""
    await send_oob_code("PASSWORD_RESET", email=email, client=client)


async def register_family(
    payload: RegisterRequest,
    users: UserRepository,
    client: httpx.AsyncClient | None = None,
) -> LoginResponse:
    """A4: crea un familiar (claim antes del inicio de sesión) y devuelve lo mismo que A1."""
    # Sin clave web no se podría iniciar sesión: se falla antes de crear nada.
    _get_web_api_key()

    uid = await run_in_threadpool(
        create_firebase_user,
        payload.email,
        payload.password,
        _display_name(payload.first_name, payload.last_name),
        Role.FAMILIAR,
    )
    users.add_profile(
        UserProfile(
            uid=uid,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            role=Role.FAMILIAR,
        )
    )

    request_client = client if client is not None else httpx.AsyncClient(timeout=10.0)
    should_close_client = client is None
    try:
        login = await sign_in_with_email_password(
            LoginRequest(email=payload.email, password=payload.password),
            request_client,
        )
        # Intento sin garantía (D-R2-3, P9): sin correo verificado no hay vínculo
        # contacto -> usuario, pero el registro no falla por eso.
        try:
            await send_oob_code(
                "VERIFY_EMAIL",
                id_token=login.id_token,
                client=request_client,
            )
        except (AuthProviderUnavailableError, FirebaseConfigurationError) as error:
            logger.warning(
                "No se pudo pedir el correo de verificación (%s)",
                type(error).__name__,
            )
    finally:
        if should_close_client:
            await request_client.aclose()

    return login


def create_adult(
    family: AuthenticatedUser,
    payload: AdultCreateRequest,
    users: UserRepository,
) -> AdultResponse:
    """A7: el familiar crea la cuenta del adulto mayor y queda como su responsable."""
    _require_role(family, Role.FAMILIAR)

    uid = create_firebase_user(
        payload.email,
        payload.password,
        _display_name(payload.first_name, payload.last_name),
        Role.ADULTO_MAYOR,
    )
    profile = users.add_profile(
        UserProfile(
            uid=uid,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            role=Role.ADULTO_MAYOR,
            phone=payload.phone,
            birth_date=payload.birth_date,
            address=payload.address,
            photo_url=payload.photo_url,
        )
    )
    users.link_adult(family.uid, uid, Role.FAMILIAR)
    return _adult_response(profile)


def list_adults(user: AuthenticatedUser, users: UserRepository) -> list[AdultResponse]:
    """A8: adultos vinculados al familiar o cuidador; un adulto sin perfil se omite."""
    _require_role(user, Role.FAMILIAR, Role.CUIDADOR)

    adults: list[AdultResponse] = []
    for adult_uid in users.adults_of(user.uid):
        profile = users.get_profile(adult_uid)
        if profile is not None:
            adults.append(_adult_response(profile))
    return adults


async def create_caregiver(
    family: AuthenticatedUser,
    adult_id: str,
    payload: CaregiverCreateRequest,
    users: UserRepository,
    client: httpx.AsyncClient | None = None,
) -> CaregiverResponse:
    """A9 (P5): invita a un cuidador para un adulto del que el familiar es responsable.

    - Correo de un cuidador existente: solo vínculo con los permisos del payload.
    - Correo existente con otro rol o sin rol: EmailAlreadyExistsError.
    - Correo nuevo: cuenta con contraseña temporal aleatoria, claim cuidador, vínculo y
      correo de restablecimiento de Firebase para que el cuidador fije la suya.
    """
    _require_role(family, Role.FAMILIAR)

    link = users.link_of(family.uid, adult_id)
    if link is None or link.role != Role.FAMILIAR:
        raise AdultNotFoundError("El adulto mayor no existe.")

    existing = await run_in_threadpool(find_firebase_user_by_email, payload.email)
    if existing is not None:
        caregiver_uid, caregiver_role = existing
        if caregiver_role != Role.CUIDADOR:
            raise EmailAlreadyExistsError("El correo ya está registrado.")
        profile = users.get_profile(caregiver_uid)
        if profile is None:
            profile = users.add_profile(_caregiver_profile(caregiver_uid, payload))
        users.link_adult(caregiver_uid, adult_id, Role.CUIDADOR, payload.permissions)
        return _caregiver_response(profile, adult_id, payload, users)

    caregiver_uid = await run_in_threadpool(
        create_firebase_user,
        payload.email,
        secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES),
        _display_name(payload.first_name, payload.last_name),
        Role.CUIDADOR,
    )
    profile = users.add_profile(_caregiver_profile(caregiver_uid, payload))
    users.link_adult(caregiver_uid, adult_id, Role.CUIDADOR, payload.permissions)

    try:
        await send_password_reset_email(payload.email, client=client)
    except (AuthProviderUnavailableError, FirebaseConfigurationError) as error:
        logger.warning(
            "No se pudo pedir el correo de invitación del cuidador (%s)",
            type(error).__name__,
        )

    return _caregiver_response(profile, adult_id, payload, users)


def update_profile(
    user: AuthenticatedUser,
    payload: ProfileUpdateRequest,
    users: UserRepository,
) -> AuthenticatedUser:
    """A6: actualiza nombre y foto en Firebase y el perfil (con phone) en el repositorio.

    Devuelve el objeto user del contrato; phone se guarda pero no se expone (P4).
    """
    changes = payload.model_dump(exclude_unset=True)
    # Un null explícito en un nombre se descarta: el nombre es obligatorio en el perfil.
    for field_name in _PROFILE_NAME_FIELDS:
        if field_name in changes and changes[field_name] is None:
            del changes[field_name]

    profile = users.get_profile(user.uid)
    if profile is not None:
        current_first_name, current_last_name = profile.first_name, profile.last_name
    else:
        current_first_name, current_last_name = _split_display_name(user.display_name)

    first_name = changes.get("first_name", current_first_name)
    last_name = changes.get("last_name", current_last_name)
    names_changed = any(field_name in changes for field_name in _PROFILE_NAME_FIELDS)
    display_name = (
        _display_name(first_name, last_name) if names_changed else user.display_name
    )
    photo_changed = "photo_url" in changes
    photo_url = changes["photo_url"] if photo_changed else user.photo_url

    firebase_changes: dict[str, object] = {}
    if names_changed:
        firebase_changes["display_name"] = display_name
    if photo_changed:
        firebase_changes["photo_url"] = (
            photo_url if photo_url is not None else firebase_auth.DELETE_ATTRIBUTE
        )
    if firebase_changes:
        _update_firebase_user(user.uid, firebase_changes)

    if profile is not None:
        repository_changes = {
            field_name: changes[field_name]
            for field_name in _PROFILE_REPOSITORY_FIELDS
            if field_name in changes
        }
        if repository_changes:
            users.update_profile(user.uid, repository_changes)
    elif user.role is not None:
        users.add_profile(
            UserProfile(
                uid=user.uid,
                first_name=first_name,
                last_name=last_name,
                email=normalize_email(user.email or ""),
                role=user.role,
                phone=changes.get("phone"),
                photo_url=photo_url,
            )
        )

    return AuthenticatedUser(
        uid=user.uid,
        email=user.email,
        email_verified=user.email_verified,
        display_name=display_name,
        photo_url=photo_url,
        role=user.role,
    )


def register_device(
    user: AuthenticatedUser,
    payload: DeviceRegisterRequest,
    users: UserRepository,
    devices: DeviceRepository,
) -> None:
    """A10: guarda el token push y, con correo verificado, lo vuelve alcanzable (D-R2-3)."""
    devices.register(user.uid, payload.fcm_token)
    if user.email and user.email_verified is True:
        users.mark_email_verified(user.uid, user.email)


def _require_role(user: AuthenticatedUser, *roles: Role) -> None:
    if user.role is None or user.role not in roles:
        raise AccountPermissionDeniedError("No tienes permiso para esta acción.")


def _display_name(first_name: str, last_name: str) -> str:
    return f"{first_name} {last_name}".strip()


def _split_display_name(display_name: str | None) -> tuple[str, str]:
    """Parte el nombre visible en el primer espacio: (first_name, last_name)."""
    parts = (display_name or "").strip().split(" ", 1)
    first_name = parts[0].strip()
    last_name = parts[1].strip() if len(parts) > 1 else ""
    return first_name, last_name


def _update_firebase_user(uid: str, changes: Mapping[str, object]) -> None:
    firebase_app = get_firebase_app()
    try:
        firebase_auth.update_user(uid, app=firebase_app, **changes)
    except firebase_auth.UserNotFoundError as error:
        raise UserProfileNotFoundError("El usuario no existe.") from error
    except ValueError as error:
        raise InvalidProfileDataError(
            "Datos inválidos: photo_url: no es válido"
        ) from error
    except FirebaseError as error:
        raise AuthProviderUnavailableError(
            "Firebase no pudo actualizar el perfil."
        ) from error


def _delete_firebase_user_quietly(uid: str, firebase_app: object) -> None:
    try:
        firebase_auth.delete_user(uid, app=firebase_app)
    except (FirebaseError, ValueError) as error:
        logger.error(
            "No se pudo borrar la cuenta %s que quedó sin rol (%s)",
            uid,
            type(error).__name__,
        )


def _firebase_error_code(response: httpx.Response) -> str:
    """Extrae el código de error de Firebase sin fallar nunca ("desconocido" si no hay)."""
    try:
        response_data = response.json()
    except ValueError:
        return _UNKNOWN_FIREBASE_CODE

    error_data = response_data.get("error") if isinstance(response_data, dict) else None
    message = error_data.get("message") if isinstance(error_data, dict) else None
    if not isinstance(message, str):
        return _UNKNOWN_FIREBASE_CODE

    code = message.split(":", 1)[0].strip()
    if _FIREBASE_CODE_PATTERN.fullmatch(code) is None:
        return _UNKNOWN_FIREBASE_CODE
    return code


def _adult_response(profile: UserProfile) -> AdultResponse:
    return AdultResponse(
        id=profile.uid,
        first_name=profile.first_name,
        last_name=profile.last_name,
        email=profile.email,
        phone=profile.phone,
        birth_date=profile.birth_date,
        address=profile.address,
        photo_url=profile.photo_url,
    )


def _caregiver_profile(uid: str, payload: CaregiverCreateRequest) -> UserProfile:
    return UserProfile(
        uid=uid,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        role=Role.CUIDADOR,
    )


def _caregiver_response(
    profile: UserProfile,
    adult_id: str,
    payload: CaregiverCreateRequest,
    users: UserRepository,
) -> CaregiverResponse:
    link = users.link_of(profile.uid, adult_id)
    permissions = (
        link.permissions
        if link is not None and link.permissions is not None
        else payload.permissions
    )
    return CaregiverResponse(
        id=profile.uid,
        first_name=profile.first_name,
        last_name=profile.last_name,
        email=profile.email,
        permissions=permissions,
    )
