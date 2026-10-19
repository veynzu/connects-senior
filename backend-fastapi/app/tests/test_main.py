import asyncio
import logging
import threading

import pytest
import sqlalchemy.exc
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
from app.core.security import get_current_user
from app.integrations import fcm_client
from app.main import app
from app.models.auth import AuthenticatedUser
from app.models.enums import Role
from app.repositories.contact_repository import get_contact_repository


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


# --- Ronda 3, F3: errores de la base de datos (D-R3-20) y rutas sin cambios ---

DATABASE_UNAVAILABLE_DETAIL = "El servicio no está disponible. Intenta más tarde."
DATABASE_DATA_ERROR_DETAIL = "Datos inválidos: cuerpo: no es válido"
DATABASE_INTERNAL_ERROR_DETAIL = "Error interno. Intenta más tarde."
CONTACTS_PATH = "/api/contacts"
EXPECTED_PATHS = [
    "/",
    "/api/admin/users",
    "/api/admin/users/{uid}",
    "/api/alerts",
    "/api/alerts/{alert_id}",
    "/api/alerts/{alert_id}/acknowledge",
    "/api/auth/adults",
    "/api/auth/adults/{adult_id}/caregivers",
    "/api/auth/devices",
    "/api/auth/login",
    "/api/auth/me",
    "/api/auth/password-reset",
    "/api/auth/refresh",
    "/api/auth/register",
    "/api/calls",
    "/api/calls/voice-messages",
    "/api/calls/voice-messages/{voice_message_id}/audio",
    "/api/calls/{call_id}",
    "/api/contacts",
    "/api/contacts/{contact_id}",
    "/api/messages",
    "/api/reminders",
    "/api/reminders/{reminder_id}",
    "/api/reminders/{reminder_id}/confirm",
    "/api/reminders/{reminder_id}/snooze",
]


@pytest.fixture
def adult_with_failing_contacts():
    """Adulto mayor autenticado cuyo repositorio de contactos lanza el error indicado."""

    def install(error: Exception) -> None:
        def failing_contact_repository():
            raise error

        app.dependency_overrides[get_contact_repository] = failing_contact_repository

    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        uid="adulto-1", role=Role.ADULTO_MAYOR
    )
    try:
        yield install
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "error",
    [
        sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("x")),
        sqlalchemy.exc.InterfaceError("SELECT 1", {}, Exception("x")),
        sqlalchemy.exc.TimeoutError("SELECT 1"),
    ],
    ids=["operational", "interface", "pool-timeout"],
)
def test_database_unavailable_returns_503_text(adult_with_failing_contacts, caplog, error):
    adult_with_failing_contacts(error)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = TestClient(app).get(CONTACTS_PATH)

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_DETAIL}
    assert type(error).__name__ in caplog.text
    assert "SELECT 1" not in caplog.text


def test_database_data_error_returns_422_text(adult_with_failing_contacts, caplog):
    adult_with_failing_contacts(
        sqlalchemy.exc.DataError("INSERT INTO dato VALUES (1)", {}, Exception("x"))
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = TestClient(app).get(CONTACTS_PATH)

    assert response.status_code == 422
    assert response.json() == {"detail": DATABASE_DATA_ERROR_DETAIL}
    assert "DataError" in caplog.text
    assert "INSERT" not in caplog.text


def test_database_integrity_error_returns_500_text(adult_with_failing_contacts, caplog):
    adult_with_failing_contacts(
        sqlalchemy.exc.IntegrityError("INSERT", {}, Exception("x"))
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = TestClient(app).get(CONTACTS_PATH)

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_INTERNAL_ERROR_DETAIL}
    assert caplog.messages == [
        "La base de datos falló de forma inesperada (IntegrityError)"
    ]
    assert "x" not in caplog.text
    assert "INSERT" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        sqlalchemy.exc.ProgrammingError("SELECT 1", {}, Exception("x")),
        sqlalchemy.exc.NoResultFound(),
        sqlalchemy.exc.SQLAlchemyError("SELECT 1"),
    ],
    ids=["programming", "no-result", "base"],
)
def test_other_sqlalchemy_errors_return_500_text(adult_with_failing_contacts, caplog, error):
    adult_with_failing_contacts(error)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = TestClient(app).get(CONTACTS_PATH)

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_INTERNAL_ERROR_DETAIL}
    assert type(error).__name__ in caplog.text
    assert "SELECT 1" not in caplog.text


class _FakeDiag:
    constraint_name = "uq_adulto_contacto_orden_favorito"


class _FakePostgresError(Exception):
    """Causa del driver con diag.constraint_name, como psycopg.errors.UniqueViolation."""

    diag = _FakeDiag()


@pytest.mark.parametrize(
    "error_class, status_code, detail",
    [
        (sqlalchemy.exc.OperationalError, 503, DATABASE_UNAVAILABLE_DETAIL),
        (sqlalchemy.exc.DataError, 422, DATABASE_DATA_ERROR_DETAIL),
        (sqlalchemy.exc.IntegrityError, 500, DATABASE_INTERNAL_ERROR_DETAIL),
    ],
    ids=["503", "422", "500"],
)
def test_database_handlers_log_only_type_and_constraint(
    adult_with_failing_contacts, caplog, error_class, status_code, detail
):
    # Cada handler responde lo suyo (Starlette elige por el MRO de la excepción) y el log
    # nunca lleva la sentencia, los parámetros ni el DETAIL de Postgres.
    orig = _FakePostgresError(
        "duplicate key value; DETAIL: Key (correo)=(ana@example.invalid) already exists."
    )
    adult_with_failing_contacts(
        error_class(
            "INSERT INTO connects_senior.usuario (correo) VALUES (%(correo)s)",
            {"correo": "ana@example.invalid"},
            orig,
        )
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = TestClient(app).get(CONTACTS_PATH)

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert (
        f"({error_class.__name__}, restricción uq_adulto_contacto_orden_favorito)"
        in caplog.text
    )
    assert "ana@example.invalid" not in caplog.text
    assert "INSERT" not in caplog.text
    assert "duplicate" not in caplog.text


def test_database_error_handlers_are_registered_by_class():
    handlers = app.exception_handlers

    for error_class in (
        sqlalchemy.exc.OperationalError,
        sqlalchemy.exc.InterfaceError,
        sqlalchemy.exc.TimeoutError,
    ):
        assert handlers[error_class] is main_module.database_unavailable_handler
    assert handlers[sqlalchemy.exc.DataError] is main_module.database_data_error_handler
    assert (
        handlers[sqlalchemy.exc.SQLAlchemyError]
        is main_module.database_internal_error_handler
    )


def test_openapi_still_has_the_25_contract_paths():
    assert sorted(app.openapi()["paths"]) == EXPECTED_PATHS
