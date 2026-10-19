import os
from collections.abc import Mapping
from threading import Lock

import firebase_admin
from firebase_admin import credentials

from app.core.config import Settings, get_settings


FIREBASE_APP_NAME = "connects-senior-backend"
EMULATOR_ENV_VAR = "FIREBASE_AUTH_EMULATOR_HOST"
EMULATOR_NOT_ALLOWED_MESSAGE = (
    "El emulador de Firebase Auth está presente en el entorno "
    "pero no está habilitado en la configuración."
)
_initialization_lock = Lock()


class FirebaseConfigurationError(RuntimeError):
    pass


def ensure_emulator_not_leaking(
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Falla si el emulador de Firebase Auth está en el entorno sin opt-in explícito.

    Con el emulador activo, verify_id_token acepta tokens sin firma, y firebase_admin
    lee la variable desde os.environ, no desde Settings. Solo se permite con
    firebase_use_emulator activado y environment == "development".
    """
    environ = os.environ if environ is None else environ
    if not environ.get(EMULATOR_ENV_VAR):
        return

    settings = get_settings() if settings is None else settings
    if not (settings.firebase_use_emulator and settings.environment == "development"):
        raise FirebaseConfigurationError(EMULATOR_NOT_ALLOWED_MESSAGE)


def get_firebase_app() -> firebase_admin.App:
    ensure_emulator_not_leaking()

    try:
        return firebase_admin.get_app(FIREBASE_APP_NAME)
    except ValueError:
        pass

    with _initialization_lock:
        try:
            return firebase_admin.get_app(FIREBASE_APP_NAME)
        except ValueError:
            settings = get_settings()
            credentials_file = settings.firebase_credentials_file

            if credentials_file is None:
                raise FirebaseConfigurationError(
                    "La ruta de credenciales de Firebase no está configurada."
                )
            if not credentials_file.is_file():
                raise FirebaseConfigurationError(
                    "No se encontró el archivo de credenciales de Firebase."
                )

            try:
                firebase_credentials = credentials.Certificate(str(credentials_file))
                options = (
                    {"projectId": settings.firebase_project_id}
                    if settings.firebase_project_id
                    else None
                )
                return firebase_admin.initialize_app(
                    firebase_credentials,
                    options=options,
                    name=FIREBASE_APP_NAME,
                )
            except (ValueError, OSError) as error:
                raise FirebaseConfigurationError(
                    "El archivo de credenciales de Firebase no es válido."
                ) from error
