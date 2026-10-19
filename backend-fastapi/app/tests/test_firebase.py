import os
from types import SimpleNamespace

import pytest

from app.core import firebase
from app.core.config import Settings


EMULATOR_MESSAGE = (
    "El emulador de Firebase Auth está presente en el entorno "
    "pero no está habilitado en la configuración."
)
EMULATOR_ENVIRON = {firebase.EMULATOR_ENV_VAR: "127.0.0.1:9099"}


def _guard_settings(*, use_emulator: bool, environment: str) -> SimpleNamespace:
    return SimpleNamespace(firebase_use_emulator=use_emulator, environment=environment)


def _app_settings(credentials_file) -> SimpleNamespace:
    return SimpleNamespace(
        firebase_use_emulator=False,
        environment="development",
        firebase_credentials_file=credentials_file,
        firebase_project_id="",
    )


def test_emulator_guard_rejects_without_opt_in():
    settings = _guard_settings(use_emulator=False, environment="development")

    with pytest.raises(firebase.FirebaseConfigurationError) as exc_info:
        firebase.ensure_emulator_not_leaking(settings, EMULATOR_ENVIRON)

    assert str(exc_info.value) == EMULATOR_MESSAGE
    assert isinstance(exc_info.value, RuntimeError)


def test_emulator_guard_allows_opt_in_in_development():
    settings = _guard_settings(use_emulator=True, environment="development")

    firebase.ensure_emulator_not_leaking(settings, EMULATOR_ENVIRON)


def test_emulator_guard_rejects_opt_in_outside_development():
    settings = _guard_settings(use_emulator=True, environment="production")

    with pytest.raises(firebase.FirebaseConfigurationError) as exc_info:
        firebase.ensure_emulator_not_leaking(settings, EMULATOR_ENVIRON)

    assert str(exc_info.value) == EMULATOR_MESSAGE


def test_emulator_guard_ignores_absent_or_empty_variable():
    settings = _guard_settings(use_emulator=False, environment="production")

    firebase.ensure_emulator_not_leaking(settings, {})
    firebase.ensure_emulator_not_leaking(settings, {firebase.EMULATOR_ENV_VAR: ""})


def test_get_firebase_app_runs_emulator_guard_first(monkeypatch):
    class GuardError(Exception):
        pass

    def failing_guard(*args, **kwargs):
        raise GuardError("guard")

    def unexpected_call(*args, **kwargs):
        pytest.fail("get_firebase_app avanzó antes de ejecutar el guard del emulador.")

    monkeypatch.setattr(firebase, "ensure_emulator_not_leaking", failing_guard)
    monkeypatch.setattr(firebase, "get_settings", unexpected_call)
    monkeypatch.setattr(firebase.firebase_admin, "get_app", unexpected_call)

    with pytest.raises(GuardError):
        firebase.get_firebase_app()


def test_get_firebase_app_missing_path_message_has_no_variable_name(monkeypatch):
    monkeypatch.setattr(firebase, "get_settings", lambda: _app_settings(None))

    with pytest.raises(firebase.FirebaseConfigurationError) as exc_info:
        firebase.get_firebase_app()

    message = str(exc_info.value)
    assert message == "La ruta de credenciales de Firebase no está configurada."
    assert "FIREBASE_CREDENTIALS_PATH" not in message


def test_get_firebase_app_missing_file_message_has_no_path(monkeypatch, tmp_path):
    missing_file = tmp_path / "no.json"
    monkeypatch.setattr(firebase, "get_settings", lambda: _app_settings(missing_file))

    with pytest.raises(firebase.FirebaseConfigurationError) as exc_info:
        firebase.get_firebase_app()

    message = str(exc_info.value)
    assert message == "No se encontró el archivo de credenciales de Firebase."
    assert str(tmp_path) not in message


def test_settings_disable_emulator_by_default(monkeypatch):
    monkeypatch.delenv("FIREBASE_USE_EMULATOR", raising=False)

    assert Settings(_env_file=None).firebase_use_emulator is False


def test_emulator_variable_absent_during_tests():
    assert firebase.EMULATOR_ENV_VAR not in os.environ
