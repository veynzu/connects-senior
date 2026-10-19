"""Servicio de notificaciones push y vínculo contacto -> usuario (D-R2-3, D-R2-5, ADR-07).

Los servicios de negocio (alertas, recordatorios, mensajes, llamadas) llaman a
notify_users; este módulo resuelve los tokens FCM de cada uid y llama a
fcm_client.send_to_tokens a través del módulo (`from app.integrations import
fcm_client`), de modo que los tests pueden hacer
`monkeypatch.setattr(fcm_client, "send_to_tokens", fake)`.

notify_users es SÍNCRONA y hace red: desde código async se invoca con
`await run_in_threadpool(push_service.notify_users, ...)` (regla 4.3). Nunca lanza:
una push que falla nunca hace fallar la acción principal. Nunca se registran tokens
ni valores de data ni click_path (pueden llevar un meeting token, D-R2-16): solo
nombres de claves, tipos de excepción y conteos.

click_path y ttl_seconds (D-LL-3) se pasan a fcm_client.send_to_tokens solo cuando
vienen informados: sin ellos la llamada es exactamente la de siempre.
"""
import logging
from collections.abc import Iterable, Mapping

from app.integrations import fcm_client
from app.models.contacts import Contact
from app.repositories.device_repository import DeviceRepository
from app.repositories.user_repository import UserRepository


logger = logging.getLogger(__name__)


def _invalid_data_keys(data: Mapping[str, str]) -> list[str]:
    """Nombres de las claves cuyo nombre o valor no es str (FCM solo acepta texto)."""
    return [
        str(key)
        for key, value in data.items()
        if not isinstance(key, str) or not isinstance(value, str)
    ]


def _send(
    uids: Iterable[str],
    title: str,
    body: str,
    data: Mapping[str, str],
    devices: DeviceRepository,
    click_path: str | None,
    ttl_seconds: int | None,
) -> int:
    invalid_keys = _invalid_data_keys(data)
    if invalid_keys:
        # Solo el nombre de la clave: el valor puede llevar datos sensibles.
        logger.error(
            "La push no se envió: data tiene valores que no son texto en las claves %s",
            ", ".join(invalid_keys),
        )
        return 0

    tokens = devices.tokens_of(uids)
    if not tokens:
        return 0

    web_options: dict[str, str | int] = {}
    if click_path is not None:
        web_options["click_path"] = click_path
    if ttl_seconds is not None:
        web_options["ttl_seconds"] = ttl_seconds

    try:
        result = fcm_client.send_to_tokens(
            tokens, title, body, dict(data), **web_options
        )
    except fcm_client.FcmError as error:
        # El mensaje de FcmError es un texto fijo propio, sin datos del envío.
        logger.error("No se pudo enviar la push (%s): %s", type(error).__name__, error)
        return 0

    if result.invalid_tokens:
        try:
            devices.remove_tokens(result.invalid_tokens)
        except Exception as error:
            # Los envíos correctos ya salieron: se informa el conteo igualmente.
            logger.error(
                "No se pudieron borrar los tokens no registrados (%s)",
                type(error).__name__,
            )
    return result.success_count


def notify_users(
    uids: Iterable[str],
    title: str,
    body: str,
    data: Mapping[str, str],
    devices: DeviceRepository,
    *,
    click_path: str | None = None,
    ttl_seconds: int | None = None,
) -> int:
    """Envía una push a todos los dispositivos registrados de esos uids.

    click_path (ruta de la app que abre la notificación al tocarla) y ttl_seconds
    son opcionales y se pasan tal cual a fcm_client.send_to_tokens (D-LL-3).
    Devuelve el número de envíos aceptados por FCM; 0 si ningún uid tiene
    dispositivo, si algún valor de data no es str (no llama a FCM), si FCM no está
    disponible o ante cualquier fallo inesperado. Borra del repositorio los tokens
    que FCM reporta como no registrados. Nunca lanza.
    """
    try:
        return _send(uids, title, body, data, devices, click_path, ttl_seconds)
    except Exception as error:
        logger.error("Fallo inesperado al enviar push (%s)", type(error).__name__)
        return 0


def linked_uid_for_contact(contact: Contact, users: UserRepository) -> str | None:
    """uid de la persona de la plataforma que corresponde al contacto (P9, D-R2-3).

    Coincidencia por email normalizado solo contra correos verificados (los marca
    A10 con un id_token con email_verified = true). Sin email o sin coincidencia:
    None.
    """
    if contact.email is None or not contact.email.strip():
        return None
    return users.find_uid_by_verified_email(contact.email)


def reachable_uids(uids: Iterable[str], devices: DeviceRepository) -> list[str]:
    """Los uids que tienen al menos un dispositivo registrado (A10).

    Conserva el orden de entrada y quita duplicados.
    """
    return [uid for uid in dict.fromkeys(uids) if devices.has_device(uid)]
