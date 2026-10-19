import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.firebase import FirebaseConfigurationError
from app.core.security import (
    AUTH_UNAVAILABLE_DETAIL,
    FORBIDDEN_DETAIL,
    get_current_user,
    require_roles,
)
from app.models.auth import (
    AdultCreateRequest,
    AdultResponse,
    AuthenticatedUser,
    CaregiverCreateRequest,
    CaregiverResponse,
    DeviceRegisterRequest,
    LoginRequest,
    LoginResponse,
    PasswordResetRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
)
from app.models.enums import Role
from app.repositories.device_repository import DeviceRepository, get_device_repository
from app.repositories.user_repository import UserRepository, get_user_repository
from app.services.auth_service import (
    AccountPermissionDeniedError,
    AdultNotFoundError,
    AuthMethodDisabledError,
    AuthProviderUnavailableError,
    EmailAlreadyExistsError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    InvalidProfileDataError,
    InvalidTokenError,
    TooManyAttemptsError,
    UserDisabledError,
    UserProfileNotFoundError,
    WeakPasswordError,
    create_adult,
    create_caregiver,
    list_adults,
    refresh_session,
    register_device,
    register_family,
    send_password_reset_email,
    sign_in_with_email_password,
    update_profile,
)


ADULT_NOT_FOUND_DETAIL = "El adulto mayor no existe."
USER_NOT_FOUND_DETAIL = "El usuario no existe."

router = APIRouter()
logger = logging.getLogger(__name__)


def _service_unavailable(error: Exception) -> HTTPException:
    logger.error(
        "Proveedor de autenticación no disponible (%s): %s",
        type(error).__name__,
        error,
    )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=AUTH_UNAVAILABLE_DETAIL,
    )


def _invalid_data(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=str(error),
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=FORBIDDEN_DETAIL,
    )


def _adult_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ADULT_NOT_FOUND_DETAIL,
    )


def _email_not_verified(error: EmailNotVerifiedError) -> HTTPException:
    """403 con el texto fijo del servicio (D-LL-4)."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=str(error),
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    users: UserRepository = Depends(get_user_repository),
) -> LoginResponse:
    try:
        return await sign_in_with_email_password(payload, users=users)
    except InvalidCredentialsError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error
    except EmailNotVerifiedError as error:
        raise _email_not_verified(error) from error
    except UserDisabledError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(error),
        ) from error
    except TooManyAttemptsError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
        ) from error
    except (
        AuthMethodDisabledError,
        FirebaseConfigurationError,
        AuthProviderUnavailableError,
    ) as error:
        raise _service_unavailable(error) from error


@router.post("/refresh", response_model=LoginResponse)
async def refresh(
    payload: RefreshRequest,
    users: UserRepository = Depends(get_user_repository),
) -> LoginResponse:
    try:
        return await refresh_session(payload, users=users)
    except InvalidTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error
    except EmailNotVerifiedError as error:
        raise _email_not_verified(error) from error
    except UserDisabledError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(error),
        ) from error
    except (FirebaseConfigurationError, AuthProviderUnavailableError) as error:
        raise _service_unavailable(error) from error


@router.get("/me", response_model=AuthenticatedUser)
def current_user(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    return user


# --- A4: registro de familiar (público) ---


@router.post("/register", response_model=LoginResponse)
async def register(
    payload: RegisterRequest,
    users: UserRepository = Depends(get_user_repository),
) -> LoginResponse:
    try:
        return await register_family(payload, users)
    except (EmailAlreadyExistsError, WeakPasswordError) as error:
        raise _invalid_data(error) from error
    except InvalidCredentialsError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error
    except UserDisabledError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(error),
        ) from error
    except TooManyAttemptsError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
        ) from error
    except (
        AuthMethodDisabledError,
        FirebaseConfigurationError,
        AuthProviderUnavailableError,
    ) as error:
        raise _service_unavailable(error) from error


# --- A5: recuperar contraseña (público; 204 exista o no el correo) ---


@router.post(
    "/password-reset",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def password_reset(payload: PasswordResetRequest) -> Response:
    try:
        await send_password_reset_email(payload.email)
    except (FirebaseConfigurationError, AuthProviderUnavailableError) as error:
        raise _service_unavailable(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- A6: actualizar el perfil propio (todos los roles) ---


@router.patch("/me", response_model=AuthenticatedUser)
def update_me(
    payload: ProfileUpdateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    users: UserRepository = Depends(get_user_repository),
) -> AuthenticatedUser:
    try:
        return update_profile(user, payload, users)
    except InvalidProfileDataError as error:
        raise _invalid_data(error) from error
    except UserProfileNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=USER_NOT_FOUND_DETAIL,
        ) from error
    except (FirebaseConfigurationError, AuthProviderUnavailableError) as error:
        raise _service_unavailable(error) from error


# --- A7 y A8: adultos mayores del familiar o cuidador ---


@router.post("/adults", response_model=AdultResponse)
def create_adult_account(
    payload: AdultCreateRequest,
    user: AuthenticatedUser = Depends(require_roles(Role.FAMILIAR)),
    users: UserRepository = Depends(get_user_repository),
) -> AdultResponse:
    try:
        return create_adult(user, payload, users)
    except AccountPermissionDeniedError as error:
        raise _forbidden() from error
    except (EmailAlreadyExistsError, WeakPasswordError) as error:
        raise _invalid_data(error) from error
    except (FirebaseConfigurationError, AuthProviderUnavailableError) as error:
        raise _service_unavailable(error) from error


@router.get("/adults", response_model=list[AdultResponse])
def list_adult_accounts(
    user: AuthenticatedUser = Depends(require_roles(Role.FAMILIAR, Role.CUIDADOR)),
    users: UserRepository = Depends(get_user_repository),
) -> list[AdultResponse]:
    try:
        return list_adults(user, users)
    except AccountPermissionDeniedError as error:
        raise _forbidden() from error


# --- A9: invitar a un cuidador (solo el familiar responsable) ---


@router.post(
    "/adults/{adult_id}/caregivers",
    response_model=CaregiverResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_caregiver_account(
    adult_id: str,
    payload: CaregiverCreateRequest,
    user: AuthenticatedUser = Depends(require_roles(Role.FAMILIAR)),
    users: UserRepository = Depends(get_user_repository),
) -> CaregiverResponse:
    try:
        return await create_caregiver(user, adult_id, payload, users)
    except AccountPermissionDeniedError as error:
        raise _forbidden() from error
    except AdultNotFoundError as error:
        raise _adult_not_found() from error
    except (EmailAlreadyExistsError, WeakPasswordError) as error:
        raise _invalid_data(error) from error
    except (FirebaseConfigurationError, AuthProviderUnavailableError) as error:
        raise _service_unavailable(error) from error


# --- A10: registrar el token push del navegador (todos los roles) ---


@router.post(
    "/devices",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
def register_push_device(
    payload: DeviceRegisterRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    users: UserRepository = Depends(get_user_repository),
    devices: DeviceRepository = Depends(get_device_repository),
) -> Response:
    register_device(user, payload, users, devices)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
