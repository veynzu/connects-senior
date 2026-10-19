import pytest


FCM_BLOCKED_MESSAGE = "FCM real bloqueado en tests"


def _blocked_send_each_for_multicast(*args, **kwargs):
    raise RuntimeError(FCM_BLOCKED_MESSAGE)


@pytest.fixture(autouse=True)
def _sin_emulador(monkeypatch):
    monkeypatch.delenv("FIREBASE_AUTH_EMULATOR_HOST", raising=False)


@pytest.fixture(autouse=True)
def _block_real_fcm(monkeypatch):
    # Ninguna push real puede salir de la suite, aunque exista la cuenta de servicio.
    # Los tests de fcm_client vuelven a parchear send_each_for_multicast con su fake.
    monkeypatch.setattr(
        "app.integrations.fcm_client.messaging.send_each_for_multicast",
        _blocked_send_each_for_multicast,
    )
