"""Tests de app/db/session.py, del guard de conftest y del arranque con base (plan ronda 3, F3).

Sin red: PostgreSQL solo aparece como URL que nunca se conecta; las transacciones se
prueban con sqlite en memoria (StaticPool y los listeners de pysqlite para SAVEPOINT).
"""

import asyncio
import logging
import sqlite3
import threading
import traceback
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app import main as main_module
from app.core.config import get_settings
from app.db import session as db_session
from app.main import app
from app.repositories.contact_repository import (
    _default_contact_repository,
    get_contact_repository,
)


DB_BLOCKED_MESSAGE = "Base de datos real bloqueada en tests"
CHECK_FAILED_MESSAGE = "La base de datos no responde o no tiene el esquema connects_senior"
# URLs sin contraseña y con un dominio reservado: nunca se conectan.
PG_URL = "postgresql://usuario@db.example.invalid/base"
PG_URL_FORMS = [
    "postgres://usuario@db.example.invalid/base",
    "postgresql://usuario@db.example.invalid/base",
    "postgresql+psycopg://usuario@db.example.invalid/base",
]
POOLER_URL = "postgresql://usuario@ep-demo-pooler.example.invalid/base"
# URL de ejemplo del arranque: sin usuario ni clave; nunca se conecta.
LIFESPAN_PG_URL = "postgresql://db.example.invalid/x"
LIFESPAN_PG_HOST = "db.example.invalid"
EXPECTED_POOL_OPTIONS = {
    "pool_pre_ping": True,
    "pool_size": 5,
    "max_overflow": 5,
    "pool_recycle": 300,
    "hide_parameters": True,
    "echo": False,
}
WAIT_TIMEOUT = 5.0


def _savepoint_sqlite_engine(with_schema: bool = False):
    """sqlite en memoria compartida con SAVEPOINT funcional (receta de SQLAlchemy para pysqlite)."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        if with_schema:
            dbapi_connection.execute("ATTACH DATABASE ':memory:' AS connects_senior")

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        connection.exec_driver_sql("BEGIN")

    return engine


@pytest.fixture
def item_engine():
    engine = _savepoint_sqlite_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE item (value INTEGER NOT NULL)")
    try:
        yield engine
    finally:
        engine.dispose()


def _insert(connection, value):
    connection.execute(text("INSERT INTO item (value) VALUES (:value)"), {"value": value})


def _values(connection) -> list[int]:
    return list(connection.execute(text("SELECT value FROM item ORDER BY rowid")).scalars())


# --- is_postgres_url, sql_enabled y build_engine ---


@pytest.mark.parametrize("url", PG_URL_FORMS + ["POSTGRESQL://usuario@db.example.invalid/base"])
def test_is_postgres_url_accepts_postgres_forms(url):
    assert db_session.is_postgres_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///./connects_senior.db",
        "sqlite://",
        "",
        "mysql://usuario@db.example.invalid/base",
        "postgresqlx://usuario@db.example.invalid/base",
        "base postgresql://usuario@db.example.invalid/base",
    ],
)
def test_is_postgres_url_rejects_other_urls(url):
    assert db_session.is_postgres_url(url) is False


def test_sql_enabled_reads_settings_on_every_call(monkeypatch):
    current = SimpleNamespace(database_url=PG_URL)
    monkeypatch.setattr(db_session, "get_settings", lambda: current)

    assert db_session.sql_enabled() is True
    current.database_url = "sqlite:///./connects_senior.db"
    assert db_session.sql_enabled() is False


@pytest.mark.parametrize("url", PG_URL_FORMS)
def test_build_engine_normalizes_driver_without_connecting(url):
    engine = db_session.build_engine(url)
    try:
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.host == "db.example.invalid"
        assert engine.hide_parameters is True
    finally:
        engine.dispose()


def test_build_engine_postgres_options(monkeypatch):
    calls = []

    def fake_create_engine(url, **kwargs):
        calls.append((url, kwargs))
        return "motor"

    monkeypatch.setattr(db_session, "create_engine", fake_create_engine)

    assert db_session.build_engine(PG_URL) == "motor"
    (url, kwargs), = calls
    assert url.drivername == "postgresql+psycopg"
    assert kwargs == {**EXPECTED_POOL_OPTIONS, "connect_args": {"connect_timeout": 10}}


def test_build_engine_pooler_host_disables_prepared_statements(monkeypatch):
    calls = []
    monkeypatch.setattr(
        db_session, "create_engine", lambda url, **kwargs: calls.append(kwargs) or "motor"
    )

    db_session.build_engine(POOLER_URL)

    assert calls[0]["connect_args"] == {"connect_timeout": 10, "prepare_threshold": None}


def test_build_engine_sqlite_is_plain(monkeypatch):
    calls = []
    real_create_engine = db_session.create_engine

    def spy_create_engine(url, **kwargs):
        calls.append(kwargs)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(db_session, "create_engine", spy_create_engine)

    engine = db_session.build_engine("sqlite://")
    try:
        assert calls == [{}]
        assert engine.url.drivername == "sqlite"
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


# --- Database ---


def test_database_needs_engine_or_connection():
    with pytest.raises(ValueError):
        db_session.Database()


def test_database_engine_property(item_engine):
    assert db_session.Database.from_engine(item_engine).engine is item_engine
    with item_engine.connect() as connection:
        assert db_session.Database.for_connection(connection).engine is None


def test_from_engine_commits(item_engine):
    database = db_session.Database.from_engine(item_engine)

    with database.transaction() as connection:
        _insert(connection, 7)

    with item_engine.connect() as connection:
        assert _values(connection) == [7]


def test_from_engine_rolls_back_on_error(item_engine):
    database = db_session.Database.from_engine(item_engine)

    with pytest.raises(ValueError):
        with database.transaction() as connection:
            _insert(connection, 7)
            raise ValueError("fallo")

    with item_engine.connect() as connection:
        assert _values(connection) == []


def test_for_connection_outer_rollback_reverts_everything(item_engine):
    with item_engine.connect() as connection:
        outer = connection.begin()
        database = db_session.Database.for_connection(connection)

        with database.transaction() as tx:
            _insert(tx, 1)
        with database.transaction() as tx:
            _insert(tx, 2)
        assert _values(connection) == [1, 2]

        outer.rollback()
        assert _values(connection) == []


def test_for_connection_failure_only_reverts_its_savepoint(item_engine):
    with item_engine.connect() as connection:
        outer = connection.begin()
        database = db_session.Database.for_connection(connection)

        with database.transaction() as tx:
            _insert(tx, 1)
        with pytest.raises(ValueError):
            with database.transaction() as tx:
                _insert(tx, 2)
                raise ValueError("fallo")
        with pytest.raises(IntegrityError):
            with database.transaction() as tx:
                _insert(tx, None)

        assert outer.is_active
        with database.transaction() as tx:
            _insert(tx, 3)
        assert _values(connection) == [1, 3]
        outer.rollback()


# --- check_database ---


def test_check_database_without_schema_raises_clean_error(caplog):
    engine = create_engine("sqlite://")
    try:
        with caplog.at_level(logging.ERROR, logger="app.db.session"):
            with pytest.raises(RuntimeError) as error_info:
                db_session.check_database(db_session.Database.from_engine(engine))
    finally:
        engine.dispose()

    error = error_info.value
    assert str(error) == CHECK_FAILED_MESSAGE
    assert "sqlite" not in str(error).lower()
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert caplog.messages == [
        "La base de datos configurada no responde o no tiene el esquema "
        "connects_senior (OperationalError)"
    ]


def test_check_database_with_schema_passes():
    engine = _savepoint_sqlite_engine(with_schema=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE connects_senior.usuario (id_usuario INTEGER PRIMARY KEY)"
            )
        db_session.check_database(db_session.Database.from_engine(engine))
    finally:
        engine.dispose()


# --- Guard de conftest y get_database ---


def test_guard_blocks_real_database():
    with pytest.raises(RuntimeError, match=DB_BLOCKED_MESSAGE):
        db_session.get_database()


def test_guard_keeps_memory_repositories():
    assert get_settings().database_url.startswith("sqlite")
    assert db_session.sql_enabled() is False
    assert get_contact_repository() is _default_contact_repository


def test_real_get_database_builds_from_settings_and_caches(_db_en_memoria, monkeypatch):
    real_get_database = _db_en_memoria
    monkeypatch.setattr(
        db_session, "get_settings", lambda: SimpleNamespace(database_url="sqlite://")
    )

    database = real_get_database()
    try:
        assert isinstance(database, db_session.Database)
        assert database.engine.url.drivername == "sqlite"
        assert real_get_database() is database
    finally:
        database.engine.dispose()


# --- lazy_sql_repository ---


def test_lazy_sql_repository_creates_once(monkeypatch):
    database = object()
    created = []

    def factory(db):
        created.append(db)
        return SimpleNamespace(db=db)

    monkeypatch.setattr(db_session, "get_database", lambda: database)
    provider = db_session.lazy_sql_repository(factory)
    assert created == []

    results = []
    threads = [threading.Thread(target=lambda: results.append(provider())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT_TIMEOUT)

    assert len(results) == 8
    assert all(result is results[0] for result in results)
    assert created == [database]
    assert provider() is results[0]


def test_lazy_sql_repository_is_blocked_by_guard_and_retries(monkeypatch):
    provider = db_session.lazy_sql_repository(lambda db: SimpleNamespace(db=db))

    with pytest.raises(RuntimeError, match=DB_BLOCKED_MESSAGE):
        provider()

    database = object()
    monkeypatch.setattr(db_session, "get_database", lambda: database)
    assert provider().db is database


# --- Arranque (lifespan, D-R3-16) ---


@pytest.fixture
def scheduler_events(monkeypatch):
    """Sustituye scheduler.scheduler_loop: "call" al llamarlo, "start" y "cancelled" al correr."""
    events: list[str] = []

    async def _fake_loop():
        events.append("start")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    def fake_scheduler_loop(interval_seconds=30.0, stop_event=None):
        events.append("call")
        return _fake_loop()

    monkeypatch.setattr(main_module.scheduler, "scheduler_loop", fake_scheduler_loop)
    return events


def _postgres_settings(monkeypatch):
    monkeypatch.setattr(
        main_module, "get_settings", lambda: SimpleNamespace(database_url=LIFESPAN_PG_URL)
    )


def _spy_dispose(monkeypatch, engine) -> list[int]:
    """Cuenta las llamadas a engine.dispose sin dejar de liberar el motor."""
    disposed: list[int] = []
    real_dispose = engine.dispose

    def spy(*args, **kwargs):
        disposed.append(1)
        return real_dispose(*args, **kwargs)

    monkeypatch.setattr(engine, "dispose", spy)
    return disposed


def _unreachable_sqlite_engine():
    """sqlite en memoria cuya conexión falla como libpq: el texto del error trae la URL."""
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "do_connect")
    def _refuse(dialect, connection_record, cargs, cparams):
        raise sqlite3.OperationalError(f"could not connect to server at {LIFESPAN_PG_URL}")

    return engine


def test_lifespan_memory_does_not_open_database(scheduler_events, caplog):
    with caplog.at_level(logging.INFO, logger="app.main"):
        with TestClient(app) as client:
            assert client.get("/").status_code == 200

    assert "Persistencia: memoria" in caplog.messages
    assert "Persistencia: PostgreSQL" not in caplog.messages


def test_lifespan_reads_database_url_at_runtime(monkeypatch, scheduler_events, caplog):
    # La variable de módulo `settings` de main.py no se usa para decidir la persistencia.
    monkeypatch.setattr(main_module, "settings", SimpleNamespace(database_url=PG_URL))

    with caplog.at_level(logging.INFO, logger="app.main"):
        with TestClient(app):
            pass

    assert "Persistencia: memoria" in caplog.messages


def test_lifespan_postgres_checks_database_and_disposes(
    monkeypatch, scheduler_events, caplog
):
    _postgres_settings(monkeypatch)
    engine = create_engine("sqlite://")
    database = db_session.Database.from_engine(engine)
    disposed = _spy_dispose(monkeypatch, engine)
    checked = []
    monkeypatch.setattr(db_session, "get_database", lambda: database)
    monkeypatch.setattr(db_session, "check_database", lambda db: checked.append(db))

    with caplog.at_level(logging.INFO):
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            assert checked == [database]
            assert disposed == []
            assert scheduler_events.count("call") == 1

    assert disposed == [1]
    assert "Persistencia: PostgreSQL" in caplog.messages
    assert "Persistencia: memoria" not in caplog.messages
    assert LIFESPAN_PG_HOST not in caplog.text


def test_lifespan_postgres_failure_aborts_startup(monkeypatch, scheduler_events, caplog):
    # get_database devuelve una Database de sqlite (si no, el guard de conftest lanzaría
    # antes de llegar a check_database); check_database, parcheado con un espía sobre el
    # real, lanza porque la conexión falla con un error de driver que trae la URL.
    _postgres_settings(monkeypatch)
    engine = _unreachable_sqlite_engine()
    database = db_session.Database.from_engine(engine)
    disposed = _spy_dispose(monkeypatch, engine)
    real_check_database = db_session.check_database
    checked = []

    def failing_check(db):
        checked.append(db)
        real_check_database(db)

    monkeypatch.setattr(db_session, "get_database", lambda: database)
    monkeypatch.setattr(db_session, "check_database", failing_check)

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError) as error_info:
            with TestClient(app):
                pass

    error = error_info.value
    assert checked == [database]
    assert str(error) == CHECK_FAILED_MESSAGE
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert LIFESPAN_PG_HOST not in "".join(traceback.format_exception(error))
    assert scheduler_events == []
    assert disposed == [1]
    assert (
        "La base de datos configurada no responde o no tiene el esquema "
        "connects_senior (OperationalError)"
    ) in caplog.messages
    assert "Persistencia: PostgreSQL" not in caplog.messages
    assert "Persistencia: memoria" not in caplog.messages
    assert LIFESPAN_PG_HOST not in caplog.text


def test_lifespan_with_real_check_on_database_without_schema(
    monkeypatch, scheduler_events, caplog
):
    _postgres_settings(monkeypatch)
    engine = create_engine("sqlite://")
    disposed = _spy_dispose(monkeypatch, engine)
    monkeypatch.setattr(
        db_session, "get_database", lambda: db_session.Database.from_engine(engine)
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError) as error_info:
            with TestClient(app):
                pass

    assert str(error_info.value) == CHECK_FAILED_MESSAGE
    assert scheduler_events == []
    assert disposed == [1]
    assert LIFESPAN_PG_HOST not in caplog.text
