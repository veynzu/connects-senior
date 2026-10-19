import logging

import pytest

from app.integrations import fcm_client
from app.integrations.fcm_client import FcmResult, FcmUnavailableError
from app.models.contacts import Contact, ContactPermissions
from app.models.enums import Role
from app.models.users import UserProfile
from app.repositories.device_repository import InMemoryDeviceRepository
from app.repositories.user_repository import InMemoryUserRepository
from app.services import push_service
from app.services.push_service import (
    linked_uid_for_contact,
    notify_users,
    reachable_uids,
)


PUSH_LOGGER = "app.services.push_service"
TITLE = "Pedir ayuda"
BODY = "Rosa necesita ayuda"
DATA = {"type": "alert", "alert_id": "alert-1"}
UID_A = "uid-a"
UID_B = "uid-b"
UID_C = "uid-c"
TOKEN_A1 = "token-secreto-a1"
TOKEN_A2 = "token-secreto-a2"
TOKEN_B1 = "token-secreto-b1"
SECRET_VALUE = "meeting-token-secreto-xyz"
CONTACT_EMAIL = "Hija@Example.com "


class FakeSendToTokens:
    """Sustituto de fcm_client.send_to_tokens: registra cada llamada, sin red.

    "options" guarda los argumentos con nombre recibidos (click_path, ttl_seconds).
    """

    def __init__(self, result: FcmResult | None = None, error: Exception | None = None):
        self.calls: list[dict] = []
        self.result = result
        self.error = error

    def __call__(self, tokens, title, body, data, **options):
        self.calls.append(
            {
                "tokens": list(tokens),
                "title": title,
                "body": body,
                "data": data,
                "options": options,
            }
        )
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return FcmResult(success_count=len(tokens), failure_count=0, invalid_tokens=[])


@pytest.fixture
def fake_send(monkeypatch) -> FakeSendToTokens:
    fake = FakeSendToTokens()
    monkeypatch.setattr(fcm_client, "send_to_tokens", fake)
    return fake


@pytest.fixture
def devices() -> InMemoryDeviceRepository:
    repository = InMemoryDeviceRepository()
    repository.register(UID_A, TOKEN_A1)
    repository.register(UID_A, TOKEN_A2)
    repository.register(UID_B, TOKEN_B1)
    return repository


def _contact(email: str | None = CONTACT_EMAIL) -> Contact:
    return Contact(
        id="contact-1",
        first_name="Ana",
        last_name="Gómez",
        relationship="hija",
        email=email,
        permissions=ContactPermissions(
            calls=True, video_calls=True, messages=True, help_alerts=True
        ),
    )


# --- notify_users ---


def test_notify_users_without_tokens_does_not_call_fcm(fake_send):
    empty_devices = InMemoryDeviceRepository()

    sent = notify_users([UID_A, UID_B], TITLE, BODY, DATA, empty_devices)

    assert sent == 0
    assert fake_send.calls == []


def test_notify_users_unknown_uids_do_not_call_fcm(fake_send, devices):
    sent = notify_users([UID_C, "otro-uid"], TITLE, BODY, DATA, devices)

    assert sent == 0
    assert fake_send.calls == []


def test_notify_users_empty_uids_do_not_call_fcm(fake_send, devices):
    sent = notify_users([], TITLE, BODY, DATA, devices)

    assert sent == 0
    assert fake_send.calls == []


def test_notify_users_success_returns_success_count(fake_send, devices):
    fake_send.result = FcmResult(success_count=2, failure_count=1, invalid_tokens=[])

    sent = notify_users([UID_A, UID_B], TITLE, BODY, DATA, devices)

    assert sent == 2
    assert len(fake_send.calls) == 1


def test_notify_users_passes_tokens_title_body_and_data_exactly(fake_send, devices):
    sent = notify_users([UID_B, UID_A], TITLE, BODY, DATA, devices)

    assert sent == 3
    call = fake_send.calls[0]
    # Orden de registro de los tokens (InMemoryDeviceRepository.tokens_of).
    assert call["tokens"] == [TOKEN_A1, TOKEN_A2, TOKEN_B1]
    assert call["title"] == TITLE
    assert call["body"] == BODY
    assert call["data"] == DATA
    assert type(call["data"]) is dict
    # Copia: modificar lo enviado no altera el data del llamante.
    assert call["data"] is not DATA


def test_notify_users_only_sends_to_requested_uids(fake_send, devices):
    notify_users([UID_B], TITLE, BODY, DATA, devices)

    assert fake_send.calls[0]["tokens"] == [TOKEN_B1]


def test_notify_users_accepts_generator_of_uids(fake_send, devices):
    sent = notify_users((uid for uid in [UID_A]), TITLE, BODY, DATA, devices)

    assert sent == 2
    assert fake_send.calls[0]["tokens"] == [TOKEN_A1, TOKEN_A2]


def test_notify_users_accepts_empty_data(fake_send, devices):
    sent = notify_users([UID_B], TITLE, BODY, {}, devices)

    assert sent == 1
    assert fake_send.calls[0]["data"] == {}


def test_notify_users_fcm_error_returns_zero_and_logs(fake_send, devices, caplog):
    caplog.set_level(logging.DEBUG, logger=PUSH_LOGGER)
    fake_send.error = FcmUnavailableError(fcm_client.FCM_UNAVAILABLE_MESSAGE)

    sent = notify_users([UID_A, UID_B], TITLE, BODY, DATA, devices)

    assert sent == 0
    assert len(fake_send.calls) == 1
    assert "FcmUnavailableError" in caplog.text
    assert fcm_client.FCM_UNAVAILABLE_MESSAGE in caplog.text
    for token in (TOKEN_A1, TOKEN_A2, TOKEN_B1):
        assert token not in caplog.text
    # Un fallo de FCM no borra tokens.
    assert devices.tokens_of([UID_A, UID_B]) == [TOKEN_A1, TOKEN_A2, TOKEN_B1]


def test_notify_users_unexpected_exception_returns_zero_and_logs(
    fake_send, devices, caplog
):
    caplog.set_level(logging.DEBUG, logger=PUSH_LOGGER)
    fake_send.error = RuntimeError(f"fallo con {TOKEN_A1}")

    sent = notify_users([UID_A], TITLE, BODY, DATA, devices)

    assert sent == 0
    assert "Fallo inesperado al enviar push (RuntimeError)" in caplog.text
    # Solo el tipo: el mensaje de la excepción podría llevar el token.
    assert TOKEN_A1 not in caplog.text


def test_notify_users_rejects_non_string_data(fake_send, devices, caplog):
    caplog.set_level(logging.DEBUG, logger=PUSH_LOGGER)
    data = {"type": "call", "token": SECRET_VALUE, "audio_only": True, "round": 2}

    sent = notify_users([UID_A], TITLE, BODY, data, devices)

    assert sent == 0
    assert fake_send.calls == []
    error_records = [
        record for record in caplog.records if record.levelno == logging.ERROR
    ]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    assert "audio_only" in message
    assert "round" in message
    # Solo los nombres de las claves, nunca los valores.
    assert SECRET_VALUE not in caplog.text


def test_notify_users_rejects_none_data_value(fake_send, devices):
    sent = notify_users([UID_A], TITLE, BODY, {"type": "alert", "alert_id": None}, devices)

    assert sent == 0
    assert fake_send.calls == []


def test_notify_users_rejects_non_string_data_key(fake_send, devices):
    sent = notify_users([UID_A], TITLE, BODY, {1: "uno"}, devices)

    assert sent == 0
    assert fake_send.calls == []


def test_notify_users_removes_invalid_tokens(fake_send, devices):
    fake_send.result = FcmResult(
        success_count=1, failure_count=0, invalid_tokens=[TOKEN_A1, TOKEN_B1]
    )

    sent = notify_users([UID_A, UID_B], TITLE, BODY, DATA, devices)

    assert sent == 1
    assert devices.tokens_of([UID_A, UID_B]) == [TOKEN_A2]
    assert devices.has_device(UID_A) is True
    assert devices.has_device(UID_B) is False


def test_notify_users_does_not_remove_tokens_without_invalid_ones(fake_send, devices):
    notify_users([UID_A, UID_B], TITLE, BODY, DATA, devices)

    assert devices.tokens_of([UID_A, UID_B]) == [TOKEN_A1, TOKEN_A2, TOKEN_B1]


class _BrokenRemoveDevices(InMemoryDeviceRepository):
    def remove_tokens(self, tokens):
        raise RuntimeError("repositorio caído")


def test_notify_users_remove_tokens_failure_keeps_success_count(
    fake_send, caplog
):
    caplog.set_level(logging.DEBUG, logger=PUSH_LOGGER)
    broken_devices = _BrokenRemoveDevices()
    broken_devices.register(UID_A, TOKEN_A1)
    broken_devices.register(UID_A, TOKEN_A2)
    fake_send.result = FcmResult(
        success_count=1, failure_count=0, invalid_tokens=[TOKEN_A2]
    )

    sent = notify_users([UID_A], TITLE, BODY, DATA, broken_devices)

    assert sent == 1
    assert "RuntimeError" in caplog.text
    assert TOKEN_A2 not in caplog.text


class _BrokenLookupDevices(InMemoryDeviceRepository):
    def tokens_of(self, uids):
        raise RuntimeError("repositorio caído")


def test_notify_users_repository_failure_returns_zero(fake_send, caplog):
    caplog.set_level(logging.DEBUG, logger=PUSH_LOGGER)

    sent = notify_users([UID_A], TITLE, BODY, DATA, _BrokenLookupDevices())

    assert sent == 0
    assert fake_send.calls == []
    assert "Fallo inesperado al enviar push (RuntimeError)" in caplog.text


def test_notify_users_with_real_fcm_client_never_raises(monkeypatch, devices, caplog):
    # Recorre el send_to_tokens real; conftest bloquea send_each_for_multicast
    # (RuntimeError) y get_firebase_app se sustituye para no cargar credenciales.
    caplog.set_level(logging.DEBUG, logger=PUSH_LOGGER)
    monkeypatch.setattr(fcm_client, "get_firebase_app", lambda: object())

    sent = notify_users([UID_A], TITLE, BODY, DATA, devices)

    assert sent == 0
    assert "Fallo inesperado al enviar push (RuntimeError)" in caplog.text
    assert TOKEN_A1 not in caplog.text


def test_notify_users_calls_fcm_through_module_attribute(monkeypatch, devices):
    # push_service usa fcm_client.send_to_tokens vía el módulo: el monkeypatch lo alcanza.
    calls = []

    def fake(tokens, title, body, data):
        calls.append(tokens)
        return FcmResult(success_count=7, failure_count=0, invalid_tokens=[])

    monkeypatch.setattr(fcm_client, "send_to_tokens", fake)

    assert push_service.notify_users([UID_B], TITLE, BODY, DATA, devices) == 7
    assert calls == [[TOKEN_B1]]


# --- linked_uid_for_contact ---


def test_linked_uid_for_contact_without_email_returns_none():
    users = InMemoryUserRepository()
    users.mark_email_verified(UID_A, "hija@example.com")

    assert linked_uid_for_contact(_contact(email=None), users) is None


def test_linked_uid_for_contact_blank_email_returns_none():
    users = InMemoryUserRepository()
    users.mark_email_verified(UID_A, "hija@example.com")

    assert linked_uid_for_contact(_contact(email="   "), users) is None
    assert linked_uid_for_contact(_contact(email=""), users) is None


def test_linked_uid_for_contact_without_match_returns_none():
    users = InMemoryUserRepository()
    users.mark_email_verified(UID_A, "otra@example.com")

    assert linked_uid_for_contact(_contact(), users) is None


def test_linked_uid_for_contact_unverified_profile_returns_none():
    # Un perfil con ese email pero sin verificar (A4 no exige verificación) no vincula.
    users = InMemoryUserRepository()
    users.add_profile(
        UserProfile(
            uid=UID_A,
            first_name="Ana",
            last_name="Gómez",
            email="hija@example.com",
            role=Role.FAMILIAR,
        )
    )

    assert linked_uid_for_contact(_contact(), users) is None


def test_linked_uid_for_contact_with_match_returns_uid():
    users = InMemoryUserRepository()
    users.mark_email_verified(UID_B, "otra@example.com")
    users.mark_email_verified(UID_A, "  HIJA@example.COM")

    # El email del contacto lleva mayúsculas y espacios: se compara normalizado.
    assert linked_uid_for_contact(_contact(), users) == UID_A


# --- reachable_uids ---


def test_reachable_uids_returns_only_uids_with_devices(devices):
    assert reachable_uids([UID_C, UID_B, UID_A], devices) == [UID_B, UID_A]


def test_reachable_uids_removes_duplicates_keeping_order(devices):
    assert reachable_uids([UID_A, UID_C, UID_A, UID_B, UID_B], devices) == [
        UID_A,
        UID_B,
    ]


def test_reachable_uids_empty_and_without_devices():
    empty_devices = InMemoryDeviceRepository()

    assert reachable_uids([], empty_devices) == []
    assert reachable_uids([UID_A], empty_devices) == []


def test_reachable_uids_after_token_removed(devices):
    devices.remove_tokens([TOKEN_B1])

    assert reachable_uids([UID_A, UID_B], devices) == [UID_A]


# --- Web push: click_path y ttl_seconds se pasan a fcm_client (D-LL-3) ---


CLICK_PATH = f"/llamadas/entrante/call-1#t={SECRET_VALUE}"


def test_notify_users_without_web_options_calls_fcm_as_before(fake_send, devices):
    notify_users([UID_B], TITLE, BODY, DATA, devices)

    # Sin click_path ni ttl_seconds la llamada a FCM es exactamente la de siempre.
    assert fake_send.calls[0]["options"] == {}


def test_notify_users_passes_click_path_and_ttl_to_fcm(fake_send, devices):
    sent = notify_users(
        [UID_B],
        TITLE,
        BODY,
        DATA,
        devices,
        click_path=CLICK_PATH,
        ttl_seconds=120,
    )

    assert sent == 1
    call = fake_send.calls[0]
    assert call["tokens"] == [TOKEN_B1]
    assert call["data"] == DATA
    assert call["options"] == {"click_path": CLICK_PATH, "ttl_seconds": 120}


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"click_path": CLICK_PATH}, {"click_path": CLICK_PATH}),
        ({"ttl_seconds": 30}, {"ttl_seconds": 30}),
        ({"click_path": None, "ttl_seconds": None}, {}),
    ],
    ids=["click_path_only", "ttl_only", "explicit_none"],
)
def test_notify_users_passes_only_given_web_options(
    fake_send, devices, options, expected
):
    notify_users([UID_B], TITLE, BODY, DATA, devices, **options)

    assert fake_send.calls[0]["options"] == expected


def test_notify_users_click_path_never_logged_on_failure(fake_send, devices, caplog):
    caplog.set_level(logging.DEBUG, logger=PUSH_LOGGER)
    fake_send.error = RuntimeError(f"fallo con {CLICK_PATH}")

    sent = notify_users(
        [UID_A], TITLE, BODY, DATA, devices, click_path=CLICK_PATH, ttl_seconds=120
    )

    assert sent == 0
    assert CLICK_PATH not in caplog.text
    assert SECRET_VALUE not in caplog.text
