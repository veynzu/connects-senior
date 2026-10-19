"""Router de mensajes de texto (M1, M2) bajo /api/messages (CLAUDE.md 6.4, D-R2-12).

Solo valida la entrada, resuelve dependencias y traduce las excepciones del servicio.
Administrador y usuario sin rol reciben 403 antes de buscar el contacto (D-R2-7).
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.security import FORBIDDEN_DETAIL, require_roles
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.models.messages import (
    MessageCreateRequest,
    MessageCreateResponse,
    MessageResponse,
)
from app.repositories.contact_repository import (
    ContactRepository,
    get_contact_repository,
)
from app.repositories.device_repository import (
    DeviceRepository,
    get_device_repository,
)
from app.repositories.message_repository import (
    MessageRepository,
    get_message_repository,
)
from app.repositories.user_repository import UserRepository, get_user_repository
from app.services.message_service import (
    MessageContactNotFoundError,
    MessagePermissionDeniedError,
    list_conversation,
    send_message,
)


CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."

router = APIRouter()

_message_roles = require_roles(Role.ADULTO_MAYOR, Role.FAMILIAR, Role.CUIDADOR)


def _contact_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=CONTACT_NOT_FOUND_DETAIL,
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=FORBIDDEN_DETAIL,
    )


@router.post(
    "",
    response_model=MessageCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_message(
    payload: MessageCreateRequest,
    user: AuthenticatedUser = Depends(_message_roles),
    contacts: ContactRepository = Depends(get_contact_repository),
    messages: MessageRepository = Depends(get_message_repository),
    users: UserRepository = Depends(get_user_repository),
    devices: DeviceRepository = Depends(get_device_repository),
) -> MessageCreateResponse:
    try:
        return send_message(user, payload, contacts, messages, users, devices)
    except MessageContactNotFoundError as error:
        raise _contact_not_found() from error
    except MessagePermissionDeniedError as error:
        raise _forbidden() from error


@router.get("", response_model=list[MessageResponse])
def list_messages(
    contact_id: str = Query(min_length=1),
    user: AuthenticatedUser = Depends(_message_roles),
    contacts: ContactRepository = Depends(get_contact_repository),
    messages: MessageRepository = Depends(get_message_repository),
    users: UserRepository = Depends(get_user_repository),
) -> list[MessageResponse]:
    try:
        return list_conversation(user, contact_id, contacts, messages, users)
    except MessageContactNotFoundError as error:
        raise _contact_not_found() from error
    except MessagePermissionDeniedError as error:
        raise _forbidden() from error
