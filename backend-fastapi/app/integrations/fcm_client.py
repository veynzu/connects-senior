"""Cliente de Firebase Cloud Messaging para notificaciones push (ADR-07).

Envía una notificación a una lista de tokens FCM con el Admin SDK
(firebase_admin.messaging) sobre la misma cuenta de servicio que Firebase Auth.
Las funciones son SÍNCRONAS y hacen red: desde código async se invocan con
run_in_threadpool (regla 4.3). Nunca se registran tokens, el contenido de la
notificación ni mensajes crudos de Firebase: solo tipos de excepción y conteos.
"""
import logging
import threading
import warnings
from dataclasses import dataclass, field

from firebase_admin import exceptions as firebase_exceptions
from firebase_admin import messaging

from app.core.firebase import FirebaseConfigurationError, get_firebase_app


MAX_TOKENS_PER_MULTICAST = 500
FCM_UNAVAILABLE_MESSAGE = "El servicio de notificaciones no está disponible."
_MISSING_EXCEPTION_NAME = "sin excepción"

logger = logging.getLogger(__name__)

# warnings.catch_warnings modifica el estado global de warnings: el lock evita que
# dos hilos del threadpool que construyen mensajes a la vez se pisen el filtro.
_warnings_lock = threading.Lock()


@dataclass(frozen=True)
class FcmResult:
    """Resultado de un envío.

    success_count: envíos aceptados por FCM. failure_count: envíos rechazados por
    un motivo distinto de token no registrado. invalid_tokens: tokens que FCM
    reporta como no registrados (UnregisteredError); el llamante los borra.
    """

    success_count: int
    failure_count: int
    invalid_tokens: list[str] = field(default_factory=list)


class FcmError(Exception):
    pass


class FcmUnavailableError(FcmError):
    pass


def _unique(tokens: list[str]) -> list[str]:
    return list(dict.fromkeys(tokens))


def _build_message(
    batch: list[str], title: str, body: str, data: dict[str, str]
) -> messaging.MulticastMessage:
    # MulticastMessage(tokens=...) emite DeprecationWarning en firebase-admin 7.7.0
    # (recomienda fids), pero A10 recibe el token FCM del navegador (contrato).
    with _warnings_lock, warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return messaging.MulticastMessage(
            tokens=batch,
            notification=messaging.Notification(title=title, body=body),
            data=data,
        )


def send_to_tokens(
    tokens: list[str], title: str, body: str, data: dict[str, str]
) -> FcmResult:
    """Envía la notificación a cada token, en lotes de MAX_TOKENS_PER_MULTICAST.

    Sin tokens devuelve FcmResult(0, 0, []) sin tocar Firebase. Lanza
    FcmUnavailableError si falta la configuración de Firebase o si Firebase
    rechaza la petición completa.
    """
    unique_tokens = _unique(tokens)
    if not unique_tokens:
        return FcmResult(success_count=0, failure_count=0, invalid_tokens=[])

    success_count = 0
    failure_count = 0
    invalid_tokens: list[str] = []
    try:
        firebase_app = get_firebase_app()
        for start in range(0, len(unique_tokens), MAX_TOKENS_PER_MULTICAST):
            batch = unique_tokens[start : start + MAX_TOKENS_PER_MULTICAST]
            message = _build_message(batch, title, body, dict(data))
            batch_response = messaging.send_each_for_multicast(
                message, app=firebase_app
            )
            for token, response in zip(batch, batch_response.responses):
                if response.success:
                    success_count += 1
                    continue
                exception = response.exception
                if isinstance(exception, messaging.UnregisteredError):
                    invalid_tokens.append(token)
                    continue
                failure_count += 1
                exception_name = (
                    type(exception).__name__
                    if exception is not None
                    else _MISSING_EXCEPTION_NAME
                )
                logger.warning("FCM no entregó una notificación (%s)", exception_name)
    except (
        FirebaseConfigurationError,
        firebase_exceptions.FirebaseError,
        ValueError,
    ) as error:
        # Solo el tipo: el mensaje de Firebase puede incluir datos del envío.
        logger.error("FCM no disponible (%s)", type(error).__name__)
        raise FcmUnavailableError(FCM_UNAVAILABLE_MESSAGE) from error

    if invalid_tokens:
        logger.info("FCM reportó %d token(s) no registrado(s)", len(invalid_tokens))
    return FcmResult(
        success_count=success_count,
        failure_count=failure_count,
        invalid_tokens=invalid_tokens,
    )
