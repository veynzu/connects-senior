"""Servicio de mensajes de voz (V1, V2; RF-21, ADR-08, P11, P19, D-R2-13).

- V1 guarda el audio en disco con un nombre generado por el backend (uuid4) y en el
  repositorio solo la referencia y los metadatos (ADR-08).
- V2 devuelve los mensajes recibidos por quien llama con una audio_url absoluta y
  firmada (HMAC-SHA256 sobre "id:exp", válida AUDIO_URL_TTL_SECONDS) que el front
  usa tal cual en <audio src>, sin Bearer.
- resolve_audio_path valida esa firma y devuelve el archivo para la ruta de audio.

No conoce FastAPI: lanza excepciones de dominio que el router traduce. Nunca se
registran la clave de firma, firmas, rutas del sistema de archivos ni el contenido de
las push.

save_voice_message es async: los repositorios (síncronos y, con SQL, con red) se
llaman solo desde funciones síncronas internas ejecutadas con run_in_threadpool
(D-R3-15). La resolución del destinatario y la push van después de guardar y dentro
de try/except: un fallo ahí nunca convierte en error un mensaje ya guardado (D-R3-23).
"""
import hashlib
import hmac
import logging
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.core.config import BACKEND_DIR, Settings
from app.core.security import FORBIDDEN_DETAIL
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.models.voice_messages import (
    VoiceMessageCreateResponse,
    VoiceMessageRecord,
    VoiceMessageResponse,
)
from app.repositories.contact_repository import ContactRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.errors import RecipientNotLinkedError
from app.repositories.user_repository import UserRepository
from app.repositories.voice_message_repository import VoiceMessageRepository
from app.services import push_service
from app.services.contact_scope import (
    ContactScope,
    ContactScopeForbiddenError,
    ContactScopeNotFoundError,
    recipient_contacts_for_user,
    resolve_contact_scope,
)


AUDIO_CONTENT_TYPE = "audio/webm"
CHUNK_SIZE = 65536
EBML_MAGIC = b"\x1a\x45\xdf\xa3"
AUDIO_URL_TTL_SECONDS = 900
# exp es un timestamp en segundos: más dígitos que esto nunca es un enlace emitido aquí.
_MAX_EXP_DIGITS = 12
MULTIPART_OVERHEAD_BYTES = 16384
VOICE_PUSH_TITLE = "Nuevo mensaje de voz"
DEVELOPMENT_ENVIRONMENT = "development"

INVALID_CONTENT_TYPE_DETAIL = "Datos inválidos: audio: debe ser un archivo audio/webm"
AUDIO_TOO_LARGE_DETAIL = "Datos inválidos: audio: supera el tamaño máximo permitido"
INVALID_AUDIO_DETAIL = "Datos inválidos: audio: el archivo está vacío o no es WebM"
VOICE_MESSAGE_NOT_FOUND_DETAIL = "El mensaje de voz no existe."
CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."

# Mensajes internos de VoiceStorageUnavailableError: van al log del servidor, sin
# rutas, nombres de variables ni valores de configuración.
_STORAGE_NOT_CONFIGURED_MESSAGE = (
    "El directorio de mensajes de voz no está configurado fuera de desarrollo."
)
_STORAGE_NOT_READY_MESSAGE = "No se pudo preparar el directorio de mensajes de voz."
_STORAGE_WRITE_MESSAGE = "No se pudo guardar el archivo de audio."
_SIGNING_KEY_NOT_CONFIGURED_MESSAGE = (
    "La clave de firma de mensajes de voz no está configurada fuera de desarrollo."
)
_EPHEMERAL_KEY_WARNING = (
    "Se usa una clave de firma efímera para los mensajes de voz (solo desarrollo): "
    "los enlaces de audio dejan de funcionar al reiniciar el servidor."
)

# Clave efímera por proceso, solo para development sin clave configurada (P22).
_EPHEMERAL_SIGNING_KEY = secrets.token_bytes(32)
_ephemeral_key_warned = False
_ephemeral_key_lock = threading.Lock()

logger = logging.getLogger(__name__)


class VoiceMessageError(Exception):
    """Base de los errores de dominio de mensajes de voz."""


class InvalidAudioError(VoiceMessageError):
    """El archivo no es audio/webm, está vacío o no tiene cabecera EBML (422)."""


class AudioTooLargeError(VoiceMessageError):
    """El archivo supera el tamaño máximo permitido (422)."""


class VoiceMessageNotFoundError(VoiceMessageError):
    """El mensaje de voz no existe o el enlace firmado no es válido (404)."""


class VoiceStorageUnavailableError(VoiceMessageError):
    """No hay dónde guardar los audios o no hay clave de firma (503)."""


class VoiceContactNotFoundError(VoiceMessageError):
    """El contacto no existe o está fuera del alcance del llamante (404)."""


class VoicePermissionDeniedError(VoiceMessageError):
    """Sin permiso: rol, contacto no vinculado o permissions.messages falso (403)."""


# --- Configuración: directorio y clave de firma ---


def _is_development(settings: Settings) -> bool:
    return getattr(settings, "environment", "") == DEVELOPMENT_ENVIRONMENT


def _configured_storage_dir(settings: Settings) -> Path:
    """Directorio de audios sin crearlo (D-R2-13).

    VOICE_MESSAGES_DIR relativo se resuelve contra backend-fastapi/, como la ruta de
    la cuenta de servicio. Vacío: carpeta temporal del sistema (fuera del repo, P11)
    en development; fuera de development, VoiceStorageUnavailableError.
    """
    configured = (getattr(settings, "voice_messages_dir", "") or "").strip()
    if configured:
        directory = Path(configured)
        if not directory.is_absolute():
            directory = BACKEND_DIR / directory
        return directory
    if _is_development(settings):
        return Path(tempfile.gettempdir()) / "connects-senior" / "voice-messages"
    raise VoiceStorageUnavailableError(_STORAGE_NOT_CONFIGURED_MESSAGE)


def storage_dir(settings: Settings) -> Path:
    """Directorio de audios, creado si no existe; OSError -> VoiceStorageUnavailableError."""
    directory = _configured_storage_dir(settings)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise VoiceStorageUnavailableError(_STORAGE_NOT_READY_MESSAGE) from error
    return directory


def _warn_ephemeral_key_once() -> None:
    global _ephemeral_key_warned
    with _ephemeral_key_lock:
        if _ephemeral_key_warned:
            return
        _ephemeral_key_warned = True
    # Nunca la clave ni ningún derivado.
    logger.warning(_EPHEMERAL_KEY_WARNING)


def signing_key(settings: Settings) -> bytes:
    """Clave HMAC de las audio_url (D-R2-13, P22).

    Configurada: sus bytes utf-8. Vacía en development: clave efímera del proceso, con
    una advertencia la primera vez. Vacía fuera de development:
    VoiceStorageUnavailableError (nunca se firma con una clave vacía).
    """
    configured = (getattr(settings, "voice_message_signing_key", "") or "").strip()
    if configured:
        return configured.encode("utf-8")
    if _is_development(settings):
        _warn_ephemeral_key_once()
        return _EPHEMERAL_SIGNING_KEY
    raise VoiceStorageUnavailableError(_SIGNING_KEY_NOT_CONFIGURED_MESSAGE)


# --- Firma de audio_url ---


def _now(now: datetime | None) -> datetime:
    current = now if now is not None else datetime.now(timezone.utc)
    return current.replace(microsecond=0)


def _signature(key: bytes, voice_message_id: str, exp: str) -> str:
    return hmac.new(
        key, f"{voice_message_id}:{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _audio_url(
    base_url: str, voice_message_id: str, key: bytes, now: datetime | None
) -> str:
    exp = str(int(_now(now).timestamp()) + AUDIO_URL_TTL_SECONDS)
    sig = _signature(key, voice_message_id, exp)
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    return (
        f"{base}api/calls/voice-messages/{quote(voice_message_id, safe='')}/audio"
        f"?sig={sig}&exp={exp}"
    )


def build_audio_url(
    base_url: str, voice_message_id: str, settings: Settings, now: datetime | None = None
) -> str:
    """URL absoluta y firmada del audio, válida AUDIO_URL_TTL_SECONDS desde now."""
    return _audio_url(base_url, voice_message_id, signing_key(settings), now)


def resolve_audio_path(
    voice_message_id: str,
    sig: str | None,
    exp: str | None,
    voice_messages: VoiceMessageRepository,
    settings: Settings,
    now: datetime | None = None,
) -> Path:
    """Archivo de audio de un enlace firmado válido.

    VoiceMessageNotFoundError ante cualquier fallo, sin revelar cuál: parámetros
    ausentes, exp no numérico o vencido, firma inválida, clave ausente fuera de
    development, registro inexistente, archivo fuera del directorio de audios o que no
    es un archivo. La ruta sale del registro (file_name), nunca del id de la URL, y se
    comprueba que queda dentro del directorio antes de tocar el archivo.
    """
    not_found = VoiceMessageNotFoundError(VOICE_MESSAGE_NOT_FOUND_DETAIL)
    if (
        not sig
        or not exp
        or len(exp) > _MAX_EXP_DIGITS
        or not exp.isascii()
        or not exp.isdigit()
    ):
        raise not_found
    if int(exp) <= int(_now(now).timestamp()):
        raise not_found
    try:
        key = signing_key(settings)
    except VoiceStorageUnavailableError:
        raise not_found from None
    expected = _signature(key, voice_message_id, exp)
    if not hmac.compare_digest(expected.encode("utf-8"), sig.encode("utf-8")):
        raise not_found

    record = voice_messages.get(voice_message_id)
    if record is None:
        raise not_found
    try:
        base = _configured_storage_dir(settings).resolve()
        candidate = (base / record.file_name).resolve()
    except (VoiceStorageUnavailableError, OSError, ValueError):
        raise not_found from None
    if candidate == base or not candidate.is_relative_to(base):
        raise not_found
    if not candidate.is_file():
        raise not_found
    return candidate


# --- V1: guardar ---


def _resolve_scope(
    user: AuthenticatedUser,
    contact_id: str,
    contacts: ContactRepository,
    users: UserRepository,
) -> ContactScope:
    try:
        return resolve_contact_scope(user, contact_id, contacts, users)
    except ContactScopeNotFoundError as error:
        raise VoiceContactNotFoundError(CONTACT_NOT_FOUND_DETAIL) from error
    except ContactScopeForbiddenError as error:
        # Incluye ContactNotLinkedError: familiar o cuidador no vinculado al contacto.
        raise VoicePermissionDeniedError(FORBIDDEN_DETAIL) from error


def _media_type(upload: UploadFile) -> str:
    """Tipo de medio sin parámetros ni mayúsculas ("audio/webm;codecs=opus" -> "audio/webm")."""
    return (upload.content_type or "").split(";", 1)[0].strip().lower()


def _sender_name(
    user: AuthenticatedUser, scope: ContactScope, users: UserRepository
) -> str:
    """from_name según D-R2-12.

    Adulto: nombre del perfil, display_name del token o uid. Contacto: display_name del
    token o "first_name last_name" del contacto.
    """
    if scope.from_adult:
        profile = users.get_profile(scope.adult_uid)
        if profile is not None and profile.full_name:
            return profile.full_name
        return user.display_name or user.uid
    contact = scope.contact
    return user.display_name or f"{contact.first_name} {contact.last_name}".strip()


def _push_recipient(scope: ContactScope, users: UserRepository) -> str | None:
    """Destinatario de la push según D-R2-12 (P25 e).

    Si envía el adulto: el uid verificado del contacto, solo si está vinculado al
    adulto (quien no puede leer la conversación no recibe push). Si envía el contacto:
    el adulto.
    """
    if not scope.from_adult:
        return scope.adult_uid
    uid = push_service.linked_uid_for_contact(scope.contact, users)
    if uid is None or not users.is_linked(uid, scope.adult_uid):
        return None
    return uid


def _persist_voice_message(
    user: AuthenticatedUser,
    scope: ContactScope,
    file_name: str,
    size_bytes: int,
    users: UserRepository,
    voice_messages: VoiceMessageRepository,
    now: datetime | None,
) -> tuple[VoiceMessageRecord, str]:
    """from_name y registro del mensaje; devuelve (registro, from_name).

    Síncrona: va por run_in_threadpool (D-R3-15). Un repositorio SQL lanza
    RecipientNotLinkedError si el contacto no corresponde a un usuario de la
    plataforma distinto del adulto (D-R3-13).
    """
    from_name = _sender_name(user, scope, users)
    record = voice_messages.add(
        VoiceMessageRecord(
            id=str(uuid4()),
            adult_uid=scope.adult_uid,
            contact_id=scope.contact.id,
            from_adult=scope.from_adult,
            from_name=from_name,
            file_name=file_name,
            content_type=AUDIO_CONTENT_TYPE,
            size_bytes=size_bytes,
            created_at=_now(now),
        )
    )
    return record, from_name


def _delete_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.error(
            "No se pudo borrar un archivo de audio incompleto (%s)",
            type(error).__name__,
        )


async def _store_upload(
    upload: UploadFile, directory: Path, max_bytes: int
) -> tuple[str, int]:
    """Copia el audio al directorio por bloques de CHUNK_SIZE; devuelve (file_name, tamaño).

    Comprueba la cabecera EBML en el primer bloque antes de escribir nada y aborta en
    cuanto el total supera max_bytes, borrando el archivo parcial. Nunca lee el archivo
    completo en memoria. OSError -> VoiceStorageUnavailableError (parcial borrado).
    """
    first_chunk = await upload.read(CHUNK_SIZE)
    if not first_chunk or not first_chunk.startswith(EBML_MAGIC):
        raise InvalidAudioError(INVALID_AUDIO_DETAIL)
    if len(first_chunk) > max_bytes:
        raise AudioTooLargeError(AUDIO_TOO_LARGE_DETAIL)

    file_name = f"{uuid4().hex}.webm"
    path = directory / file_name
    try:
        handle = await run_in_threadpool(path.open, "xb")
    except OSError as error:
        raise VoiceStorageUnavailableError(_STORAGE_WRITE_MESSAGE) from error

    total = 0
    try:
        try:
            chunk = first_chunk
            while chunk:
                total += len(chunk)
                if total > max_bytes:
                    raise AudioTooLargeError(AUDIO_TOO_LARGE_DETAIL)
                await run_in_threadpool(handle.write, chunk)
                chunk = await upload.read(CHUNK_SIZE)
        finally:
            await run_in_threadpool(handle.close)
    except OSError as error:
        await run_in_threadpool(_delete_quietly, path)
        raise VoiceStorageUnavailableError(_STORAGE_WRITE_MESSAGE) from error
    except BaseException:
        await run_in_threadpool(_delete_quietly, path)
        raise
    return file_name, total


async def save_voice_message(
    user: AuthenticatedUser,
    contact_id: str,
    upload: UploadFile,
    content_length: int | None,
    contacts: ContactRepository,
    users: UserRepository,
    voice_messages: VoiceMessageRepository,
    devices: DeviceRepository,
    settings: Settings,
    now: datetime | None = None,
) -> VoiceMessageCreateResponse:
    """V1: guarda un mensaje de voz del llamante al otro extremo del contacto.

    Orden (D-R2-13): alcance del contacto (404 o 403), permissions.messages (403),
    tipo de medio (422), Content-Length (422), directorio (503), lectura por bloques
    (422 o 503), registro y push (una push que falla no cambia el 201).
    """
    scope = await run_in_threadpool(
        _resolve_scope, user, contact_id.strip(), contacts, users
    )
    if not scope.contact.permissions.messages:
        raise VoicePermissionDeniedError(FORBIDDEN_DETAIL)
    if _media_type(upload) != AUDIO_CONTENT_TYPE:
        raise InvalidAudioError(INVALID_CONTENT_TYPE_DETAIL)

    max_bytes = int(settings.voice_message_max_bytes)
    if content_length is not None and content_length > max_bytes + MULTIPART_OVERHEAD_BYTES:
        raise AudioTooLargeError(AUDIO_TOO_LARGE_DETAIL)

    directory = await run_in_threadpool(storage_dir, settings)
    file_name, size_bytes = await _store_upload(upload, directory, max_bytes)

    try:
        record, from_name = await run_in_threadpool(
            _persist_voice_message,
            user,
            scope,
            file_name,
            size_bytes,
            users,
            voice_messages,
            now,
        )
    except RecipientNotLinkedError as error:
        # El destinatario no es un usuario de la plataforma: 403 y sin archivo huérfano.
        await run_in_threadpool(_delete_quietly, directory / file_name)
        raise VoicePermissionDeniedError(FORBIDDEN_DETAIL) from error
    except BaseException:
        # Sin registro el archivo quedaría huérfano.
        await run_in_threadpool(_delete_quietly, directory / file_name)
        raise

    try:
        await _notify_recipient(scope, record, from_name, users, devices)
    except Exception as error:
        # _notify_recipient no lanza; defensa en profundidad: el mensaje ya se guardó.
        logger.error(
            "No se pudo avisar del mensaje de voz (%s)", type(error).__name__
        )
    return VoiceMessageCreateResponse(id=record.id, created_at=record.created_at)


async def _notify_recipient(
    scope: ContactScope,
    record: VoiceMessageRecord,
    from_name: str,
    users: UserRepository,
    devices: DeviceRepository,
) -> None:
    """Push al destinatario del mensaje de voz; nunca lanza (D-R3-23)."""
    try:
        recipient = await run_in_threadpool(_push_recipient, scope, users)
    except Exception as error:
        # El mensaje ya se guardó: V1 responde 201 igual.
        logger.error(
            "No se pudo resolver el destinatario del mensaje de voz (%s)",
            type(error).__name__,
        )
        return
    if recipient is None:
        return
    data = {
        "type": "voice_message",
        "voice_message_id": record.id,
        "contact_id": record.contact_id,
        "adult_id": record.adult_uid,
        "from_name": from_name,
    }
    try:
        await run_in_threadpool(
            push_service.notify_users,
            [recipient],
            VOICE_PUSH_TITLE,
            f"{from_name} te envió un mensaje de voz",
            data,
            devices,
        )
    except Exception as error:
        # notify_users no lanza; esto es defensa en profundidad: el mensaje ya se guardó.
        logger.error(
            "No se pudo enviar la push del mensaje de voz (%s)", type(error).__name__
        )


# --- V2: listar ---


def list_received(
    user: AuthenticatedUser,
    base_url: str,
    contacts: ContactRepository,
    users: UserRepository,
    voice_messages: VoiceMessageRepository,
    settings: Settings,
    now: datetime | None = None,
) -> list[VoiceMessageResponse]:
    """V2: mensajes recibidos por quien llama, del más reciente al más antiguo.

    adulto_mayor: los que le enviaron sus contactos. Familiar o cuidador: los que cada
    adulto vinculado envió a los contactos que corresponden a su email verificado (sin
    email verificado, lista vacía). Administrador o sin rol: VoicePermissionDeniedError.
    La clave de firma se obtiene antes de consultar nada: fuera de development sin
    clave, VoiceStorageUnavailableError aunque la lista esté vacía.
    """
    key = signing_key(settings)

    if user.role == Role.ADULTO_MAYOR:
        records = voice_messages.list_received_by_adult(user.uid)
    else:
        try:
            pairs = recipient_contacts_for_user(user, contacts, users)
        except ContactScopeForbiddenError as error:
            raise VoicePermissionDeniedError(FORBIDDEN_DETAIL) from error
        records = [
            record
            for adult_uid, contact in pairs
            for record in voice_messages.list_sent_to_contact(adult_uid, contact.id)
        ]
        # sorted es estable también con reverse=True.
        records = sorted(records, key=lambda record: record.created_at, reverse=True)

    return [
        VoiceMessageResponse(
            id=record.id,
            from_name=record.from_name,
            created_at=record.created_at,
            audio_url=_audio_url(base_url, record.id, key, now),
        )
        for record in records
    ]
