"""Router de llamadas y videollamadas (L1-L3) sobre Daily.co y de mensajes de voz (V1, V2)."""
import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse

from app.core.config import Settings, get_settings
from app.core.security import (
    FORBIDDEN_DETAIL,
    get_current_user,
    require_roles,
    resolve_adult_scope,
)
from app.integrations.daily_client import (
    DailyClient,
    DailyNotConfiguredError,
    DailyUnavailableError,
    get_daily_client,
)
from app.models.auth import AuthenticatedUser
from app.models.calls import (
    CallCreateRequest,
    CallResponse,
    CallStartResponse,
    CallUpdateRequest,
)
from app.models.enums import Role
from app.models.voice_messages import VoiceMessageCreateResponse, VoiceMessageResponse
from app.repositories.call_repository import CallRepository, get_call_repository
from app.repositories.contact_repository import (
    ContactRepository,
    get_contact_repository,
)
from app.repositories.device_repository import DeviceRepository, get_device_repository
from app.repositories.user_repository import UserRepository, get_user_repository
from app.repositories.voice_message_repository import (
    VoiceMessageRepository,
    get_voice_message_repository,
)
from app.services.call_service import (
    CallNotFoundError,
    CallPermissionDeniedError,
    ContactNotFoundError,
    list_calls,
    start_call,
    update_call_status,
)
from app.services.voice_message_service import (
    AudioTooLargeError,
    InvalidAudioError,
    VoiceContactNotFoundError,
    VoiceMessageNotFoundError,
    VoicePermissionDeniedError,
    VoiceStorageUnavailableError,
    list_received,
    resolve_audio_path,
    save_voice_message,
)


CALLS_UNAVAILABLE_DETAIL = "El servicio de llamadas no está disponible. Intenta más tarde."
CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."
CALL_NOT_FOUND_DETAIL = "La llamada no existe."
VOICE_MESSAGES_UNAVAILABLE_DETAIL = (
    "El servicio de mensajes de voz no está disponible. Intenta más tarde."
)
VOICE_MESSAGE_NOT_FOUND_DETAIL = "El mensaje de voz no existe."
AUDIO_MEDIA_TYPE = "audio/webm"

router = APIRouter()
logger = logging.getLogger(__name__)

_voice_message_roles = require_roles(Role.ADULTO_MAYOR, Role.FAMILIAR, Role.CUIDADOR)


def _content_length(request: Request) -> int | None:
    """Content-Length de la petición; ausente, no numérico o negativo -> None."""
    raw_value = request.headers.get("content-length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _voice_storage_unavailable(error: VoiceStorageUnavailableError) -> HTTPException:
    # Solo el tipo y el mensaje propio de la excepción; nunca __cause__.
    logger.error(
        "Mensajes de voz no disponibles (%s): %s", type(error).__name__, error
    )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=VOICE_MESSAGES_UNAVAILABLE_DETAIL,
    )


# --- Mensajes de voz (V1, V2). Van antes de /{call_id}. ---


@router.post(
    "/voice-messages",
    response_model=VoiceMessageCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_voice_message(
    request: Request,
    contact_id: str = Form(min_length=1),
    audio: UploadFile = File(...),
    user: AuthenticatedUser = Depends(_voice_message_roles),
    contacts: ContactRepository = Depends(get_contact_repository),
    users: UserRepository = Depends(get_user_repository),
    voice_messages: VoiceMessageRepository = Depends(get_voice_message_repository),
    devices: DeviceRepository = Depends(get_device_repository),
    settings: Settings = Depends(get_settings),
) -> VoiceMessageCreateResponse:
    try:
        return await save_voice_message(
            user,
            contact_id,
            audio,
            _content_length(request),
            contacts,
            users,
            voice_messages,
            devices,
            settings,
        )
    except VoiceContactNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=CONTACT_NOT_FOUND_DETAIL,
        ) from error
    except VoicePermissionDeniedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=FORBIDDEN_DETAIL,
        ) from error
    except (InvalidAudioError, AudioTooLargeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except VoiceStorageUnavailableError as error:
        raise _voice_storage_unavailable(error) from error


@router.get("/voice-messages", response_model=list[VoiceMessageResponse])
def list_voice_messages(
    request: Request,
    user: AuthenticatedUser = Depends(_voice_message_roles),
    contacts: ContactRepository = Depends(get_contact_repository),
    users: UserRepository = Depends(get_user_repository),
    voice_messages: VoiceMessageRepository = Depends(get_voice_message_repository),
    settings: Settings = Depends(get_settings),
) -> list[VoiceMessageResponse]:
    try:
        return list_received(
            user, str(request.base_url), contacts, users, voice_messages, settings
        )
    except VoicePermissionDeniedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=FORBIDDEN_DETAIL,
        ) from error
    except VoiceStorageUnavailableError as error:
        raise _voice_storage_unavailable(error) from error


@router.get(
    "/voice-messages/{voice_message_id}/audio",
    response_class=FileResponse,
    response_model=None,
    responses={200: {"content": {AUDIO_MEDIA_TYPE: {}}}},
)
def get_voice_message_audio(
    voice_message_id: str,
    sig: str | None = Query(default=None),
    exp: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
    voice_messages: VoiceMessageRepository = Depends(get_voice_message_repository),
) -> FileResponse:
    # Sin Bearer: la autorización es la firma de audio_url (6.3, ADR-08, P11).
    try:
        path = resolve_audio_path(voice_message_id, sig, exp, voice_messages, settings)
    except VoiceMessageNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=VOICE_MESSAGE_NOT_FOUND_DETAIL,
        ) from error
    return FileResponse(path, media_type=AUDIO_MEDIA_TYPE)


# --- Llamadas (L1-L3) ---


@router.post("", response_model=CallStartResponse)
async def create_call(
    payload: CallCreateRequest,
    user: AuthenticatedUser = Depends(require_roles(Role.ADULTO_MAYOR)),
    contacts: ContactRepository = Depends(get_contact_repository),
    calls: CallRepository = Depends(get_call_repository),
    daily: DailyClient = Depends(get_daily_client),
    users: UserRepository = Depends(get_user_repository),
    devices: DeviceRepository = Depends(get_device_repository),
) -> CallStartResponse:
    try:
        return await start_call(user, payload, contacts, calls, daily, users, devices)
    except ContactNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=CONTACT_NOT_FOUND_DETAIL,
        ) from error
    except CallPermissionDeniedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=FORBIDDEN_DETAIL,
        ) from error
    except (DailyNotConfiguredError, DailyUnavailableError) as error:
        # Solo el tipo y el mensaje propio de la excepción; nunca __cause__.
        logger.error("Daily.co no disponible (%s): %s", type(error).__name__, error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=CALLS_UNAVAILABLE_DETAIL,
        ) from error


@router.patch("/{call_id}", response_model=CallResponse)
def update_call(
    call_id: str,
    payload: CallUpdateRequest,
    user: AuthenticatedUser = Depends(require_roles(Role.ADULTO_MAYOR)),
    calls: CallRepository = Depends(get_call_repository),
) -> CallResponse:
    try:
        return update_call_status(user.uid, call_id, payload.status, calls)
    except CallNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=CALL_NOT_FOUND_DETAIL,
        ) from error


@router.get("", response_model=list[CallResponse])
def list_call_history(
    adult_id: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
    users: UserRepository = Depends(get_user_repository),
    calls: CallRepository = Depends(get_call_repository),
) -> list[CallResponse]:
    adult_uid = resolve_adult_scope(user, adult_id, users)
    return list_calls(adult_uid, calls)
