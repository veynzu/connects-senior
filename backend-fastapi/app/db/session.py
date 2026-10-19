"""Motor, transacciones y selección de persistencia (plan ronda 3, D-R3-1 a D-R3-3, D-R3-16).

Patrón obligatorio en cada módulo de repositorio (D-R3-2): importar este módulo como
``from app.db import session as db_session`` y llamar siempre ``db_session.get_database()``
y ``db_session.sql_enabled()`` por atributo, porque el guard de los tests sustituye
``db_session.get_database``. Nunca ``from app.db.session import get_database``.

Ninguna función de este módulo registra ni devuelve la URL de la base: contiene la clave.
"""

import logging
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import TypeVar

import sqlalchemy.exc
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.config import get_settings
from app.db.mapping import T_USUARIO


logger = logging.getLogger(__name__)

T = TypeVar("T")

# Errores que significan "la base no responde" (D-R3-20): main.py los traduce a 503.
DATABASE_UNAVAILABLE_ERRORS = (OperationalError, InterfaceError, sqlalchemy.exc.TimeoutError)

_POSTGRES_URL = re.compile(r"^(postgres|postgresql)(\+[a-z0-9_]+)?://", re.IGNORECASE)
_PSYCOPG_DRIVER = "postgresql+psycopg"
_POOLER_HOST_MARK = "-pooler"
_CONNECT_TIMEOUT_SECONDS = 10


def is_postgres_url(url: str) -> bool:
    """True para postgres://, postgresql:// y postgresql+<driver>://; False para el resto."""
    if not isinstance(url, str):
        return False
    return _POSTGRES_URL.match(url.strip()) is not None


def build_engine(url: str) -> Engine:
    """Crea el motor sin conectar (D-R3-1).

    Con PostgreSQL normaliza el driver a psycopg 3 (el único instalado), activa el pool con
    pre-ping y reciclado, oculta los parámetros en los errores (tokens FCM, correos, textos)
    y, si el host es el pooler de Neon ("-pooler"), desactiva las sentencias preparadas.
    Con cualquier otra URL (sqlite en tests) usa create_engine sin opciones.
    """
    if not is_postgres_url(url):
        return create_engine(url)

    normalized = make_url(url.strip()).set(drivername=_PSYCOPG_DRIVER)
    connect_args: dict[str, object] = {"connect_timeout": _CONNECT_TIMEOUT_SECONDS}
    if _POOLER_HOST_MARK in (normalized.host or ""):
        connect_args["prepare_threshold"] = None
    return create_engine(
        normalized,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=300,
        hide_parameters=True,
        echo=False,
        connect_args=connect_args,
    )


class Database:
    """Punto de acceso a la base para los repositorios SQL (D-R3-3).

    ``transaction()`` entrega una Connection dentro de una transacción: con
    ``from_engine`` es ``engine.begin()`` (commit al salir, rollback ante excepción); con
    ``for_connection`` (tests) es un SAVEPOINT sobre una conexión cuya transacción exterior
    revierte el test. Nunca se llama a commit() explícito.
    """

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        connection: Connection | None = None,
    ) -> None:
        if (engine is None) == (connection is None):
            raise ValueError("Database necesita un motor o una conexión, no ambos.")
        self._engine = engine
        self._connection = connection

    @classmethod
    def from_engine(cls, engine: Engine) -> "Database":
        """Una transacción nueva del pool por operación."""
        return cls(engine=engine)

    @classmethod
    def for_connection(cls, connection: Connection) -> "Database":
        """Cada operación en un SAVEPOINT de una conexión ya abierta (tests de integración)."""
        return cls(connection=connection)

    @property
    def engine(self) -> Engine | None:
        """Motor propio; None cuando la Database envuelve una conexión de test."""
        return self._engine

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        if self._connection is not None:
            with self._connection.begin_nested():
                yield self._connection
            return
        assert self._engine is not None
        with self._engine.begin() as connection:
            yield connection


@lru_cache
def get_database() -> Database:
    """Database del proceso, construida con la DATABASE_URL vigente (sin conectar)."""
    return Database.from_engine(build_engine(get_settings().database_url))


def sql_enabled() -> bool:
    """True si DATABASE_URL apunta a PostgreSQL; se evalúa en cada llamada (D-R3-2)."""
    return is_postgres_url(get_settings().database_url)


def lazy_sql_repository(factory: Callable[[Database], T]) -> Callable[[], T]:
    """Proveedor que crea el repositorio SQL una sola vez, al primer uso (D-R3-2).

    Llama a ``get_database()`` por el atributo del módulo, así el guard de los tests lo
    bloquea. Si la creación falla no guarda nada y el siguiente uso lo vuelve a intentar.
    """
    lock = threading.Lock()
    holder: list[T] = []

    def provider() -> T:
        if holder:
            return holder[0]
        with lock:
            if not holder:
                holder.append(factory(get_database()))
            return holder[0]

    return provider


def check_database(db: Database) -> None:
    """Comprueba en el arranque que la base responde y tiene el esquema (D-R3-16).

    Ejecuta ``SELECT 1`` y ``SELECT 1 FROM connects_senior.usuario LIMIT 0``. Ante cualquier
    fallo registra solo el tipo de la excepción y lanza RuntimeError sin la causa (la causa
    de libpq puede traer el host o el usuario).
    """
    try:
        with db.transaction() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(text(f"SELECT 1 FROM {T_USUARIO} LIMIT 0"))
    except Exception as exc:
        logger.error(
            "La base de datos configurada no responde o no tiene el esquema connects_senior (%s)",
            type(exc).__name__,
        )
        raise RuntimeError(
            "La base de datos no responde o no tiene el esquema connects_senior"
        ) from None
