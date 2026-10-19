"""Router de administración (B1, B2) bajo /api/admin, solo rol administrador (contrato 6.7).

Cualquier otro rol, o un usuario sin rol, recibe 403 antes de llamar a Firebase
(D-R2-7, paso 0). Las rutas son def porque el Admin SDK es síncrono (regla 4.3).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.firebase import FirebaseConfigurationError
from app.core.security import AUTH_UNAVAILABLE_DETAIL, require_roles
from app.models.admin import AdminUserResponse, AdminUserUpdateRequest
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.services.admin_service import (
    AdminUserNotFoundError,
    SelfDisableError,
    list_users,
    set_user_disabled,
)
from app.services.auth_service import AuthProviderUnavailableError


router = APIRouter()
logger = logging.getLogger(__name__)

require_administrator = require_roles(Role.ADMINISTRADOR)


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


@router.get("/users", response_model=list[AdminUserResponse])
def list_admin_users(
    user: AuthenticatedUser = Depends(require_administrator),
) -> list[AdminUserResponse]:
    try:
        return list_users()
    except (AuthProviderUnavailableError, FirebaseConfigurationError) as error:
        raise _service_unavailable(error) from error


@router.patch("/users/{uid}", response_model=AdminUserResponse)
def update_admin_user(
    uid: str,
    payload: AdminUserUpdateRequest,
    user: AuthenticatedUser = Depends(require_administrator),
) -> AdminUserResponse:
    try:
        return set_user_disabled(user.uid, uid, payload.disabled)
    except AdminUserNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except SelfDisableError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except (AuthProviderUnavailableError, FirebaseConfigurationError) as error:
        raise _service_unavailable(error) from error
