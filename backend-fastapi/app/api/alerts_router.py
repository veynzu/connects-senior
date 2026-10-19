"""Router de Pedir ayuda (H1-H5, tarea 3) y tick de escalamiento (RF-24, D-R2-6).

Solo valida la entrada, resuelve dependencias y traduce las excepciones del
servicio; las reglas de negocio y permisos viven en alert_service.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.config import get_settings
from app.core.scheduler import register_tick
from app.core.security import FORBIDDEN_DETAIL, require_roles, resolve_adult_scope
from app.models.alerts import (
    AlertCreateRequest,
    AlertCreateResponse,
    AlertDetail,
    AlertSummary,
    AlertUpdateRequest,
)
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.repositories.alert_repository import AlertRepository, get_alert_repository
from app.repositories.contact_repository import (
    ContactRepository,
    get_contact_repository,
)
from app.repositories.device_repository import (
    DeviceRepository,
    get_device_repository,
)
from app.repositories.user_repository import UserRepository, get_user_repository
from app.services.alert_service import (
    AlertAlreadyClosedError,
    AlertLocationIncompleteError,
    AlertNotFoundError,
    AlertPermissionDeniedError,
    acknowledge_alert,
    create_alert,
    escalate_pending_alerts,
    get_alert,
    list_alerts,
    update_alert_status,
)


ALERT_NOT_FOUND_DETAIL = "La solicitud no existe."
ALERT_ALREADY_CLOSED_DETAIL = "La solicitud ya fue atendida."
ALERT_LOCATION_INCOMPLETE_DETAIL = (
    "Datos inválidos: latitude y longitude deben enviarse juntas"
)
ALERT_ESCALATION_TICK = "alert_escalation"

router = APIRouter()

_require_adult = require_roles(Role.ADULTO_MAYOR)
_require_linked_roles = require_roles(Role.ADULTO_MAYOR, Role.FAMILIAR, Role.CUIDADOR)


def _http_error(error: Exception) -> HTTPException:
    """Traduce una excepción de dominio de alert_service a HTTPException."""
    if isinstance(error, AlertNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=ALERT_NOT_FOUND_DETAIL
        )
    if isinstance(error, AlertPermissionDeniedError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL
        )
    if isinstance(error, AlertAlreadyClosedError):
        return HTTPException(status_code=422, detail=ALERT_ALREADY_CLOSED_DETAIL)
    return HTTPException(status_code=422, detail=ALERT_LOCATION_INCOMPLETE_DETAIL)


_DOMAIN_ERRORS = (
    AlertNotFoundError,
    AlertPermissionDeniedError,
    AlertAlreadyClosedError,
    AlertLocationIncompleteError,
)


@router.post("", response_model=AlertCreateResponse)
def create_alert_route(
    payload: AlertCreateRequest | None = None,
    user: AuthenticatedUser = Depends(_require_adult),
    alerts: AlertRepository = Depends(get_alert_repository),
    contacts: ContactRepository = Depends(get_contact_repository),
    users: UserRepository = Depends(get_user_repository),
    devices: DeviceRepository = Depends(get_device_repository),
) -> AlertCreateResponse:
    # Ruta def: notify_users es síncrona y hace red (threadpool de FastAPI, regla 4.3).
    try:
        return create_alert(user, payload, alerts, contacts, users, devices)
    except _DOMAIN_ERRORS as error:
        raise _http_error(error) from error


@router.get("", response_model=list[AlertSummary])
def list_alerts_route(
    adult_id: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(_require_linked_roles),
    users: UserRepository = Depends(get_user_repository),
    alerts: AlertRepository = Depends(get_alert_repository),
) -> list[AlertSummary]:
    adult_uid = resolve_adult_scope(user, adult_id, users)
    return list_alerts(adult_uid, alerts)


@router.get("/{alert_id}", response_model=AlertDetail)
def get_alert_route(
    alert_id: str,
    user: AuthenticatedUser = Depends(_require_linked_roles),
    users: UserRepository = Depends(get_user_repository),
    alerts: AlertRepository = Depends(get_alert_repository),
) -> AlertDetail:
    try:
        return get_alert(user, alert_id, alerts, users)
    except _DOMAIN_ERRORS as error:
        raise _http_error(error) from error


@router.post("/{alert_id}/acknowledge", response_model=AlertDetail)
def acknowledge_alert_route(
    alert_id: str,
    user: AuthenticatedUser = Depends(_require_linked_roles),
    users: UserRepository = Depends(get_user_repository),
    alerts: AlertRepository = Depends(get_alert_repository),
) -> AlertDetail:
    try:
        return acknowledge_alert(user, alert_id, alerts, users)
    except _DOMAIN_ERRORS as error:
        raise _http_error(error) from error


@router.patch("/{alert_id}", response_model=AlertDetail)
def update_alert_route(
    alert_id: str,
    payload: AlertUpdateRequest,
    user: AuthenticatedUser = Depends(_require_linked_roles),
    users: UserRepository = Depends(get_user_repository),
    alerts: AlertRepository = Depends(get_alert_repository),
) -> AlertDetail:
    try:
        return update_alert_status(user, alert_id, payload.status, alerts, users)
    except _DOMAIN_ERRORS as error:
        raise _http_error(error) from error


def alert_escalation_tick() -> None:
    """Tick del planificador: reenvía las solicitudes pendientes (RF-24, P10).

    Usa los proveedores por defecto porque corre fuera de una petición.
    """
    escalate_pending_alerts(
        alerts=get_alert_repository(),
        contacts=get_contact_repository(),
        users=get_user_repository(),
        devices=get_device_repository(),
        escalation_seconds=get_settings().alert_escalation_seconds,
    )


register_tick(ALERT_ESCALATION_TICK, alert_escalation_tick)
