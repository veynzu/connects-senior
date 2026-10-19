"""Servicio de llamadas y videollamadas (L1-L3, RF-10, RF-11).

No conoce FastAPI: lanza excepciones de dominio que el router traduce. Los
errores de Daily.co (DailyClientError) se propagan sin envolver.

Push de llamada entrante (P20, D-R2-16): si el contacto corresponde a una cuenta
verificada con dispositivo registrado, L1 crea un segundo meeting token para esa
persona y se lo envía por push. Ese paso nunca hace fallar L1, y el token del
contacto no se guarda ni se registra en el log.
"""
import logging
from datetime import datetime, timezone
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from app.integrations.daily_client import DailyClient
from app.models.auth import AuthenticatedUser
from app.models.calls import (
    CallCreateRequest,
    CallRecord,
    CallResponse,
    CallStartResponse,
)
from app.models.contacts import Contact
from app.models.enums import CallStatus
from app.repositories.call_repository import CallRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.user_repository import UserRepository
from app.services import push_service


AUDIO_CALL_PUSH_TITLE = "Llamada entrante"
VIDEO_CALL_PUSH_TITLE = "Videollamada entrante"

logger = logging.getLogger(__name__)


class ContactNotFoundError(Exception):
    """El contacto no existe o pertenece a otro adulto."""


class CallNotFoundError(Exception):
    """La llamada no existe o pertenece a otro adulto."""


class CallPermissionDeniedError(Exception):
    """El contacto no admite llamadas (calls) o videollamadas (video_calls)."""


async def start_call(
    adult: AuthenticatedUser,
    payload: CallCreateRequest,
    contacts: ContactRepository,
    calls: CallRepository,
    daily: DailyClient,
    users: UserRepository,
    devices: DeviceRepository,
) -> CallStartResponse:
    contact = contacts.get(adult.uid, payload.contact_id)
    if contact is None:
        raise ContactNotFoundError("El contacto no existe.")

    allowed = (
        contact.permissions.calls
        if payload.audio_only
        else contact.permissions.video_calls
    )
    if not allowed:
        raise CallPermissionDeniedError("El contacto no admite este tipo de llamada.")

    room = await daily.create_room(audio_only=payload.audio_only)
    token = await daily.create_meeting_token(
        room_name=room.name,
        user_id=adult.uid,
        user_name=adult.display_name or adult.uid,
        audio_only=payload.audio_only,
        expires_at=room.expires_at,
    )

    # Solo se guarda cuando Daily respondió bien; si falla, la sala huérfana expira sola.
    record = calls.add(
        CallRecord(
            id=str(uuid4()),
            adult_uid=adult.uid,
            contact_id=contact.id,
            started_at=datetime.now(timezone.utc).replace(microsecond=0),
            status=CallStatus.NO_CONTESTADA,
            audio_only=payload.audio_only,
            room_name=room.name,
            room_url=room.url,
            room_expires_at=room.expires_at,
        )
    )
    await _notify_callee(adult, contact, record, daily, users, devices)
    return CallStartResponse(call_id=record.id, room_url=record.room_url, token=token)


async def _notify_callee(
    adult: AuthenticatedUser,
    contact: Contact,
    record: CallRecord,
    daily: DailyClient,
    users: UserRepository,
    devices: DeviceRepository,
) -> None:
    """Push de llamada entrante al contacto (D-R2-16); nunca lanza.

    Sin uid verificado para el contacto o sin dispositivo registrado no se hace nada
    (ni siquiera se pide el segundo token). Si Daily falla al crear el token del
    contacto o la push falla, solo se registra el tipo del error.
    """
    callee_uid = push_service.linked_uid_for_contact(contact, users)
    if callee_uid is None or not devices.has_device(callee_uid):
        return

    try:
        callee_token = await daily.create_meeting_token(
            room_name=record.room_name,
            user_id=callee_uid,
            user_name=f"{contact.first_name} {contact.last_name}".strip(),
            audio_only=record.audio_only,
            expires_at=record.room_expires_at,
        )
    except Exception as error:
        # La llamada ya está creada para el adulto: L1 responde 200 igual.
        logger.error(
            "No se pudo crear el token del contacto para la llamada (%s)",
            type(error).__name__,
        )
        return

    from_name = adult.display_name or adult.uid
    title = AUDIO_CALL_PUSH_TITLE if record.audio_only else VIDEO_CALL_PUSH_TITLE
    # Todos los valores de data son texto (FCM); data lleva el token: nunca se registra.
    data = {
        "type": "call",
        "call_id": record.id,
        "room_url": record.room_url,
        "token": callee_token,
        "audio_only": "true" if record.audio_only else "false",
        "from_name": from_name,
    }
    try:
        await run_in_threadpool(
            push_service.notify_users,
            [callee_uid],
            title,
            f"{from_name} te está llamando",
            data,
            devices,
        )
    except Exception as error:
        # notify_users no lanza; esto es defensa en profundidad.
        logger.error(
            "No se pudo enviar la push de llamada entrante (%s)", type(error).__name__
        )


def update_call_status(
    adult_uid: str,
    call_id: str,
    status: CallStatus,
    calls: CallRepository,
) -> CallResponse:
    record = calls.update_status(adult_uid, call_id, status)
    if record is None:
        raise CallNotFoundError("La llamada no existe.")
    return _to_response(record)


def list_calls(adult_uid: str, calls: CallRepository) -> list[CallResponse]:
    return [_to_response(record) for record in calls.list_for_adult(adult_uid)]


def _to_response(record: CallRecord) -> CallResponse:
    return CallResponse(
        id=record.id,
        contact_id=record.contact_id,
        started_at=record.started_at,
        status=record.status,
    )
