import asyncio
import logging
import threading

import pytest
from fastapi.testclient import TestClient
from firebase_admin import messaging

from app import main as main_module
from app.api import (
    admin_router,
    alerts_router,
    auth_router,
    calls_router,
    contacts_router,
    messages_router,
    reminders_router,
)
from app.core import scheduler
from app.integrations import fcm_client
from app.main import app


LOGIN_PATH = "/api/auth/login"


def test_validation_error_detail_is_spanish_text():
    response = TestClient(app).post(LOGIN_PATH, json={})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Datos inválidos: email: es obligatorio; password: es obligatorio"
    }


def test_malformed_json_detail_is_text():
    response = TestClient(app).post(
        LOGIN_PATH,
        content=b"{bad",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Datos inválidos: cuerpo: no tiene un formato válido"
    }


def test_missing_body_detail_is_text():
    response = TestClient(app).post(LOGIN_PATH)

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: cuerpo: es obligatorio"}


def test_httpx_logger_level_is_warning():
    httpx_logger = logging.getLogger("httpx")

    assert httpx_logger.level == logging.WARNING
    assert not httpx_logger.isEnabledFor(logging.INFO)


def test_root_still_ok():
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "ConnectsSenior API is running"}


# --- Ronda 2, F2: routers registrados, lifespan del planificador y guardia de FCM ---

PREVIOUS_ROUTES = {
    "/": {"get"},
    "/api/auth/login": {"post"},
    "/api/auth/me": {"get"},
    "/api/auth/refresh": {"post"},
    "/api/calls": {"get", "post"},
    "/api/calls/{call_id}": {"patch"},
}
EXPECTED_ROUTERS = {
    "/api/auth": (auth_router.router, ["Auth"]),
    "/api/contacts": (contacts_router.router, ["Contacts"]),
    "/api/calls": (calls_router.router, ["Calls"]),
    "/api/alerts": (alerts_router.router, ["Alerts"]),
    "/api/reminders": (reminders_router.router, ["Reminders"]),
    "/api/messages": (messages_router.router, ["Messages"]),
    "/api/admin": (admin_router.router, ["Admin"]),
}
FCM_BLOCKED_MESSAGE = "FCM real bloqueado en tests"
WAIT_TIMEOUT = 5.0


def _included_routers() -> dict[str, tuple[object, list[str]]]:
    # FastAPI 0.142 conserva cada include_router como un _IncludedRouter con su
    # include_context (prefijo, tags y router original).
    included = {}
    for route in app.routes:
        context = getattr(route, "include_context", None)
        if context is not None:
            included[context.prefix] = (context.included_router, list(context.tags))
    return included


@pytest.fixture
def empty_tick_registry():
    saved = scheduler.registered_ticks()
    for name in saved:
        scheduler.unregister_tick(name)
    try:
        yield
    finally:
        for name in scheduler.registered_ticks():
            scheduler.unregister_tick(name)
        for name, fn in saved.items():
            scheduler.register_tick(name, fn)


def test_openapi_keeps_previous_routes():
    paths = app.openapi()["paths"]

    for path, methods in PREVIOUS_ROUTES.items():
        assert path in paths
        assert methods <= set(paths[path])


def test_main_registers_all_seven_routers_with_prefix_and_tags():
    included = _included_routers()

    assert set(included) == set(EXPECTED_ROUTERS)
    for prefix, (router, tags) in EXPECTED_ROUTERS.items():
        assert included[prefix][0] is router
        assert included[prefix][1] == tags


def test_app_uses_lifespan():
    assert app.router.lifespan_context is not None
    assert main_module.lifespan is not None


def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    started = threading.Event()
    events: list[tuple[str, bool]] = []

    async def fake_scheduler_loop(interval_seconds=30.0, stop_event=None):
        events.append(("start", stop_event is not None and not stop_event.is_set()))
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append(("cancelled", stop_event.is_set()))
            raise

    monkeypatch.setattr(main_module.scheduler, "scheduler_loop", fake_scheduler_loop)

    with TestClient(app) as client:
        assert started.wait(timeout=WAIT_TIMEOUT)
        assert client.get("/").status_code == 200

    assert events == [("start", True), ("cancelled", True)]


def test_testclient_without_context_does_not_start_scheduler(monkeypatch):
    events: list[str] = []

    async def fake_scheduler_loop(interval_seconds=30.0, stop_event=None):
        events.append("start")

    monkeypatch.setattr(main_module.scheduler, "scheduler_loop", fake_scheduler_loop)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert events == []


def test_lifespan_runs_registered_ticks_with_real_loop(monkeypatch, empty_tick_registry):
    real_scheduler_loop = scheduler.scheduler_loop
    ticked = threading.Event()

    def fast_scheduler_loop(stop_event=None):
        return real_scheduler_loop(interval_seconds=0.01, stop_event=stop_event)

    scheduler.register_tick("demo", ticked.set)
    monkeypatch.setattr(main_module.scheduler, "scheduler_loop", fast_scheduler_loop)

    with TestClient(app):
        assert ticked.wait(timeout=WAIT_TIMEOUT)


def test_conftest_guard_blocks_real_fcm_send():
    with pytest.raises(RuntimeError, match=FCM_BLOCKED_MESSAGE):
        messaging.send_each_for_multicast(messaging.MulticastMessage(fids=["fid"]))


def test_conftest_guard_blocks_fcm_client_send(monkeypatch):
    monkeypatch.setattr(fcm_client, "get_firebase_app", lambda: object())

    with pytest.raises(RuntimeError, match=FCM_BLOCKED_MESSAGE):
        fcm_client.send_to_tokens(["token-a"], "Título", "Cuerpo", {"type": "demo"})
