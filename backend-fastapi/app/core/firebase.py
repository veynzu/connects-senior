from threading import Lock

import firebase_admin
from firebase_admin import credentials

from app.core.config import get_settings


FIREBASE_APP_NAME = "connects-senior-backend"
_initialization_lock = Lock()


class FirebaseConfigurationError(RuntimeError):
    pass


def get_firebase_app() -> firebase_admin.App:
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
                    "FIREBASE_CREDENTIALS_PATH no está configurado."
                )
            if not credentials_file.is_file():
                raise FirebaseConfigurationError(
                    f"No se encontró el archivo de credenciales de Firebase: {credentials_file}"
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
