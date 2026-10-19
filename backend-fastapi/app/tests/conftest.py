import os
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import dotenv_values
from sqlalchemy import text

from app.core.config import BACKEND_DIR, get_settings
from app.db import session as db_session
from app.db.mapping import T_ADULTO_CONTACTO, T_CONTACTO, T_USUARIO


FCM_BLOCKED_MESSAGE = "FCM real bloqueado en tests"
DB_BLOCKED_MESSAGE = "Base de datos real bloqueada en tests"
SQL_TESTS_FLAG = "CS_SQL_TESTS"
SQL_TESTS_URL_OVERRIDE = "CS_TEST_DATABASE_URL"
TEST_DATABASE_URL = "sqlite:///./ignorado-en-tests.db"


def _blocked_send_each_for_multicast(*args, **kwargs):
    raise RuntimeError(FCM_BLOCKED_MESSAGE)


def _blocked_get_database():
    raise RuntimeError(DB_BLOCKED_MESSAGE)


@pytest.fixture(autouse=True)
def _sin_emulador(monkeypatch):
    monkeypatch.delenv("FIREBASE_AUTH_EMULATOR_HOST", raising=False)


@pytest.fixture(autouse=True)
def _block_real_fcm(monkeypatch):
    # Ninguna push real puede salir de la suite, aunque exista la cuenta de servicio.
    # Los tests de fcm_client vuelven a parchear send_each_for_multicast con su fake.
    monkeypatch.setattr(
        "app.integrations.fcm_client.messaging.send_each_for_multicast",
        _blocked_send_each_for_multicast,
    )


@pytest.fixture(autouse=True)
def _db_en_memoria(monkeypatch):
    """La suite usa siempre los repositorios en memoria y nunca abre la base real (D-R3-17a).

    DATABASE_URL pasa a sqlite (los proveedores devuelven InMemory) y get_database queda
    bloqueado. Devuelve la get_database original para los tests que la necesitan.
    """
    real_get_database = db_session.get_database
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    real_get_database.cache_clear()
    monkeypatch.setattr(db_session, "get_database", _blocked_get_database)
    try:
        yield real_get_database
    finally:
        get_settings.cache_clear()
        real_get_database.cache_clear()


@pytest.fixture
def sql_db():
    """Database sobre la base PostgreSQL de pruebas, todo dentro de una transacción revertida.

    Solo con CS_SQL_TESTS=1 (D-R3-17b). La URL se lee dentro del proceso, nunca se imprime
    ni se registra, y en este orden (D-R3-25e): CS_TEST_DATABASE_URL de backend-fastapi/.env,
    CS_TEST_DATABASE_URL del entorno y DATABASE_URL de backend-fastapi/.env. Cada
    operación del repositorio corre en un SAVEPOINT; al final se revierte la transacción
    exterior. Nunca ejecutar los test_sql_* con -l ni --showlocals.
    """
    if os.environ.get(SQL_TESTS_FLAG) != "1":
        pytest.skip(f"sin {SQL_TESTS_FLAG}=1")

    env_file_values = dotenv_values(BACKEND_DIR / ".env")
    database_url = (
        env_file_values.get(SQL_TESTS_URL_OVERRIDE)
        or os.environ.get(SQL_TESTS_URL_OVERRIDE)
        or env_file_values.get("DATABASE_URL")
    )
    del env_file_values
    if not database_url or not db_session.is_postgres_url(database_url):
        del database_url
        pytest.fail("La base de pruebas no es PostgreSQL o no está configurada", pytrace=False)

    engine = db_session.build_engine(database_url)
    del database_url
    connection = None
    failure = None
    try:
        connection = engine.connect()
    except Exception as exc:
        failure = type(exc).__name__
    if connection is None:
        engine.dispose()
        pytest.fail(f"No se pudo conectar a la base de pruebas ({failure})", pytrace=False)

    outer = connection.begin()
    try:
        yield db_session.Database.for_connection(connection)
    finally:
        try:
            if outer.is_active:
                outer.rollback()
        finally:
            connection.close()
            engine.dispose()


@pytest.fixture
def sql_user(sql_db):
    """Fábrica que inserta un usuario de prueba con SQL directo y devuelve (uid, id_usuario).

    Tabla usuario: firebase_uid "test-<uuid4>", nombre, apellido, correo (el dado o
    "test-<uuid4>@example.invalid"), correo_verificado y creado_en = actualizado_en = ahora
    UTC. Todo se revierte con la transacción de sql_db.
    """

    def make(
        first_name: str = "Test",
        last_name: str = "Usuario",
        email: str | None = None,
        verified: bool = False,
    ) -> tuple[str, int]:
        uid = f"test-{uuid.uuid4()}"
        correo = email if email is not None else f"test-{uuid.uuid4()}@example.invalid"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with sql_db.transaction() as conn:
            user_id = conn.execute(
                text(
                    f"INSERT INTO {T_USUARIO} "
                    "(firebase_uid, nombre, apellido, correo, correo_verificado, "
                    "creado_en, actualizado_en) "
                    "VALUES (:uid, :nombre, :apellido, :correo, :verificado, :now, :now) "
                    "RETURNING id_usuario"
                ),
                {
                    "uid": uid,
                    "nombre": first_name,
                    "apellido": last_name,
                    "correo": correo,
                    "verificado": verified,
                    "now": now,
                },
            ).scalar_one()
        return uid, int(user_id)

    return make


@pytest.fixture
def sql_contact(sql_db):
    """Fábrica que inserta un contacto de un adulto con SQL directo (D-R3-25a).

    ``adult_user_id`` es el id_usuario del adulto (el segundo valor de ``sql_user``).
    Tabla contacto: usuario_id NULL, foto_url NULL, creado_en = actualizado_en = ahora UTC
    sin microsegundos. Tabla adulto_contacto: adulto_mayor_id = adult_user_id, nombre,
    apellido, relacion 'Amigo', telefono NULL, correo = email, es_confianza = help_alerts,
    sin favorito (es_favorito false, orden_favorito NULL), permite_llamadas = calls,
    permite_videollamadas = video_calls, permite_mensajes = messages, estado,
    configurado_por_id = adult_user_id y las mismas fechas. Devuelve id_adulto_contacto
    (el id del contacto en la API, en texto con mapping.id_str). Todo se revierte con la
    transacción de sql_db.
    """

    def make(
        adult_user_id: int,
        *,
        first_name: str = "Contacto",
        last_name: str = "Prueba",
        email: str | None = None,
        help_alerts: bool = False,
        calls: bool = True,
        video_calls: bool = True,
        messages: bool = True,
        estado: str = "ACTIVO",
    ) -> int:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with sql_db.transaction() as conn:
            contact_id = conn.execute(
                text(
                    f"INSERT INTO {T_CONTACTO} "
                    "(usuario_id, foto_url, creado_en, actualizado_en) "
                    "VALUES (NULL, NULL, :now, :now) "
                    "RETURNING id_contacto"
                ),
                {"now": now},
            ).scalar_one()
            adult_contact_id = conn.execute(
                text(
                    f"INSERT INTO {T_ADULTO_CONTACTO} "
                    "(adulto_mayor_id, contacto_id, nombre, apellido, relacion, telefono, "
                    "correo, es_confianza, es_favorito, orden_favorito, permite_llamadas, "
                    "permite_videollamadas, permite_mensajes, estado, configurado_por_id, "
                    "creado_en, actualizado_en) "
                    "VALUES (:adulto, :contacto, :nombre, :apellido, 'Amigo', NULL, "
                    ":correo, :confianza, false, NULL, :llamadas, :videollamadas, "
                    ":mensajes, :estado, :adulto, :now, :now) "
                    "RETURNING id_adulto_contacto"
                ),
                {
                    "adulto": int(adult_user_id),
                    "contacto": int(contact_id),
                    "nombre": first_name,
                    "apellido": last_name,
                    "correo": email,
                    "confianza": help_alerts,
                    "llamadas": calls,
                    "videollamadas": video_calls,
                    "mensajes": messages,
                    "estado": estado,
                    "now": now,
                },
            ).scalar_one()
        return int(adult_contact_id)

    return make
