import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.integrations import daily_client
from app.integrations.daily_client import (
    DailyClient,
    DailyNotConfiguredError,
    DailyRoom,
    DailyUnavailableError,
)


NOW = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
EXPECTED_EXP = int(NOW.timestamp()) + 3600
API_KEY = "test-key"
DOMAIN = "acme.daily.co"
# Host distinto del dominio configurado: prueba que room_url sale de la respuesta.
RESPONSE_URL_BASE = "https://connects.daily.co"
ROOMS_ROUTE = ("POST", "/v1/rooms")
TOKENS_ROUTE = ("POST", "/v1/meeting-tokens")
ROOM_NAME_PATTERN = re.compile(r"^cs-[0-9a-f]{32}$")

NOT_CONFIGURED_MESSAGE = "La integración con Daily.co no está configurada."
UNAVAILABLE_MESSAGE = "Daily.co no está disponible temporalmente."
INVALID_RESPONSE_MESSAGE = "Daily.co devolvió una respuesta inválida."

Handler = Callable[[httpx.Request], httpx.Response]


def _daily_transport(
    routes: dict[tuple[str, str], Handler],
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        if route not in routes:
            raise AssertionError(f"Petición inesperada a {route}")
        if seen is not None:
            seen.append(request)
        return routes[route](request)

    return httpx.MockTransport(handler)


def _room_handler(*, include_url: bool = True) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        response_data = {
            "id": "5e3cf703-5547-47d6-a371-37b1f0b4427f",
            "name": body["name"],
            "api_created": True,
            "privacy": "private",
            "created_at": "2026-10-17T15:00:00.000Z",
            "config": body["properties"],
        }
        if include_url:
            response_data["url"] = f"{RESPONSE_URL_BASE}/{body['name']}"
        return httpx.Response(200, json=response_data)

    return handler


def _token_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"token": "meeting-token"})


def _static_handler(status_code: int, **response_kwargs) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, **response_kwargs)

    return handler


def _raising_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("conexión rechazada", request=request)


async def _create_room(
    routes: dict[tuple[str, str], Handler],
    *,
    audio_only: bool = False,
    domain: str = DOMAIN,
    seen: list[httpx.Request] | None = None,
) -> DailyRoom:
    async with httpx.AsyncClient(transport=_daily_transport(routes, seen)) as client:
        daily = DailyClient(api_key=API_KEY, domain=domain, client=client)
        return await daily.create_room(audio_only=audio_only, now=NOW)


async def _create_meeting_token(
    routes: dict[tuple[str, str], Handler],
    *,
    audio_only: bool = False,
    seen: list[httpx.Request] | None = None,
) -> str:
    async with httpx.AsyncClient(transport=_daily_transport(routes, seen)) as client:
        daily = DailyClient(api_key=API_KEY, domain=DOMAIN, client=client)
        return await daily.create_meeting_token(
            room_name="cs-test",
            user_id="firebase-uid",
            user_name="Ana Pérez",
            audio_only=audio_only,
            expires_at=datetime.fromtimestamp(EXPECTED_EXP, timezone.utc),
        )


@pytest.fixture
def clear_daily_client_cache():
    daily_client.get_daily_client.cache_clear()
    yield
    daily_client.get_daily_client.cache_clear()


# --- create_room ---


@pytest.mark.asyncio
async def test_create_room_sends_private_room_with_expiration():
    seen: list[httpx.Request] = []

    room = await _create_room({ROOMS_ROUTE: _room_handler()}, seen=seen)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.daily.co/v1/rooms"
    assert request.headers["Authorization"] == "Bearer test-key"

    body = json.loads(request.content)
    name = body["name"]
    assert ROOM_NAME_PATTERN.fullmatch(name)
    assert body == {
        "name": name,
        "privacy": "private",
        "properties": {
            "exp": int(NOW.timestamp()) + 3600,
            "eject_at_room_exp": True,
            "max_participants": 2,
            "enable_chat": False,
            "enable_knocking": False,
            "enable_prejoin_ui": False,
            "enable_screenshare": False,
            "start_video_off": False,
            "permissions": {"canSend": ["audio", "video"]},
        },
    }

    assert room == DailyRoom(
        name=name,
        url=f"{RESPONSE_URL_BASE}/{name}",
        expires_at=datetime.fromtimestamp(EXPECTED_EXP, timezone.utc),
    )
    assert room.expires_at.tzinfo is timezone.utc


@pytest.mark.asyncio
async def test_create_room_audio_only_blocks_video():
    seen: list[httpx.Request] = []

    await _create_room({ROOMS_ROUTE: _room_handler()}, audio_only=True, seen=seen)

    properties = json.loads(seen[0].content)["properties"]
    assert properties["start_video_off"] is True
    assert properties["permissions"] == {"canSend": ["audio"]}
    assert properties["enable_screenshare"] is False
    assert properties["exp"] == EXPECTED_EXP


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["acme", "acme.daily.co", "https://acme.daily.co/"])
async def test_create_room_builds_url_from_domain_when_missing(domain):
    room = await _create_room(
        {ROOMS_ROUTE: _room_handler(include_url=False)},
        domain=domain,
    )

    assert ROOM_NAME_PATTERN.fullmatch(room.name)
    assert room.url == f"https://acme.daily.co/{room.name}"


# --- create_meeting_token ---


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_only", [True, False])
async def test_create_meeting_token_sends_restricted_token(audio_only):
    seen: list[httpx.Request] = []
    expires_at = datetime.fromtimestamp(EXPECTED_EXP, timezone.utc)

    token = await _create_meeting_token(
        {TOKENS_ROUTE: _token_handler},
        audio_only=audio_only,
        seen=seen,
    )

    assert token == "meeting-token"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.daily.co/v1/meeting-tokens"
    assert request.headers["Authorization"] == "Bearer test-key"

    body = json.loads(request.content)
    assert body == {
        "properties": {
            "room_name": "cs-test",
            "exp": int(expires_at.timestamp()),
            "user_id": "firebase-uid",
            "user_name": "Ana Pérez",
            "is_owner": False,
            "start_video_off": audio_only,
            "enable_screenshare": False,
            "permissions": {"canSend": ["audio"] if audio_only else ["audio", "video"]},
        }
    }
    assert "eject_at_token_exp" not in body["properties"]


# --- Errores ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "response_kwargs", "error_code"),
    [
        (
            400,
            {"json": {"error": "invalid-request-error", "info": "detalle interno"}},
            "invalid-request-error",
        ),
        (
            401,
            {"json": {"error": "authentication-error", "info": "detalle interno"}},
            "authentication-error",
        ),
        (429, {"json": {"error": {"detalle": "interno"}}}, "sin código"),
        (500, {"content": b"detalle interno"}, "sin código"),
    ],
    ids=["400", "401", "429", "500"],
)
async def test_daily_http_error_maps_to_unavailable(
    status_code, response_kwargs, error_code
):
    expected_message = f"Daily.co respondió con estado {status_code} ({error_code})."
    handler = _static_handler(status_code, **response_kwargs)

    with pytest.raises(DailyUnavailableError) as room_error:
        await _create_room({ROOMS_ROUTE: handler})
    with pytest.raises(DailyUnavailableError) as token_error:
        await _create_meeting_token({TOKENS_ROUTE: handler})

    assert str(room_error.value) == expected_message
    assert str(token_error.value) == expected_message


@pytest.mark.asyncio
async def test_daily_connect_error_maps_to_unavailable():
    with pytest.raises(DailyUnavailableError) as room_error:
        await _create_room({ROOMS_ROUTE: _raising_handler})
    with pytest.raises(DailyUnavailableError) as token_error:
        await _create_meeting_token({TOKENS_ROUTE: _raising_handler})

    assert str(room_error.value) == UNAVAILABLE_MESSAGE
    assert str(token_error.value) == UNAVAILABLE_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "response_kwargs"),
    [
        (ROOMS_ROUTE, {"json": {"url": f"{RESPONSE_URL_BASE}/cs-test"}}),
        (TOKENS_ROUTE, {"json": {}}),
        (TOKENS_ROUTE, {"json": {"token": ""}}),
        (ROOMS_ROUTE, {"content": b"<html>no es JSON</html>"}),
        (TOKENS_ROUTE, {"content": b"<html>no es JSON</html>"}),
    ],
    ids=[
        "sala-sin-name",
        "token-sin-token",
        "token-vacio",
        "sala-no-json",
        "token-no-json",
    ],
)
async def test_daily_invalid_response_maps_to_unavailable(route, response_kwargs):
    routes = {route: _static_handler(200, **response_kwargs)}

    with pytest.raises(DailyUnavailableError) as error_info:
        if route == ROOMS_ROUTE:
            await _create_room(routes)
        else:
            await _create_meeting_token(routes)

    assert str(error_info.value) == INVALID_RESPONSE_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "domain"),
    [("", DOMAIN), (API_KEY, ""), ("   ", DOMAIN), (API_KEY, "   ")],
    ids=["api-key-vacia", "dominio-vacio", "api-key-espacios", "dominio-espacios"],
)
async def test_client_without_configuration_raises_before_request(api_key, domain):
    seen: list[httpx.Request] = []

    async with httpx.AsyncClient(transport=_daily_transport({}, seen)) as client:
        daily = DailyClient(api_key=api_key, domain=domain, client=client)

        with pytest.raises(DailyNotConfiguredError) as room_error:
            await daily.create_room(audio_only=False, now=NOW)
        with pytest.raises(DailyNotConfiguredError) as token_error:
            await daily.create_meeting_token(
                room_name="cs-test",
                user_id="firebase-uid",
                user_name="Ana Pérez",
                audio_only=False,
                expires_at=datetime.fromtimestamp(EXPECTED_EXP, timezone.utc),
            )

    assert seen == []
    assert str(room_error.value) == NOT_CONFIGURED_MESSAGE
    assert str(token_error.value) == NOT_CONFIGURED_MESSAGE


# --- Proveedor y ciclo de vida del cliente HTTP ---


@pytest.mark.asyncio
async def test_get_daily_client_uses_settings_lazily(
    monkeypatch, clear_daily_client_cache
):
    settings_calls: list[int] = []

    def fake_settings() -> SimpleNamespace:
        settings_calls.append(1)
        return SimpleNamespace(daily_api_key="", daily_domain="")

    def unexpected_client(**kwargs):
        raise AssertionError("No debe crearse un cliente HTTP sin configuración.")

    monkeypatch.setattr(daily_client, "get_settings", fake_settings)
    monkeypatch.setattr(daily_client.httpx, "AsyncClient", unexpected_client)

    client = daily_client.get_daily_client()

    assert isinstance(client, DailyClient)
    assert daily_client.get_daily_client() is client
    assert len(settings_calls) == 1
    with pytest.raises(DailyNotConfiguredError):
        await client.create_room(audio_only=False, now=NOW)


@pytest.mark.asyncio
async def test_create_room_closes_own_client(monkeypatch):
    real_async_client = httpx.AsyncClient
    created: list[httpx.AsyncClient] = []
    seen: list[httpx.Request] = []
    responses = iter([_room_handler(), _static_handler(500, json={})])

    def rooms_handler(request: httpx.Request) -> httpx.Response:
        return next(responses)(request)

    def factory(**kwargs) -> httpx.AsyncClient:
        client = real_async_client(
            transport=_daily_transport({ROOMS_ROUTE: rooms_handler}, seen),
            **kwargs,
        )
        created.append(client)
        return client

    monkeypatch.setattr(daily_client.httpx, "AsyncClient", factory)
    daily = DailyClient(api_key=API_KEY, domain=DOMAIN)

    room = await daily.create_room(audio_only=False, now=NOW)

    assert ROOM_NAME_PATTERN.fullmatch(room.name)
    assert len(created) == 1
    assert created[0].is_closed is True
    assert created[0].timeout == httpx.Timeout(10.0)

    with pytest.raises(DailyUnavailableError):
        await daily.create_room(audio_only=False, now=NOW)

    assert len(created) == 2
    assert created[1].is_closed is True
    assert len(seen) == 2

    async with real_async_client(
        transport=_daily_transport({ROOMS_ROUTE: _room_handler()})
    ) as injected_client:
        injected = DailyClient(api_key=API_KEY, domain=DOMAIN, client=injected_client)
        await injected.create_room(audio_only=False, now=NOW)

        assert injected_client.is_closed is False
    assert len(created) == 2


@pytest.mark.asyncio
async def test_daily_errors_never_expose_api_key(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx")
    leaky_body = {"error": "authentication-error", "info": f"clave {API_KEY} inválida"}
    errors: list[Exception] = []

    scenarios = [
        {ROOMS_ROUTE: _static_handler(401, json=leaky_body)},
        {ROOMS_ROUTE: _raising_handler},
        {ROOMS_ROUTE: _static_handler(200, content=b"no es JSON")},
    ]
    for routes in scenarios:
        with pytest.raises(DailyUnavailableError) as error_info:
            await _create_room(routes)
        errors.append(error_info.value)

    with pytest.raises(DailyUnavailableError) as token_error:
        await _create_meeting_token({TOKENS_ROUTE: _static_handler(401, json=leaky_body)})
    errors.append(token_error.value)

    unconfigured = DailyClient(api_key=API_KEY, domain="")
    with pytest.raises(DailyNotConfiguredError) as config_error:
        await unconfigured.create_room(audio_only=False, now=NOW)
    errors.append(config_error.value)

    assert len(errors) == 5
    for error in errors:
        assert API_KEY not in str(error)
        assert API_KEY not in repr(error)
    assert API_KEY not in repr(unconfigured)
    assert API_KEY not in repr(DailyClient(api_key=API_KEY, domain=DOMAIN))
    assert API_KEY not in caplog.text


# --- Settings ---


def test_settings_daily_fields_default_empty(monkeypatch):
    monkeypatch.delenv("DAILY_API_KEY", raising=False)
    monkeypatch.delenv("DAILY_DOMAIN", raising=False)

    settings = Settings(_env_file=None)

    assert settings.daily_api_key == ""
    assert settings.daily_domain == ""
