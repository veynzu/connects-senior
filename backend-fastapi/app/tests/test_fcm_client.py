import logging
import warnings

import pytest
from firebase_admin import exceptions as firebase_exceptions
from firebase_admin import messaging

from app.core.firebase import FirebaseConfigurationError
from app.integrations import fcm_client
from app.integrations.fcm_client import (
    FCM_UNAVAILABLE_MESSAGE,
    MAX_TOKENS_PER_MULTICAST,
    FcmError,
    FcmResult,
    FcmUnavailableError,
    send_to_tokens,
)


FIREBASE_APP = object()
TITLE = "Pedir ayuda"
BODY = "Rosa necesita ayuda"
DATA = {"type": "alert", "alert_id": "alert-1"}
SECRET_TOKEN = "token-secreto-abc123"
FCM_LOGGER = "app.integrations.fcm_client"


def _ok(index: int = 0) -> messaging.SendResponse:
    return messaging.SendResponse({"name": f"projects/demo/messages/{index}"}, None)


def _failed(exception: Exception | None) -> messaging.SendResponse:
    return messaging.SendResponse(None, exception)


class FakeSendEach:
    """Sustituto de messaging.send_each_for_multicast: registra cada llamada, sin red."""

    def __init__(self, responder=None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.responder = responder
        self.error = error

    def __call__(self, multicast_message, dry_run=False, app=None):
        self.calls.append(
            {"message": multicast_message, "dry_run": dry_run, "app": app}
        )
        if self.error is not None:
            raise self.error
        if self.responder is not None:
            return messaging.BatchResponse(self.responder(multicast_message))
        return messaging.BatchResponse(
            [_ok(index) for index, _ in enumerate(multicast_message.tokens)]
        )


@pytest.fixture
def fake_send(monkeypatch) -> FakeSendEach:
    fake = FakeSendEach()
    monkeypatch.setattr(fcm_client.messaging, "send_each_for_multicast", fake)
    monkeypatch.setattr(fcm_client, "get_firebase_app", lambda: FIREBASE_APP)
    return fake


def test_send_to_tokens_empty_does_not_call_firebase(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("send_to_tokens tocó Firebase sin tokens.")

    monkeypatch.setattr(fcm_client, "get_firebase_app", unexpected)
    monkeypatch.setattr(fcm_client.messaging, "send_each_for_multicast", unexpected)

    result = send_to_tokens([], TITLE, BODY, DATA)

    assert result == FcmResult(success_count=0, failure_count=0, invalid_tokens=[])


def test_send_to_tokens_success_counts_all(fake_send):
    result = send_to_tokens(["token-a", "token-b"], TITLE, BODY, DATA)

    assert result == FcmResult(success_count=2, failure_count=0, invalid_tokens=[])
    assert len(fake_send.calls) == 1
    assert fake_send.calls[0]["app"] is FIREBASE_APP
    assert fake_send.calls[0]["dry_run"] is False
    assert fake_send.calls[0]["message"].tokens == ["token-a", "token-b"]


def test_send_to_tokens_sends_exact_notification_and_data(fake_send):
    send_to_tokens(["token-a"], TITLE, BODY, DATA)

    message = fake_send.calls[0]["message"]
    assert isinstance(message, messaging.MulticastMessage)
    assert isinstance(message.notification, messaging.Notification)
    assert message.notification.title == TITLE
    assert message.notification.body == BODY
    assert message.data == DATA
    assert message.data is not DATA


def test_send_to_tokens_removes_duplicates_keeping_order(fake_send):
    result = send_to_tokens(["token-b", "token-a", "token-b"], TITLE, BODY, DATA)

    assert fake_send.calls[0]["message"].tokens == ["token-b", "token-a"]
    assert result.success_count == 2


def test_send_to_tokens_collects_unregistered_tokens(fake_send):
    fake_send.responder = lambda message: [
        _ok(0),
        _failed(messaging.UnregisteredError("Requested entity was not found.")),
    ]

    result = send_to_tokens(["token-ok", "token-gone"], TITLE, BODY, DATA)

    assert result == FcmResult(
        success_count=1, failure_count=0, invalid_tokens=["token-gone"]
    )


def test_send_to_tokens_other_error_counts_failure_without_token_in_logs(
    fake_send, caplog
):
    caplog.set_level(logging.DEBUG, logger=FCM_LOGGER)
    fake_send.responder = lambda message: [
        _failed(messaging.QuotaExceededError(f"cuota agotada para {SECRET_TOKEN}")),
        _ok(1),
    ]

    result = send_to_tokens([SECRET_TOKEN, "token-ok"], TITLE, BODY, DATA)

    assert result == FcmResult(success_count=1, failure_count=1, invalid_tokens=[])
    assert "QuotaExceededError" in caplog.text
    assert SECRET_TOKEN not in caplog.text
    assert "cuota agotada" not in caplog.text
    assert BODY not in caplog.text


def test_send_to_tokens_response_without_exception_counts_failure(fake_send, caplog):
    caplog.set_level(logging.DEBUG, logger=FCM_LOGGER)
    fake_send.responder = lambda message: [_failed(None)]

    result = send_to_tokens([SECRET_TOKEN], TITLE, BODY, DATA)

    assert result == FcmResult(success_count=0, failure_count=1, invalid_tokens=[])
    assert SECRET_TOKEN not in caplog.text


def test_send_to_tokens_batches_of_500(fake_send):
    tokens = [f"token-{index}" for index in range(MAX_TOKENS_PER_MULTICAST + 1)]

    result = send_to_tokens(tokens, TITLE, BODY, DATA)

    assert MAX_TOKENS_PER_MULTICAST == 500
    assert len(fake_send.calls) == 2
    assert fake_send.calls[0]["message"].tokens == tokens[:500]
    assert fake_send.calls[1]["message"].tokens == tokens[500:]
    assert result.success_count == 501
    assert result.failure_count == 0


def test_send_to_tokens_maps_unregistered_tokens_in_second_batch(fake_send):
    tokens = [f"token-{index}" for index in range(MAX_TOKENS_PER_MULTICAST + 2)]

    def responder(message):
        responses = [_ok(index) for index, _ in enumerate(message.tokens)]
        if message.tokens[0] == "token-500":
            responses[1] = _failed(messaging.UnregisteredError("gone"))
        return responses

    fake_send.responder = responder

    result = send_to_tokens(tokens, TITLE, BODY, DATA)

    assert result == FcmResult(
        success_count=501, failure_count=0, invalid_tokens=["token-501"]
    )


def test_send_to_tokens_firebase_error_raises_unavailable(fake_send, caplog):
    caplog.set_level(logging.DEBUG, logger=FCM_LOGGER)
    fake_send.error = firebase_exceptions.UnavailableError(
        f"fallo con {SECRET_TOKEN}"
    )

    with pytest.raises(FcmUnavailableError) as exc_info:
        send_to_tokens([SECRET_TOKEN], TITLE, BODY, DATA)

    assert str(exc_info.value) == FCM_UNAVAILABLE_MESSAGE
    assert isinstance(exc_info.value, FcmError)
    assert "UnavailableError" in caplog.text
    assert SECRET_TOKEN not in caplog.text


def test_send_to_tokens_configuration_error_raises_unavailable(monkeypatch):
    def missing_credentials():
        raise FirebaseConfigurationError(
            "La ruta de credenciales de Firebase no está configurada."
        )

    def unexpected(*args, **kwargs):
        pytest.fail("send_to_tokens envió sin configuración de Firebase.")

    monkeypatch.setattr(fcm_client, "get_firebase_app", missing_credentials)
    monkeypatch.setattr(fcm_client.messaging, "send_each_for_multicast", unexpected)

    with pytest.raises(FcmUnavailableError) as exc_info:
        send_to_tokens(["token-a"], TITLE, BODY, DATA)

    assert str(exc_info.value) == FCM_UNAVAILABLE_MESSAGE


def test_send_to_tokens_value_error_raises_unavailable(fake_send):
    fake_send.error = ValueError("Message.data must not contain non-string values.")

    with pytest.raises(FcmUnavailableError) as exc_info:
        send_to_tokens(["token-a"], TITLE, BODY, DATA)

    assert str(exc_info.value) == FCM_UNAVAILABLE_MESSAGE


def test_send_to_tokens_emits_no_deprecation_warning(fake_send):
    # Control: construir MulticastMessage(tokens=...) sí avisa en firebase-admin 7.7.0.
    with warnings.catch_warnings(record=True) as control:
        warnings.simplefilter("always")
        messaging.MulticastMessage(tokens=["token-a"])
    assert any(issubclass(item.category, DeprecationWarning) for item in control)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        send_to_tokens(["token-a"], TITLE, BODY, DATA)

    assert not [item for item in caught if issubclass(item.category, DeprecationWarning)]
    assert len(fake_send.calls) == 1


def test_fcm_unavailable_error_is_fcm_error():
    assert issubclass(FcmUnavailableError, FcmError)
    assert issubclass(FcmError, Exception)
