import logging
from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.firebase import FirebaseConfigurationError
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.repositories.user_repository import UserRepository
from app.services.auth_service import (
    AuthProviderUnavailableError,
    InvalidTokenError,
    UserDisabledError,
    verify_firebase_id_token,
)


AUTH_UNAVAILABLE_DETAIL = (
    "El servicio de autenticación no está disponible. Intenta más tarde."
)
FORBIDDEN_DETAIL = "No tienes permiso para esta acción."
ADULT_ID_REQUIRED_DETAIL = "Datos inválidos: adult_id: es obligatorio"

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Debes enviar un token Bearer.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return verify_firebase_id_token(credentials.credentials)
    except InvalidTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except UserDisabledError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(error),
        ) from error
    except (FirebaseConfigurationError, AuthProviderUnavailableError) as error:
        logger.error(
            "Proveedor de autenticación no disponible (%s): %s",
            type(error).__name__,
            error,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=AUTH_UNAVAILABLE_DETAIL,
        ) from error


def require_roles(*roles: Role) -> Callable[..., AuthenticatedUser]:
    """Dependencia que exige un usuario autenticado con alguno de los roles dados.

    Un usuario sin claim role (role None) o con un rol fuera de la lista recibe 403.
    """

    def _require(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if user.role is None or user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return user

    return _require


def resolve_adult_scope(
    user: AuthenticatedUser,
    adult_id: str | None,
    users: UserRepository,
) -> str:
    """Resuelve el adulto mayor sobre el que actúa el usuario (regla del adulto implícito).

    - adulto_mayor: su propio uid; enviar su uid como adult_id es válido y uno ajeno da 403.
    - familiar o cuidador: adult_id obligatorio (422) y vinculado al usuario (403).
    - administrador o usuario sin rol: 403.

    adult_id se normaliza con strip: None, "" y solo espacios cuentan como ausentes.
    """
    requested_adult_id = (adult_id or "").strip()

    if user.role == Role.ADULTO_MAYOR:
        if requested_adult_id and requested_adult_id != user.uid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return user.uid

    if user.role in (Role.FAMILIAR, Role.CUIDADOR):
        if not requested_adult_id:
            raise HTTPException(status_code=422, detail=ADULT_ID_REQUIRED_DETAIL)
        if not users.is_linked(user.uid, requested_adult_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return requested_adult_id

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=FORBIDDEN_DETAIL,
    )


def can_access_adult(
    user: AuthenticatedUser,
    adult_uid: str,
    users: UserRepository,
) -> bool:
    """Indica si el usuario puede actuar sobre los recursos del adulto (D-R2-7, paso 1).

    - adulto_mayor: solo sobre sí mismo.
    - familiar o cuidador: solo sobre adultos vinculados.
    - administrador o usuario sin rol: nunca (defensa en profundidad, P7 y D-02.4).

    Los servicios responden 404 sin revelar existencia cuando devuelve False (P18).
    """
    if user.role == Role.ADULTO_MAYOR:
        return user.uid == adult_uid
    if user.role in (Role.FAMILIAR, Role.CUIDADOR):
        return users.is_linked(user.uid, adult_uid)
    return False
