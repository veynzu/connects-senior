"""Servicio de administración de usuarios (B1, B2; contrato 6.7, RF-06).

Usa el Admin SDK de Firebase directamente (D-R2-15): desviación documentada de
CLAUDE.md 4.1, en la misma capa de servicios que auth_service.py. Las funciones
son síncronas y hacen red: el router las expone como rutas def (threadpool de
FastAPI, regla 4.3). Nunca se registran ni se devuelven mensajes crudos de Firebase.
"""
import logging
from collections.abc import Mapping
from typing import Any

from firebase_admin import auth as firebase_auth
from firebase_admin.exceptions import FirebaseError

from app.core.firebase import get_firebase_app
from app.models.admin import AdminUserResponse
from app.services.auth_service import AuthProviderUnavailableError, _parse_role


USER_NOT_FOUND_DETAIL = "El usuario no existe."
SELF_DISABLE_DETAIL = "No puedes deshabilitar tu propia cuenta."

logger = logging.getLogger(__name__)


class AdminUserNotFoundError(Exception):
    """El uid no existe en Firebase o está mal formado (404)."""


class SelfDisableError(Exception):
    """Un administrador intenta deshabilitar su propia cuenta (422, P24)."""


def _to_response(record: Any) -> AdminUserResponse:
    claims = record.custom_claims
    role_claim = claims.get("role") if isinstance(claims, Mapping) else None
    return AdminUserResponse(
        uid=record.uid,
        email=record.email,
        display_name=record.display_name,
        role=_parse_role(role_claim, record.uid),
        disabled=bool(record.disabled),
    )


def list_users() -> list[AdminUserResponse]:
    """B1: todos los usuarios de Firebase, recorriendo todas las páginas.

    FirebaseConfigurationError (de get_firebase_app) se propaga: el router responde 503.
    """
    app = get_firebase_app()
    users: list[AdminUserResponse] = []
    try:
        page = firebase_auth.list_users(app=app)
        while page is not None:
            users.extend(_to_response(record) for record in page.users)
            page = page.get_next_page()
    except (FirebaseError, ValueError) as error:
        raise AuthProviderUnavailableError(
            f"Firebase no pudo listar los usuarios ({type(error).__name__})."
        ) from error
    return users


def set_user_disabled(admin_uid: str, uid: str, disabled: bool) -> AdminUserResponse:
    """B2: habilita o deshabilita la cuenta uid.

    Deshabilitar revoca los refresh tokens para cortar las sesiones activas
    (check_revoked=True las rechaza); habilitar no revoca nada. Un administrador
    no puede deshabilitarse a sí mismo (se comprueba antes de llamar a Firebase).
    """
    if disabled and uid == admin_uid:
        raise SelfDisableError(SELF_DISABLE_DETAIL)

    app = get_firebase_app()
    try:
        record = firebase_auth.update_user(uid, disabled=disabled, app=app)
        if disabled:
            firebase_auth.revoke_refresh_tokens(uid, app=app)
    except (firebase_auth.UserNotFoundError, ValueError) as error:
        raise AdminUserNotFoundError(USER_NOT_FOUND_DETAIL) from error
    except FirebaseError as error:
        raise AuthProviderUnavailableError(
            f"Firebase no pudo actualizar el usuario ({type(error).__name__})."
        ) from error

    logger.info(
        "Usuario %s %s por el administrador %s",
        uid,
        "deshabilitado" if disabled else "habilitado",
        admin_uid,
    )
    return _to_response(record)
