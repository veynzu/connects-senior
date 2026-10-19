"""Router de recordatorios (R1-R6, RF-27 a RF-30) y tick del planificador (RF-28)."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.core.config import get_settings
from app.core.scheduler import register_tick
from app.core.security import FORBIDDEN_DETAIL, require_roles, resolve_adult_scope
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.models.reminders import (
    ReminderCreateRequest,
    ReminderResponse,
    ReminderSnoozeRequest,
    ReminderUpdateRequest,
)
from app.repositories.device_repository import get_device_repository
from app.repositories.reminder_repository import (
    ReminderRepository,
    get_reminder_repository,
)
from app.repositories.user_repository import UserRepository, get_user_repository
from app.services.reminder_service import (
    ReminderNotFoundError,
    ReminderPermissionDeniedError,
    confirm_reminder,
    create_reminder,
    delete_reminder,
    list_reminders,
    process_due_reminders,
    snooze_reminder,
    update_reminder,
)


REMINDER_NOT_FOUND_DETAIL = "El recordatorio no existe."
REMINDER_TICK_NAME = "reminders"

router = APIRouter()

# R1-R4: los tres roles con vínculo; administrador y usuario sin rol -> 403 (D-R2-7).
_linked_roles = require_roles(Role.ADULTO_MAYOR, Role.FAMILIAR, Role.CUIDADOR)
# R5 y R6: solo el adulto mayor (RF-29).
_adult_only = require_roles(Role.ADULTO_MAYOR)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=REMINDER_NOT_FOUND_DETAIL,
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=FORBIDDEN_DETAIL,
    )


@router.get("", response_model=list[ReminderResponse])
def list_adult_reminders(
    adult_id: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(_linked_roles),
    users: UserRepository = Depends(get_user_repository),
    reminders: ReminderRepository = Depends(get_reminder_repository),
) -> list[ReminderResponse]:
    adult_uid = resolve_adult_scope(user, adult_id, users)
    return list_reminders(adult_uid, reminders)


@router.post("", response_model=ReminderResponse, status_code=status.HTTP_200_OK)
def create_adult_reminder(
    payload: ReminderCreateRequest,
    user: AuthenticatedUser = Depends(_linked_roles),
    users: UserRepository = Depends(get_user_repository),
    reminders: ReminderRepository = Depends(get_reminder_repository),
) -> ReminderResponse:
    adult_uid = resolve_adult_scope(user, payload.adult_id, users)
    try:
        return create_reminder(user, adult_uid, payload, reminders, users)
    except ReminderPermissionDeniedError as error:
        raise _forbidden() from error


@router.patch("/{reminder_id}", response_model=ReminderResponse)
def update_adult_reminder(
    reminder_id: str,
    payload: ReminderUpdateRequest,
    user: AuthenticatedUser = Depends(_linked_roles),
    users: UserRepository = Depends(get_user_repository),
    reminders: ReminderRepository = Depends(get_reminder_repository),
) -> ReminderResponse:
    try:
        return update_reminder(user, reminder_id, payload, reminders, users)
    except ReminderNotFoundError as error:
        raise _not_found() from error
    except ReminderPermissionDeniedError as error:
        raise _forbidden() from error


@router.delete(
    "/{reminder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
def delete_adult_reminder(
    reminder_id: str,
    user: AuthenticatedUser = Depends(_linked_roles),
    users: UserRepository = Depends(get_user_repository),
    reminders: ReminderRepository = Depends(get_reminder_repository),
) -> Response:
    try:
        delete_reminder(user, reminder_id, reminders, users)
    except ReminderNotFoundError as error:
        raise _not_found() from error
    except ReminderPermissionDeniedError as error:
        raise _forbidden() from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{reminder_id}/confirm", response_model=ReminderResponse)
def confirm_adult_reminder(
    reminder_id: str,
    user: AuthenticatedUser = Depends(_adult_only),
    reminders: ReminderRepository = Depends(get_reminder_repository),
) -> ReminderResponse:
    try:
        return confirm_reminder(user, reminder_id, reminders)
    except ReminderNotFoundError as error:
        raise _not_found() from error
    except ReminderPermissionDeniedError as error:
        raise _forbidden() from error


@router.post("/{reminder_id}/snooze", response_model=ReminderResponse)
def snooze_adult_reminder(
    reminder_id: str,
    payload: ReminderSnoozeRequest,
    user: AuthenticatedUser = Depends(_adult_only),
    reminders: ReminderRepository = Depends(get_reminder_repository),
) -> ReminderResponse:
    try:
        return snooze_reminder(user, reminder_id, payload.minutes, reminders)
    except ReminderNotFoundError as error:
        raise _not_found() from error
    except ReminderPermissionDeniedError as error:
        raise _forbidden() from error


def reminder_tick() -> None:
    """Tick del planificador (D-R2-6): push a la hora programada y marcado de omitido.

    Usa los proveedores por defecto; process_due_reminders se busca en este módulo
    para que los tests puedan sustituirlo.
    """
    process_due_reminders(
        get_reminder_repository(),
        get_device_repository(),
        missed_minutes=get_settings().reminder_missed_minutes,
    )


register_tick(REMINDER_TICK_NAME, reminder_tick)
