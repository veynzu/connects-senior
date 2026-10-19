"""Cliente de Daily.co para videollamadas (ADR-06).

Crea salas privadas con expiración y meeting tokens por participante contra la
API REST de Daily.co. La clave de la API se usa solo aquí: nunca aparece en
repr, mensajes de excepción ni logs.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings


DAILY_API_URL = "https://api.daily.co/v1"
CALL_ROOM_TTL_SECONDS = 3600

_REQUEST_TIMEOUT_SECONDS = 10.0
_NOT_CONFIGURED_MESSAGE = "La integración con Daily.co no está configurada."
_UNAVAILABLE_MESSAGE = "Daily.co no está disponible temporalmente."
_INVALID_RESPONSE_MESSAGE = "Daily.co devolvió una respuesta inválida."
_UNKNOWN_ERROR_CODE = "sin código"


class DailyClientError(Exception):
    pass


class DailyNotConfiguredError(DailyClientError):
    pass


class DailyUnavailableError(DailyClientError):
    pass


@dataclass(frozen=True)
class DailyRoom:
    name: str
    url: str
    expires_at: datetime


class DailyClient:
    """Encapsula las llamadas REST a Daily.co.

    No guarda un httpx.AsyncClient propio: la instancia vive en lru_cache y un
    cliente reutilizado entre event loops falla. Si no se inyecta `client`, cada
    método crea uno y lo cierra al terminar; el inyectado nunca se cierra.
    """

    def __init__(
        self,
        api_key: str,
        domain: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._domain = domain
        self._client = client

    def __repr__(self) -> str:
        return f"{type(self).__name__}(domain={self._domain!r})"

    async def create_room(
        self,
        *,
        audio_only: bool,
        now: datetime | None = None,
    ) -> DailyRoom:
        api_key, domain = self._require_configuration()

        current_time = now if now is not None else datetime.now(timezone.utc)
        exp = int(current_time.timestamp()) + CALL_ROOM_TTL_SECONDS
        payload = {
            "name": f"cs-{uuid4().hex}",
            "privacy": "private",
            "properties": {
                "exp": exp,
                "eject_at_room_exp": True,
                "max_participants": 2,
                "enable_chat": False,
                "enable_knocking": False,
                "enable_prejoin_ui": False,
                "enable_screenshare": False,
                "start_video_off": audio_only,
                "permissions": _send_permissions(audio_only),
            },
        }

        response_data = await self._post("/rooms", payload, api_key)

        name = response_data.get("name")
        if not isinstance(name, str) or not name:
            raise DailyUnavailableError(_INVALID_RESPONSE_MESSAGE)

        url = response_data.get("url")
        if not isinstance(url, str) or not url.strip():
            url = f"https://{_normalize_domain(domain)}/{name}"

        return DailyRoom(
            name=name,
            url=url,
            expires_at=datetime.fromtimestamp(exp, timezone.utc),
        )

    async def create_meeting_token(
        self,
        *,
        room_name: str,
        user_id: str,
        user_name: str,
        audio_only: bool,
        expires_at: datetime,
    ) -> str:
        api_key, _ = self._require_configuration()

        payload = {
            "properties": {
                "room_name": room_name,
                "exp": int(expires_at.timestamp()),
                "user_id": user_id,
                "user_name": user_name,
                "is_owner": False,
                "start_video_off": audio_only,
                "enable_screenshare": False,
                "permissions": _send_permissions(audio_only),
            }
        }

        response_data = await self._post("/meeting-tokens", payload, api_key)

        token = response_data.get("token")
        if not isinstance(token, str) or not token:
            raise DailyUnavailableError(_INVALID_RESPONSE_MESSAGE)
        return token

    def _require_configuration(self) -> tuple[str, str]:
        api_key = self._api_key.strip()
        domain = self._domain.strip()
        if not api_key or not domain:
            raise DailyNotConfiguredError(_NOT_CONFIGURED_MESSAGE)
        return api_key, domain

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        api_key: str,
    ) -> dict[str, Any]:
        request_client = (
            self._client
            if self._client is not None
            else httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
        )
        should_close_client = self._client is None

        try:
            try:
                response = await request_client.post(
                    f"{DAILY_API_URL}{path}",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.RequestError as error:
                raise DailyUnavailableError(_UNAVAILABLE_MESSAGE) from error
        finally:
            if should_close_client:
                await request_client.aclose()

        if response.status_code >= 400:
            raise DailyUnavailableError(
                f"Daily.co respondió con estado {response.status_code} "
                f"({_error_code(response)})."
            )

        try:
            response_data = response.json()
        except ValueError as error:
            raise DailyUnavailableError(_INVALID_RESPONSE_MESSAGE) from error

        if not isinstance(response_data, dict):
            raise DailyUnavailableError(_INVALID_RESPONSE_MESSAGE)
        return response_data


@lru_cache
def get_daily_client() -> DailyClient:
    """Proveedor para Depends. Nunca lanza: la configuración se comprueba al usarlo."""
    settings = get_settings()
    return DailyClient(api_key=settings.daily_api_key, domain=settings.daily_domain)


def _send_permissions(audio_only: bool) -> dict[str, list[str]]:
    return {"canSend": ["audio"] if audio_only else ["audio", "video"]}


def _normalize_domain(domain: str) -> str:
    """Acepta "acme", "acme.daily.co" o "https://acme.daily.co/" y devuelve el host."""
    host = domain.strip()
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
            break
    host = host.rstrip("/")
    if "." not in host:
        host = f"{host}.daily.co"
    return host


def _error_code(response: httpx.Response) -> str:
    # Solo el campo "error" (código corto de Daily); nunca "info" ni el cuerpo completo.
    try:
        response_data = response.json()
    except ValueError:
        return _UNKNOWN_ERROR_CODE

    error = response_data.get("error") if isinstance(response_data, dict) else None
    return error if isinstance(error, str) else _UNKNOWN_ERROR_CODE
