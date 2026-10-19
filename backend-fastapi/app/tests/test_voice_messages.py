import asyncio
import hashlib
import hmac
import io
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from starlette.datastructures import Headers, UploadFile

from app.api import calls_router
from app.core.config import get_settings
from app.core.security import FORBIDDEN_DETAIL, get_current_user
from app.integrations import fcm_client
from app.integrations.fcm_client import FcmResult, FcmUnavailableError
from app.main import app
from app.models.auth import AuthenticatedUser
from app.models.contacts import Contact, ContactPermissions
from app.models.enums import Role
from app.models.users import UserProfile
from app.models.voice_messages import VoiceMessageRecord
from app.repositories.contact_repository import (
    InMemoryContactRepository,
    get_contact_repository,
)
from app.repositories.device_repository import (
    InMemoryDeviceRepository,
    get_device_repository,
)
from app.repositories.user_repository import (
    InMemoryUserRepository,
    get_user_repository,
)
from app.repositories.errors import RecipientNotLinkedError
from app.repositories.voice_message_repository import (
    InMemoryVoiceMessageRepository,
    get_voice_message_repository,
)
from app.services import push_service, voice_message_service
from app.services.voice_message_service import (
    AUDIO_URL_TTL_SECONDS,
    CHUNK_SIZE,
    EBML_MAGIC,
    MULTIPART_OVERHEAD_BYTES,
    AudioTooLargeError,
    VoiceMessageNotFoundError,
    VoiceStorageUnavailableError,
    build_audio_url,
    list_received,
    resolve_audio_path,
    save_voice_message,
    signing_key,
)


VOICE_PATH = "/api/calls/voice-messages"
BASE_URL = "http://testserver/"
ADULT_UID = "adult-uid"
OTHER_ADULT_UID = "other-adult-uid"
SECOND_ADULT_UID = "second-adult-uid"
FAMILY_UID = "family-uid"
CAREGIVER_UID = "caregiver-uid"
STRANGER_UID = "stranger-uid"
ADULT_NAME = "Rosa Gómez"
ADULT_TOKEN_NAME = "Rosita"
FAMILY_DISPLAY_NAME = "Ana Pérez Ruiz"
CONTACT_NAME = "Ana Pérez"
FAMILY_EMAIL = "ana.perez@example.com"
# Así lo escribió el familiar en C2: se compara normalizado.
STORED_CONTACT_EMAIL = " Ana.Perez@Example.com "
CONTACT_ID = "contact-ana"
FOREIGN_CONTACT_ID = "contact-ana-other-adult"
SECOND_CONTACT_ID = "contact-ana-second-adult"
ADULT_FCM_TOKEN = "fcm-token-adult"
FAMILY_FCM_TOKEN = "fcm-token-family"
SIGNING_KEY = "test-signing-key"
MAX_BYTES = 1024
WEBM = EBML_MAGIC + b"\x42\x86\x81\x01opus-audio"
BEARER_DETAIL = "Debes enviar un token Bearer."
CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."
VOICE_NOT_FOUND_DETAIL = "El mensaje de voz no existe."
UNAVAILABLE_DETAIL = (
    "El servicio de mensajes de voz no está disponible. Intenta más tarde."
)
INVALID_TYPE_DETAIL = "Datos inválidos: audio: debe ser un archivo audio/webm"
TOO_LARGE_DETAIL = "Datos inválidos: audio: supera el tamaño máximo permitido"
INVALID_AUDIO_DETAIL = "Datos inválidos: audio: el archivo está vacío o no es WebM"
VOICE_PUSH_TITLE = "Nuevo mensaje de voz"
T0 = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
SERVICE_LOGGER = "app.services.voice_message_service"


class FakeSendToTokens:
    """Sustituto de fcm_client.send_to_tokens: registra cada envío, sin red."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, tokens, title, body, data):
        self.calls.append(
            {"tokens": list(tokens), "title": title, "body": body, "data": dict(data)}
        )
        if self.error is not None:
            raise self.error
        return FcmResult(success_count=len(tokens), failure_count=0, invalid_tokens=[])


def _contact(contact_id: str = CONTACT_ID, *, messages: bool = True) -> Contact:
    return Contact(
        id=contact_id,
        first_name="Ana",
        last_name="Pérez",
        relationship="hija",
        email=STORED_CONTACT_EMAIL,
        permissions=ContactPermissions(
            calls=True, video_calls=True, messages=messages, help_alerts=True
        ),
    )


def _seed(state) -> None:
    state.users.add_profile(
        UserProfile(
            uid=ADULT_UID,
            first_name="Rosa",
            last_name="Gómez",
            email="rosa@example.com",
            role=Role.ADULTO_MAYOR,
        )
    )
    state.contacts.add(ADULT_UID, _contact(CONTACT_ID))
    # Mismo correo, pero de un adulto al que el familiar no está vinculado.
    state.contacts.add(OTHER_ADULT_UID, _contact(FOREIGN_CONTACT_ID))
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.users.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR)


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    shared = SimpleNamespace(
        contacts=InMemoryContactRepository(),
        users=InMemoryUserRepository(),
        voice_messages=InMemoryVoiceMessageRepository(),
        devices=InMemoryDeviceRepository(),
        push=FakeSendToTokens(),
        tmp_path=tmp_path,
        storage=tmp_path / "voice",
        settings=SimpleNamespace(
            environment="development",
            voice_messages_dir=str(tmp_path / "voice"),
            voice_message_max_bytes=MAX_BYTES,
            voice_message_signing_key=SIGNING_KEY,
        ),
    )
    monkeypatch.setattr(fcm_client, "send_to_tokens", shared.push)
    monkeypatch.setattr(voice_message_service, "_ephemeral_key_warned", False)
    app.dependency_overrides[get_contact_repository] = lambda: shared.contacts
    app.dependency_overrides[get_user_repository] = lambda: shared.users
    app.dependency_overrides[get_voice_message_repository] = (
        lambda: shared.voice_messages
    )
    app.dependency_overrides[get_device_repository] = lambda: shared.devices
    # Nunca el get_settings real: no se lee el .env ni se escribe fuera de tmp_path.
    app.dependency_overrides[get_settings] = lambda: shared.settings
    _seed(shared)
    yield shared
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _adult_user(uid: str = ADULT_UID) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=f"{uid}@example.com",
        email_verified=True,
        display_name=ADULT_TOKEN_NAME,
        role=Role.ADULTO_MAYOR,
    )


def _family_user(
    uid: str = FAMILY_UID,
    *,
    role: Role = Role.FAMILIAR,
    email: str | None = FAMILY_EMAIL,
    verified: bool | None = True,
    display_name: str | None = FAMILY_DISPLAY_NAME,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=email,
        email_verified=verified,
        display_name=display_name,
        role=role,
    )


def _as(user: AuthenticatedUser) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _post_voice(
    client: TestClient,
    *,
    contact_id: str = CONTACT_ID,
    content: bytes = WEBM,
    content_type: str = "audio/webm",
    filename: str = "clip.webm",
):
    return client.post(
        VOICE_PATH,
        data={"contact_id": contact_id},
        files={"audio": (filename, content, content_type)},
    )


def _stored_files(state) -> list[Path]:
    if not state.storage.exists():
        return []
    return sorted(path for path in state.storage.rglob("*") if path.is_file())


def _upload(content: bytes, content_type: str = "audio/webm") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename="clip.webm",
        headers=Headers({"content-type": content_type}),
    )


async def _save(
    state,
    user: AuthenticatedUser,
    *,
    contact_id: str = CONTACT_ID,
    content: bytes = WEBM,
    now: datetime | None = None,
    content_length: int | None = None,
):
    return await save_voice_message(
        user,
        contact_id,
        _upload(content),
        content_length,
        state.contacts,
        state.users,
        state.voice_messages,
        state.devices,
        state.settings,
        now=now,
    )


def _url_parts(url: str) -> tuple[str, dict[str, str]]:
    split = urlsplit(url)
    return split.path, {key: values[0] for key, values in parse_qs(split.query).items()}


def _sign(voice_message_id: str, exp: str, key: bytes = SIGNING_KEY.encode()) -> str:
    return hmac.new(
        key, f"{voice_message_id}:{exp}".encode(), hashlib.sha256
    ).hexdigest()


def _audio_path(voice_message_id: str) -> str:
    return f"{VOICE_PATH}/{voice_message_id}/audio"


def _valid_params(voice_message_id: str) -> dict[str, str]:
    exp = str(int(datetime.now(timezone.utc).timestamp()) + AUDIO_URL_TTL_SECONDS)
    return {"sig": _sign(voice_message_id, exp), "exp": exp}


def _record(
    voice_message_id: str,
    *,
    file_name: str,
    adult_uid: str = ADULT_UID,
    from_adult: bool = False,
) -> VoiceMessageRecord:
    return VoiceMessageRecord(
        id=voice_message_id,
        adult_uid=adult_uid,
        contact_id=CONTACT_ID,
        from_adult=from_adult,
        from_name=FAMILY_DISPLAY_NAME,
        file_name=file_name,
        content_type="audio/webm",
        size_bytes=len(WEBM),
        created_at=T0,
    )


class PathIsFileSpy:
    """Registra las llamadas a Path.is_file sobre rutas de la prueba (tmp_path)."""

    def __init__(self, monkeypatch, root: Path) -> None:
        self.calls: list[Path] = []
        self._root = str(root)
        original = Path.is_file

        def spy(path_self, *args, **kwargs):
            if str(path_self).startswith(self._root):
                self.calls.append(path_self)
            return original(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", spy)


class FileResponseSpy:
    """Sustituye FileResponse en el router para comprobar que no se abre ningún archivo."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[tuple] = []
        original = calls_router.FileResponse

        def spy(*args, **kwargs):
            self.calls.append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(calls_router, "FileResponse", spy)


# --- V1 POST /api/calls/voice-messages: adulto mayor ---


def test_v1_adult_201_saves_uuid_file_ignoring_client_filename(state, client):
    _as(_adult_user())
    before = datetime.now(timezone.utc).replace(microsecond=0)

    response = _post_voice(client, filename="../../x.webm")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "created_at"}
    created_at = datetime.fromisoformat(body["created_at"])
    assert created_at.tzinfo is not None
    assert before <= created_at <= datetime.now(timezone.utc)
    record = state.voice_messages.get(body["id"])
    assert record is not None
    assert re.fullmatch(r"[0-9a-f]{32}\.webm", record.file_name)
    assert record.adult_uid == ADULT_UID
    assert record.contact_id == CONTACT_ID
    assert record.from_adult is True
    # from_name del adulto: nombre del perfil antes que el display_name del token.
    assert record.from_name == ADULT_NAME
    assert record.content_type == "audio/webm"
    assert record.size_bytes == len(WEBM)
    assert _stored_files(state) == [state.storage / record.file_name]
    assert (state.storage / record.file_name).read_bytes() == WEBM
    # El nombre del cliente nunca se usa: nada escapa del directorio.
    assert not (state.tmp_path.parent / "x.webm").exists()
    assert not any(path.name == "x.webm" for path in state.tmp_path.rglob("*"))


@pytest.mark.parametrize(
    "content_type", ["audio/webm;codecs=opus", "Audio/WebM; codecs=opus", "audio/webm"]
)
def test_v1_content_type_with_codecs_accepted(state, client, content_type):
    _as(_adult_user())

    response = _post_voice(client, content_type=content_type)

    assert response.status_code == 201
    record = state.voice_messages.get(response.json()["id"])
    assert record.content_type == "audio/webm"
    assert len(_stored_files(state)) == 1


@pytest.mark.parametrize("content_type", ["audio/mpeg", "video/webm", "audio/webmx"])
def test_v1_content_type_mpeg_422(state, client, content_type):
    _as(_adult_user())

    response = _post_voice(client, content_type=content_type)

    assert response.status_code == 422
    assert response.json() == {"detail": INVALID_TYPE_DETAIL}
    assert _stored_files(state) == []
    assert state.voice_messages.list_sent_to_contact(ADULT_UID, CONTACT_ID) == []


def test_v1_missing_content_type_422(state, client):
    _as(_adult_user())
    boundary = "voice-test-boundary"
    # httpx siempre pone Content-Type a la parte del archivo: el cuerpo se arma a mano.
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="contact_id"\r\n\r\n'
            f"{CONTACT_ID}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="audio"; filename="clip.webm"\r\n\r\n'
        ).encode()
        + WEBM
        + f"\r\n--{boundary}--\r\n".encode()
    )

    response = client.post(
        VOICE_PATH,
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": INVALID_TYPE_DETAIL}
    assert _stored_files(state) == []


def test_v1_content_length_over_limit_422_without_reading(state, client, monkeypatch):
    _as(_adult_user())
    reads: list[int] = []
    original_read = UploadFile.read

    async def spy_read(self, size: int = -1) -> bytes:
        reads.append(size)
        return await original_read(self, size)

    monkeypatch.setattr(UploadFile, "read", spy_read)
    content = WEBM + b"\x00" * (MAX_BYTES + MULTIPART_OVERHEAD_BYTES)

    response = _post_voice(client, content=content)

    assert response.status_code == 422
    assert response.json() == {"detail": TOO_LARGE_DETAIL}
    assert reads == []
    assert _stored_files(state) == []
    assert state.voice_messages.list_sent_to_contact(ADULT_UID, CONTACT_ID) == []


def test_v1_body_over_limit_422_partial_deleted(state, client):
    _as(_adult_user())
    content = WEBM + b"\x00" * (MAX_BYTES + 1 - len(WEBM))
    assert len(content) == MAX_BYTES + 1

    response = _post_voice(client, content=content)

    assert response.status_code == 422
    assert response.json() == {"detail": TOO_LARGE_DETAIL}
    assert _stored_files(state) == []
    assert state.voice_messages.list_sent_to_contact(ADULT_UID, CONTACT_ID) == []


def test_v1_exactly_max_bytes_is_accepted(state, client):
    _as(_adult_user())
    content = WEBM + b"\x00" * (MAX_BYTES - len(WEBM))

    response = _post_voice(client, content=content)

    assert response.status_code == 201
    record = state.voice_messages.get(response.json()["id"])
    assert record.size_bytes == MAX_BYTES
    assert (state.storage / record.file_name).read_bytes() == content


@pytest.mark.asyncio
async def test_v1_multi_chunk_over_limit_deletes_partial_file(state, monkeypatch):
    state.settings.voice_message_max_bytes = CHUNK_SIZE + 10
    content = WEBM + b"\x00" * (3 * CHUNK_SIZE)
    deleted: list[tuple[Path, int | None]] = []
    original_delete = voice_message_service._delete_quietly

    def spy_delete(path: Path) -> None:
        deleted.append((path, path.stat().st_size if path.exists() else None))
        original_delete(path)

    monkeypatch.setattr(voice_message_service, "_delete_quietly", spy_delete)

    with pytest.raises(AudioTooLargeError):
        await _save(state, _adult_user(), content=content)

    # Se escribió el primer bloque y se borró al superar el máximo con el segundo.
    assert len(deleted) == 1
    partial_path, partial_size = deleted[0]
    assert partial_path.parent == state.storage
    assert partial_size == CHUNK_SIZE
    assert not partial_path.exists()
    assert _stored_files(state) == []
    assert state.voice_messages.list_sent_to_contact(ADULT_UID, CONTACT_ID) == []


@pytest.mark.asyncio
async def test_v1_write_error_503_partial_deleted(state, monkeypatch):
    original_open = Path.open

    class FailingWriter:
        def __init__(self, handle) -> None:
            self._handle = handle

        def write(self, chunk: bytes) -> int:
            self._handle.write(chunk[:10])
            raise OSError("disco lleno")

        def close(self) -> None:
            self._handle.close()

    def failing_open(path_self, *args, **kwargs):
        handle = original_open(path_self, *args, **kwargs)
        if path_self.parent == state.storage:
            return FailingWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(VoiceStorageUnavailableError) as error_info:
        await _save(state, _adult_user())

    assert "disco lleno" not in str(error_info.value)
    assert _stored_files(state) == []
    assert state.voice_messages.list_sent_to_contact(ADULT_UID, CONTACT_ID) == []


@pytest.mark.parametrize(
    "content",
    [b"", b"RIFF\x24\x00\x00\x00WAVEfmt ", EBML_MAGIC[:2], b"\x00" + EBML_MAGIC],
)
def test_v1_empty_or_non_webm_422(state, client, content):
    _as(_adult_user())

    response = _post_voice(client, content=content)

    assert response.status_code == 422
    assert response.json() == {"detail": INVALID_AUDIO_DETAIL}
    assert _stored_files(state) == []
    assert state.voice_messages.list_sent_to_contact(ADULT_UID, CONTACT_ID) == []


@pytest.mark.parametrize("contact_id", [FOREIGN_CONTACT_ID, "missing-contact", "   "])
def test_v1_adult_foreign_contact_404(state, client, contact_id):
    _as(_adult_user())

    response = _post_voice(client, contact_id=contact_id)

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert _stored_files(state) == []
    assert state.voice_messages.list_received_by_adult(OTHER_ADULT_UID) == []


@pytest.mark.parametrize("sender", ["adult", "family"])
def test_v1_permissions_messages_false_403_no_file_written(state, client, sender):
    state.contacts.update(ADULT_UID, _contact(CONTACT_ID, messages=False))
    _as(_adult_user() if sender == "adult" else _family_user())

    response = _post_voice(client)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _stored_files(state) == []
    assert not state.storage.exists()
    assert state.voice_messages.list_received_by_adult(ADULT_UID) == []
    assert state.voice_messages.list_sent_to_contact(ADULT_UID, CONTACT_ID) == []


# --- V1: familiar y cuidador (P19) ---


@pytest.mark.parametrize(
    ("role", "uid"), [(Role.FAMILIAR, FAMILY_UID), (Role.CUIDADOR, CAREGIVER_UID)]
)
def test_v1_family_linked_201_saved_as_contact_to_adult(state, client, role, uid):
    _as(_family_user(uid, role=role))

    response = _post_voice(client)

    assert response.status_code == 201
    record = state.voice_messages.get(response.json()["id"])
    assert record is not None
    assert record.adult_uid == ADULT_UID
    assert record.contact_id == CONTACT_ID
    assert record.from_adult is False
    assert record.from_name == FAMILY_DISPLAY_NAME
    assert (state.storage / record.file_name).read_bytes() == WEBM
    assert state.voice_messages.list_received_by_adult(ADULT_UID) == [record]


def test_v1_family_without_display_name_uses_contact_name(state, client):
    _as(_family_user(display_name=None))

    response = _post_voice(client)

    assert response.status_code == 201
    record = state.voice_messages.get(response.json()["id"])
    assert record.from_name == CONTACT_NAME


@pytest.mark.parametrize("verified", [False, None])
def test_v1_family_email_unverified_403(state, client, verified):
    _as(_family_user(verified=verified))

    response = _post_voice(client)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _stored_files(state) == []
    assert state.voice_messages.list_received_by_adult(ADULT_UID) == []


@pytest.mark.parametrize("email", ["otra.persona@example.com", None])
def test_v1_family_email_mismatch_403(state, client, email):
    _as(_family_user(email=email))

    response = _post_voice(client)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _stored_files(state) == []
    assert state.voice_messages.list_received_by_adult(ADULT_UID) == []


@pytest.mark.parametrize(
    ("uid", "contact_id"),
    [(FAMILY_UID, FOREIGN_CONTACT_ID), (STRANGER_UID, CONTACT_ID), (FAMILY_UID, "missing")],
)
def test_v1_family_unlinked_adult_404(state, client, uid, contact_id):
    _as(_family_user(uid))

    response = _post_voice(client, contact_id=contact_id)

    assert response.status_code == 404
    assert response.json() == {"detail": CONTACT_NOT_FOUND_DETAIL}
    assert _stored_files(state) == []
    assert state.voice_messages.list_received_by_adult(ADULT_UID) == []
    assert state.voice_messages.list_received_by_adult(OTHER_ADULT_UID) == []


@pytest.mark.parametrize(
    ("data", "files", "detail"),
    [
        (None, {"audio": ("clip.webm", WEBM, "audio/webm")}, "contact_id: es obligatorio"),
        ({"contact_id": ""}, {"audio": ("clip.webm", WEBM, "audio/webm")}, "contact_id: es obligatorio"),
        ({"contact_id": CONTACT_ID}, None, "audio: es obligatorio"),
        (None, None, "contact_id: es obligatorio; audio: es obligatorio"),
    ],
)
def test_v1_missing_contact_id_or_audio_422(state, client, data, files, detail):
    _as(_adult_user())

    response = client.post(VOICE_PATH, data=data, files=files)

    assert response.status_code == 422
    assert response.json() == {"detail": f"Datos inválidos: {detail}"}
    assert _stored_files(state) == []


# --- V1: push al destinatario (D-R2-12, D-R2-13) ---


def test_v1_adult_pushes_linked_contact(state, client):
    state.users.mark_email_verified(FAMILY_UID, FAMILY_EMAIL)
    state.devices.register(FAMILY_UID, FAMILY_FCM_TOKEN)
    state.devices.register(ADULT_UID, ADULT_FCM_TOKEN)
    _as(_adult_user())

    response = _post_voice(client)

    assert response.status_code == 201
    voice_message_id = response.json()["id"]
    assert state.push.calls == [
        {
            "tokens": [FAMILY_FCM_TOKEN],
            "title": VOICE_PUSH_TITLE,
            "body": f"{ADULT_NAME} te envió un mensaje de voz",
            "data": {
                "type": "voice_message",
                "voice_message_id": voice_message_id,
                "contact_id": CONTACT_ID,
                "adult_id": ADULT_UID,
                "from_name": ADULT_NAME,
            },
        }
    ]


def test_v1_family_pushes_adult(state, client):
    state.users.mark_email_verified(FAMILY_UID, FAMILY_EMAIL)
    state.devices.register(FAMILY_UID, FAMILY_FCM_TOKEN)
    state.devices.register(ADULT_UID, ADULT_FCM_TOKEN)
    _as(_family_user())

    response = _post_voice(client)

    assert response.status_code == 201
    assert state.push.calls == [
        {
            "tokens": [ADULT_FCM_TOKEN],
            "title": VOICE_PUSH_TITLE,
            "body": f"{FAMILY_DISPLAY_NAME} te envió un mensaje de voz",
            "data": {
                "type": "voice_message",
                "voice_message_id": response.json()["id"],
                "contact_id": CONTACT_ID,
                "adult_id": ADULT_UID,
                "from_name": FAMILY_DISPLAY_NAME,
            },
        }
    ]


@pytest.mark.parametrize("case", ["verified_uid_not_linked", "email_not_verified"])
def test_v1_adult_no_push_when_contact_uid_not_linked_to_adult(state, client, case):
    if case == "verified_uid_not_linked":
        # La cuenta verificada con ese correo no está vinculada al adulto.
        state.users.mark_email_verified(STRANGER_UID, FAMILY_EMAIL)
        state.devices.register(STRANGER_UID, "fcm-token-stranger")
    state.devices.register(FAMILY_UID, FAMILY_FCM_TOKEN)
    _as(_adult_user())

    response = _post_voice(client)

    assert response.status_code == 201
    assert state.push.calls == []
    assert state.voice_messages.get(response.json()["id"]) is not None


@pytest.mark.parametrize("failure", ["fcm_unavailable", "unexpected"])
def test_v1_push_failure_still_201(state, client, caplog, monkeypatch, failure):
    state.devices.register(ADULT_UID, ADULT_FCM_TOKEN)
    if failure == "fcm_unavailable":
        state.push.error = FcmUnavailableError(
            "El servicio de notificaciones no está disponible."
        )
    else:

        def exploding_notify_users(*args, **kwargs):
            raise RuntimeError("fallo inesperado")

        monkeypatch.setattr(push_service, "notify_users", exploding_notify_users)
    _as(_family_user())

    with caplog.at_level(logging.ERROR):
        response = _post_voice(client)

    assert response.status_code == 201
    record = state.voice_messages.get(response.json()["id"])
    assert record is not None
    assert (state.storage / record.file_name).read_bytes() == WEBM
    if failure == "fcm_unavailable":
        assert len(state.push.calls) == 1
        assert "FcmUnavailableError" in caplog.text
    else:
        assert "RuntimeError" in caplog.text
    assert ADULT_FCM_TOKEN not in caplog.text


def test_v1_storage_not_writable_503(state, client, caplog):
    blocker = state.tmp_path / "blocker"
    blocker.write_bytes(b"no soy un directorio")
    state.settings.voice_messages_dir = str(blocker / "voice")
    _as(_adult_user())

    with caplog.at_level(logging.ERROR, logger="app.api.calls_router"):
        response = _post_voice(client)

    assert response.status_code == 503
    assert response.json() == {"detail": UNAVAILABLE_DETAIL}
    assert "VoiceStorageUnavailableError" in caplog.text
    assert str(state.tmp_path) not in caplog.text
    assert str(state.tmp_path) not in response.text
    assert state.voice_messages.list_sent_to_contact(ADULT_UID, CONTACT_ID) == []
    assert state.push.calls == []


# --- V2 GET /api/calls/voice-messages ---


def test_v2_adult_lists_received_with_absolute_audio_url_and_from_name(state, client):
    _as(_family_user())
    received_id = _post_voice(client).json()["id"]
    _as(_adult_user())
    # Lo que envía el propio adulto no aparece en sus recibidos.
    assert _post_voice(client).status_code == 201

    response = client.get(VOICE_PATH)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    item = body[0]
    assert set(item) == {"id", "from_name", "created_at", "audio_url"}
    assert item["id"] == received_id
    assert item["from_name"] == FAMILY_DISPLAY_NAME
    assert datetime.fromisoformat(item["created_at"]).tzinfo is not None
    split = urlsplit(item["audio_url"])
    assert (split.scheme, split.netloc) == ("http", "testserver")
    assert split.path == _audio_path(received_id)
    _, params = _url_parts(item["audio_url"])
    assert set(params) == {"sig", "exp"}
    assert re.fullmatch(r"[0-9a-f]{64}", params["sig"])
    assert params["sig"] == _sign(received_id, params["exp"])
    remaining = int(params["exp"]) - int(datetime.now(timezone.utc).timestamp())
    assert AUDIO_URL_TTL_SECONDS - 5 <= remaining <= AUDIO_URL_TTL_SECONDS


@pytest.mark.parametrize(
    ("role", "uid"), [(Role.FAMILIAR, FAMILY_UID), (Role.CUIDADOR, CAREGIVER_UID)]
)
def test_v2_family_linked_lists_adult_messages(state, client, role, uid):
    _as(_adult_user())
    sent_id = _post_voice(client).json()["id"]
    # Mensaje de otro adulto a un contacto con el mismo correo: el familiar no está vinculado.
    _as(_adult_user(OTHER_ADULT_UID))
    assert _post_voice(client, contact_id=FOREIGN_CONTACT_ID).status_code == 201
    # Lo que envía el propio familiar no aparece en sus recibidos.
    _as(_family_user(uid, role=role))
    assert _post_voice(client).status_code == 201

    response = client.get(VOICE_PATH)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [sent_id]
    assert body[0]["from_name"] == ADULT_NAME
    assert urlsplit(body[0]["audio_url"]).path == _audio_path(sent_id)


@pytest.mark.parametrize(
    ("email", "verified"),
    [(FAMILY_EMAIL, False), (FAMILY_EMAIL, None), ("otra.persona@example.com", True)],
)
def test_v2_family_email_unverified_returns_empty(state, client, email, verified):
    _as(_adult_user())
    assert _post_voice(client).status_code == 201
    _as(_family_user(email=email, verified=verified))

    response = client.get(VOICE_PATH)

    assert response.status_code == 200
    assert response.json() == []


def test_v2_order_newest_first(state, client):
    family = _family_user()
    adult = _adult_user()
    second_adult = _adult_user(SECOND_ADULT_UID)
    state.users.link_adult(FAMILY_UID, SECOND_ADULT_UID, Role.FAMILIAR)
    state.contacts.add(SECOND_ADULT_UID, _contact(SECOND_CONTACT_ID))

    def save(user, minutes: int, contact_id: str = CONTACT_ID) -> str:
        result = asyncio.run(
            _save(state, user, contact_id=contact_id, now=T0 + timedelta(minutes=minutes))
        )
        return result.id

    # Recibidos por el adulto (insertados fuera de orden).
    received_old = save(family, 0)
    received_new = save(family, 120)
    received_mid = save(family, 60)
    # Recibidos por el familiar desde dos adultos, intercalados en el tiempo.
    from_adult_10 = save(adult, 10)
    from_second_40 = save(second_adult, 40, SECOND_CONTACT_ID)
    from_adult_30 = save(adult, 30)
    from_second_5 = save(second_adult, 5, SECOND_CONTACT_ID)

    _as(adult)
    adult_body = client.get(VOICE_PATH).json()
    _as(family)
    family_body = client.get(VOICE_PATH).json()

    assert [item["id"] for item in adult_body] == [received_new, received_mid, received_old]
    assert [item["id"] for item in family_body] == [
        from_second_40,
        from_adult_30,
        from_adult_10,
        from_second_5,
    ]
    assert [datetime.fromisoformat(item["created_at"]) for item in family_body] == [
        T0 + timedelta(minutes=minutes) for minutes in (40, 30, 10, 5)
    ]
    second_item = family_body[0]
    assert second_item["from_name"] == ADULT_TOKEN_NAME


def test_repository_ties_newest_inserted_first(state):
    first = state.voice_messages.add(_record("first", file_name="a.webm"))
    second = state.voice_messages.add(_record("second", file_name="b.webm"))
    older = state.voice_messages.add(
        replace(_record("older", file_name="c.webm"), created_at=T0 - timedelta(minutes=1))
    )

    listed = state.voice_messages.list_received_by_adult(ADULT_UID)

    assert [record.id for record in listed] == [second.id, first.id, older.id]
    listed[0].from_name = "modificado"
    assert state.voice_messages.get(second.id).from_name == FAMILY_DISPLAY_NAME


# --- Ruta de audio GET /api/calls/voice-messages/{id}/audio (sin Bearer) ---


def test_audio_url_plays_without_authorization_200_webm(state, client, monkeypatch):
    _as(_family_user())
    _post_voice(client)
    _as(_adult_user())
    audio_url = client.get(VOICE_PATH).json()[0]["audio_url"]
    app.dependency_overrides.pop(get_current_user)
    is_file_spy = PathIsFileSpy(monkeypatch, state.tmp_path)
    file_response_spy = FileResponseSpy(monkeypatch)

    response = client.get(audio_url)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/webm")
    assert response.content == WEBM
    assert "authorization" not in {key.lower() for key in response.request.headers}
    # Los espías sí registran el camino feliz: su silencio en los 404 es significativo.
    assert len(is_file_spy.calls) == 1
    assert len(file_response_spy.calls) == 1


@pytest.mark.parametrize(
    "tamper", ["flipped", "empty", "other_id", "non_ascii", "uppercase"]
)
def test_audio_invalid_signature_404(state, client, tamper):
    _as(_family_user())
    voice_message_id = _post_voice(client).json()["id"]
    params = _valid_params(voice_message_id)
    if tamper == "flipped":
        params["sig"] = ("0" if params["sig"][0] != "0" else "1") + params["sig"][1:]
    elif tamper == "empty":
        params["sig"] = ""
    elif tamper == "other_id":
        params["sig"] = _sign("otro-id", params["exp"])
    elif tamper == "non_ascii":
        params["sig"] = "ñ" * 64
    else:
        params["sig"] = params["sig"].upper()
    app.dependency_overrides.pop(get_current_user)

    response = client.get(_audio_path(voice_message_id), params=params)

    assert response.status_code == 404
    assert response.json() == {"detail": VOICE_NOT_FOUND_DETAIL}


def test_audio_expired_404(state, client):
    _as(_family_user())
    voice_message_id = _post_voice(client).json()["id"]
    now = datetime.now(timezone.utc)
    expired_url = build_audio_url(
        BASE_URL,
        voice_message_id,
        state.settings,
        now=now - timedelta(seconds=AUDIO_URL_TTL_SECONDS + 5),
    )
    still_valid_url = build_audio_url(
        BASE_URL,
        voice_message_id,
        state.settings,
        now=now - timedelta(seconds=AUDIO_URL_TTL_SECONDS - 60),
    )

    expired = client.get(expired_url)
    still_valid = client.get(still_valid_url)

    assert expired.status_code == 404
    assert expired.json() == {"detail": VOICE_NOT_FOUND_DETAIL}
    assert still_valid.status_code == 200
    # Justo en exp ya no vale.
    _, params = _url_parts(still_valid_url)
    exp_moment = datetime.fromtimestamp(int(params["exp"]), tz=timezone.utc)
    with pytest.raises(VoiceMessageNotFoundError):
        resolve_audio_path(
            voice_message_id,
            params["sig"],
            params["exp"],
            state.voice_messages,
            state.settings,
            now=exp_moment,
        )


@pytest.mark.parametrize("exp", ["abc", "-1", "1e12", "12.5", " 1", "9" * 30, "١٢٣"])
def test_audio_non_numeric_exp_404(state, client, exp):
    _as(_family_user())
    voice_message_id = _post_voice(client).json()["id"]
    # Firma correcta para ese exp: el rechazo se debe solo al formato de exp.
    params = {"sig": _sign(voice_message_id, exp), "exp": exp}

    response = client.get(_audio_path(voice_message_id), params=params)

    assert response.status_code == 404
    assert response.json() == {"detail": VOICE_NOT_FOUND_DETAIL}


@pytest.mark.parametrize("keep", [(), ("sig",), ("exp",)])
def test_audio_missing_params_404(state, client, keep):
    _as(_family_user())
    voice_message_id = _post_voice(client).json()["id"]
    params = {key: value for key, value in _valid_params(voice_message_id).items() if key in keep}

    response = client.get(_audio_path(voice_message_id), params=params)

    assert response.status_code == 404
    assert response.json() == {"detail": VOICE_NOT_FOUND_DETAIL}


@pytest.mark.parametrize(
    ("raw_id", "signed_id"),
    [("missing-id", "missing-id"), ("..%2F..%2Foutside", "../../outside")],
)
def test_audio_unknown_id_404(state, client, monkeypatch, raw_id, signed_id):
    state.storage.mkdir(parents=True)
    (state.tmp_path / "outside").write_bytes(WEBM)
    is_file_spy = PathIsFileSpy(monkeypatch, state.tmp_path)
    file_response_spy = FileResponseSpy(monkeypatch)
    params = _valid_params(signed_id)

    response = client.get(
        f"{VOICE_PATH}/{raw_id}/audio?sig={params['sig']}&exp={params['exp']}"
    )

    assert response.status_code == 404
    if raw_id == "missing-id":
        assert response.json() == {"detail": VOICE_NOT_FOUND_DETAIL}
    assert is_file_spy.calls == []
    assert file_response_spy.calls == []


@pytest.mark.parametrize(
    "file_name",
    ["../outside.webm", "sub/../../outside.webm", "ABSOLUTE", ""],
)
def test_audio_path_traversal_record_404_without_opening_file(
    state, client, monkeypatch, file_name
):
    state.storage.mkdir(parents=True)
    outside = state.tmp_path / "outside.webm"
    outside.write_bytes(WEBM)
    if file_name == "ABSOLUTE":
        file_name = str(outside)
    state.voice_messages.add(_record("evil", file_name=file_name))
    params = _valid_params("evil")
    is_file_spy = PathIsFileSpy(monkeypatch, state.tmp_path)
    file_response_spy = FileResponseSpy(monkeypatch)

    with pytest.raises(VoiceMessageNotFoundError):
        resolve_audio_path(
            "evil", params["sig"], params["exp"], state.voice_messages, state.settings
        )
    response = client.get(_audio_path("evil"), params=params)

    assert response.status_code == 404
    assert response.json() == {"detail": VOICE_NOT_FOUND_DETAIL}
    assert is_file_spy.calls == []
    assert file_response_spy.calls == []


def test_audio_record_without_file_404(state, client):
    _as(_family_user())
    voice_message_id = _post_voice(client).json()["id"]
    for path in _stored_files(state):
        path.unlink()

    response = client.get(_audio_path(voice_message_id), params=_valid_params(voice_message_id))

    assert response.status_code == 404
    assert response.json() == {"detail": VOICE_NOT_FOUND_DETAIL}


# --- Clave de firma (D-R2-13, P22) ---


@pytest.mark.parametrize("configured_key", ["", "   "])
def test_ephemeral_key_in_development_warns_once_and_signs(
    state, client, caplog, configured_key
):
    state.settings.voice_message_signing_key = configured_key
    ephemeral_key = voice_message_service._EPHEMERAL_SIGNING_KEY

    with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
        first_key = signing_key(state.settings)
        url = build_audio_url(BASE_URL, "some-id", state.settings)
        _as(_family_user())
        voice_message_id = _post_voice(client).json()["id"]
        _as(_adult_user())
        audio_url = client.get(VOICE_PATH).json()[0]["audio_url"]
        played = client.get(audio_url)

    assert first_key == ephemeral_key
    assert len(ephemeral_key) == 32
    _, params = _url_parts(url)
    assert params["sig"] == _sign("some-id", params["exp"], ephemeral_key)
    assert urlsplit(audio_url).path == _audio_path(voice_message_id)
    assert played.status_code == 200
    assert played.content == WEBM
    warnings = [
        record for record in caplog.records
        if record.name == SERVICE_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert ephemeral_key.hex() not in caplog.text
    # Una firma con la clave de los tests ya no vale.
    assert client.get(
        _audio_path(voice_message_id), params=_valid_params(voice_message_id)
    ).status_code == 404


def test_empty_key_outside_development_503(state, client, caplog):
    state.settings.environment = "production"
    state.settings.voice_message_signing_key = ""
    _as(_adult_user())

    with caplog.at_level(logging.ERROR, logger="app.api.calls_router"):
        empty_list = client.get(VOICE_PATH)
        _as(_family_user())
        created = _post_voice(client)
        _as(_adult_user())
        with_messages = client.get(VOICE_PATH)

    assert empty_list.status_code == 503
    assert empty_list.json() == {"detail": UNAVAILABLE_DETAIL}
    # V1 no firma nada: con directorio configurado funciona.
    assert created.status_code == 201
    assert with_messages.status_code == 503
    assert with_messages.json() == {"detail": UNAVAILABLE_DETAIL}
    assert "VoiceStorageUnavailableError" in caplog.text
    # Sin clave fuera de development la ruta de audio no sirve nada.
    voice_message_id = created.json()["id"]
    exp = str(int(datetime.now(timezone.utc).timestamp()) + 60)
    ephemeral_sig = _sign(voice_message_id, exp, voice_message_service._EPHEMERAL_SIGNING_KEY)
    for sig in (ephemeral_sig, _sign(voice_message_id, exp, b"")):
        audio = client.get(_audio_path(voice_message_id), params={"sig": sig, "exp": exp})
        assert audio.status_code == 404
        assert audio.json() == {"detail": VOICE_NOT_FOUND_DETAIL}

    # V1 sin directorio configurado fuera de development: 503 sin escribir nada.
    state.settings.voice_messages_dir = ""
    _as(_family_user())
    no_directory = _post_voice(client)

    assert no_directory.status_code == 503
    assert no_directory.json() == {"detail": UNAVAILABLE_DETAIL}
    assert len(state.voice_messages.list_received_by_adult(ADULT_UID)) == 1


def test_list_received_checks_key_before_repositories(state):
    state.settings.environment = "production"
    state.settings.voice_message_signing_key = ""

    class ExplodingRepository:
        def __getattr__(self, name):
            raise AssertionError(f"no debía consultarse: {name}")

    with pytest.raises(VoiceStorageUnavailableError):
        list_received(
            _adult_user(),
            BASE_URL,
            ExplodingRepository(),
            ExplodingRepository(),
            ExplodingRepository(),
            state.settings,
        )


# --- Roles y autenticación ---


@pytest.mark.parametrize("role", [Role.ADMINISTRADOR, None])
@pytest.mark.parametrize("route", ["v1", "v1_without_body", "v2"])
def test_voice_routes_admin_and_role_none_403(state, client, role, route):
    _as(_family_user("admin-uid", role=role))
    state.users.link_adult("admin-uid", ADULT_UID, Role.FAMILIAR)

    if route == "v1":
        response = _post_voice(client)
    elif route == "v1_without_body":
        # El rol se comprueba antes de validar el formulario.
        response = client.post(VOICE_PATH)
    else:
        response = client.get(VOICE_PATH)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _stored_files(state) == []
    assert state.voice_messages.list_received_by_adult(ADULT_UID) == []


def test_v1_requires_bearer_401(state, client):
    response = _post_voice(client)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": BEARER_DETAIL}
    assert _stored_files(state) == []


def test_v2_requires_bearer_401(state, client):
    response = client.get(VOICE_PATH)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": BEARER_DETAIL}


def test_openapi_lists_voice_message_routes():
    paths = app.openapi()["paths"]

    assert set(paths[VOICE_PATH]) == {"get", "post"}
    assert paths[VOICE_PATH]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/VoiceMessageCreateResponse"}
    assert "security" in paths[VOICE_PATH]["post"]
    assert "security" in paths[VOICE_PATH]["get"]
    audio_operation = paths[f"{VOICE_PATH}/{{voice_message_id}}/audio"]["get"]
    assert "security" not in audio_operation
    assert "audio/webm" in audio_operation["responses"]["200"]["content"]


# --- Ronda 3 (D-R3-15, D-R3-23): repositorio SQL y destinatario de la push ---


class RecipientNotLinkedVoiceMessages(InMemoryVoiceMessageRepository):
    """Como el repositorio SQL cuando el contacto no es un usuario de la plataforma."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def add(self, record: VoiceMessageRecord) -> VoiceMessageRecord:
        self.attempts += 1
        raise RecipientNotLinkedError("El contacto no es un usuario de la plataforma.")


def test_v1_recipient_not_linked_403_and_audio_file_deleted(state, client, monkeypatch):
    failing_repository = RecipientNotLinkedVoiceMessages()
    app.dependency_overrides[get_voice_message_repository] = lambda: failing_repository
    deleted: list[tuple[Path, bool]] = []
    original_delete = voice_message_service._delete_quietly

    def spy_delete(path: Path) -> None:
        deleted.append((path, path.exists()))
        original_delete(path)

    monkeypatch.setattr(voice_message_service, "_delete_quietly", spy_delete)
    state.devices.register(ADULT_UID, ADULT_FCM_TOKEN)
    _as(_adult_user())

    response = _post_voice(client)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert failing_repository.attempts == 1
    # El audio llegó a escribirse y se borró al fallar el registro: no queda huérfano.
    assert len(deleted) == 1
    deleted_path, existed = deleted[0]
    assert existed is True
    assert deleted_path.parent == state.storage
    assert not deleted_path.exists()
    assert _stored_files(state) == []
    assert state.push.calls == []


def test_v1_adult_recipient_lookup_database_error_still_201(
    state, client, caplog, monkeypatch
):
    state.users.mark_email_verified(FAMILY_UID, FAMILY_EMAIL)
    state.devices.register(FAMILY_UID, FAMILY_FCM_TOKEN)

    def failing_lookup(email):
        raise OperationalError("x", {}, Exception("x"))

    monkeypatch.setattr(state.users, "find_uid_by_verified_email", failing_lookup)
    _as(_adult_user())

    with caplog.at_level(logging.ERROR, logger=SERVICE_LOGGER):
        response = _post_voice(client)

    assert response.status_code == 201
    record = state.voice_messages.get(response.json()["id"])
    assert record is not None
    assert record.adult_uid == ADULT_UID
    assert record.from_adult is True
    assert (state.storage / record.file_name).read_bytes() == WEBM
    assert state.push.calls == []
    assert "OperationalError" in caplog.text
