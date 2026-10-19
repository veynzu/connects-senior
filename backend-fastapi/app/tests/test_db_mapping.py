"""Tests de app/db/mapping.py (plan ronda 3, F3).

Las conversiones puras se prueban directamente. Los helpers con conexión se ejecutan sobre
sqlite en memoria con el esquema connects_senior adjunto y las funciones de PostgreSQL que
usan (btrim, greatest, clock_timestamp) emuladas: sirve para detectar errores de Python
(parámetros, lectura de filas); la semántica real la cubren los test_sql_* contra
PostgreSQL con CS_SQL_TESTS=1.
"""

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from app.db import mapping
from app.models.enums import CallStatus


EXPECTED_EXPORTS = [
    "T_USUARIO",
    "T_ROL",
    "T_PERMISO",
    "T_USUARIO_ROL",
    "T_ROL_PERMISO",
    "T_VINCULO_USUARIO",
    "T_CONTACTO",
    "T_ADULTO_CONTACTO",
    "T_IDEMPOTENCIA_OPERACION",
    "T_COMUNICACION",
    "T_MENSAJE_VOZ",
    "T_COMUNICACION_HISTORIAL_ESTADO",
    "T_SOLICITUD_AYUDA",
    "T_SOLICITUD_AYUDA_NOTIFICACION",
    "T_SOLICITUD_AYUDA_HISTORIAL_ESTADO",
    "T_MENSAJE_TEXTO",
    "T_RECORDATORIO",
    "T_RECORDATORIO_HISTORIAL_ESTADO",
    "T_DISPOSITIVO_FCM",
    "OP_CREAR_COMUNICACION",
    "OP_CREAR_SOLICITUD_AYUDA",
    "TIPO_LLAMADA_AUDIO",
    "TIPO_VIDEOLLAMADA",
    "TIPO_MENSAJE_VOZ",
    "CALL_STATUS_TO_DB",
    "call_status_from_db",
    "tipo_for_audio_only",
    "audio_only_from_tipo",
    "location_state",
    "offset_text",
    "tzinfo_from_text",
    "as_utc",
    "utc_now",
    "parse_id",
    "id_str",
    "daily_room_url",
    "user_id_for_uid",
    "uid_for_user_id",
    "ensure_user_id",
    "user_full_name",
    "release_email",
    "new_idempotency",
    "contact_user_id",
]
BOGOTA = timezone(timedelta(hours=-5))


# --- Exportaciones y constantes ---


def test_exports_exactly_the_planned_names():
    assert mapping.__all__ == EXPECTED_EXPORTS
    for name in EXPECTED_EXPORTS:
        assert hasattr(mapping, name)


def test_table_constants_are_schema_qualified():
    tables = {name: getattr(mapping, name) for name in EXPECTED_EXPORTS if name.startswith("T_")}

    assert len(tables) == 19
    for name, value in tables.items():
        assert value == f"connects_senior.{name[2:].lower()}"


def test_literal_constants_match_schema_checks():
    assert mapping.OP_CREAR_COMUNICACION == "CREAR_COMUNICACION"
    assert mapping.OP_CREAR_SOLICITUD_AYUDA == "CREAR_SOLICITUD_AYUDA"
    assert mapping.TIPO_LLAMADA_AUDIO == "LLAMADA_AUDIO"
    assert mapping.TIPO_VIDEOLLAMADA == "VIDEOLLAMADA"
    assert mapping.TIPO_MENSAJE_VOZ == "MENSAJE_VOZ"


# --- parse_id / id_str ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12", 12),
        ("0", 0),
        (str(2**63 - 1), 2**63 - 1),
        ("abc", None),
        ("", None),
        ("-1", None),
        ("1e3", None),
        ("²", None),
        ("١٢", None),
        (" 12", None),
        ("12 ", None),
        (str(2**63), None),
        (None, None),
        (12, None),
    ],
)
def test_parse_id(value, expected):
    assert mapping.parse_id(value) == expected


def test_id_str():
    assert mapping.id_str(12) == "12"
    assert mapping.parse_id(mapping.id_str(2**40)) == 2**40


# --- Estados y tipos de llamada ---


def test_call_status_to_db_map():
    assert mapping.CALL_STATUS_TO_DB == {
        CallStatus.COMPLETADA: "FINALIZADA",
        CallStatus.NO_CONTESTADA: "NO_ATENDIDA",
        CallStatus.CANCELADA: "CANCELADA",
    }


@pytest.mark.parametrize(
    ("db_value", "expected"),
    [
        ("FINALIZADA", CallStatus.COMPLETADA),
        ("CANCELADA", CallStatus.CANCELADA),
        ("NO_ATENDIDA", CallStatus.NO_CONTESTADA),
        ("INICIADA", CallStatus.NO_CONTESTADA),
        ("ESTABLECIDA", CallStatus.NO_CONTESTADA),
        ("FALLIDA", CallStatus.NO_CONTESTADA),
    ],
)
def test_call_status_from_db(db_value, expected):
    assert mapping.call_status_from_db(db_value) is expected


@pytest.mark.parametrize("status", list(CallStatus))
def test_call_status_round_trip(status):
    assert mapping.call_status_from_db(mapping.CALL_STATUS_TO_DB[status]) is status


def test_tipo_and_audio_only_round_trip():
    assert mapping.tipo_for_audio_only(True) == "LLAMADA_AUDIO"
    assert mapping.tipo_for_audio_only(False) == "VIDEOLLAMADA"
    assert mapping.audio_only_from_tipo(mapping.tipo_for_audio_only(True)) is True
    assert mapping.audio_only_from_tipo(mapping.tipo_for_audio_only(False)) is False


# --- Ubicación ---


@pytest.mark.parametrize(
    ("latitude", "longitude", "expected"),
    [
        (4.6, -74.08, "DISPONIBLE"),
        (0.0, 0.0, "DISPONIBLE"),
        (None, None, "NO_DISPONIBLE"),
        (4.6, None, "NO_DISPONIBLE"),
        (None, -74.08, "NO_DISPONIBLE"),
    ],
)
def test_location_state(latitude, longitude, expected):
    assert mapping.location_state(latitude, longitude) == expected


# --- Fechas y zonas ---


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        (BOGOTA, "-05:00"),
        (timezone.utc, "+00:00"),
        (timezone(timedelta(hours=5, minutes=30)), "+05:30"),
        (timezone(-timedelta(hours=3, minutes=30)), "-03:30"),
    ],
)
def test_offset_text(tz, expected):
    assert mapping.offset_text(datetime(2026, 10, 16, 8, 30, tzinfo=tz)) == expected


def test_offset_text_rejects_naive_datetime():
    with pytest.raises(ValueError):
        mapping.offset_text(datetime(2026, 10, 16, 8, 30))


@pytest.mark.parametrize(
    ("value", "expected_offset"),
    [
        ("-05:00", timedelta(hours=-5)),
        ("+05:30", timedelta(hours=5, minutes=30)),
        ("+00:00", timedelta(0)),
    ],
)
def test_tzinfo_from_offset_text(value, expected_offset):
    tz = mapping.tzinfo_from_text(value)

    assert tz.utcoffset(None) == expected_offset


def test_tzinfo_from_iana_name():
    tz = mapping.tzinfo_from_text("America/Bogota")

    assert isinstance(tz, ZoneInfo)
    assert tz.key == "America/Bogota"
    assert datetime(2026, 10, 16, 8, 30, tzinfo=tz).utcoffset() == timedelta(hours=-5)


@pytest.mark.parametrize("value", ["basura", "", None, "+25:00", "-05:75", "5:00", "../etc"])
def test_tzinfo_from_invalid_text_is_utc(value):
    assert mapping.tzinfo_from_text(value) is timezone.utc


@pytest.mark.parametrize(
    "tz", [BOGOTA, timezone(timedelta(hours=5, minutes=30)), timezone.utc]
)
def test_scheduled_at_round_trip_keeps_offset(tz):
    original = datetime(2026, 10, 16, 8, 30, tzinfo=tz)
    stored_utc = original.astimezone(timezone.utc)
    stored_zone = mapping.offset_text(original)

    restored = stored_utc.astimezone(mapping.tzinfo_from_text(stored_zone))

    assert restored == original
    assert restored.utcoffset() == original.utcoffset()
    assert restored.isoformat() == original.isoformat()


def test_as_utc():
    aware = datetime(2026, 10, 16, 8, 30, tzinfo=BOGOTA)
    naive = datetime(2026, 10, 16, 13, 30)

    assert mapping.as_utc(aware) == aware
    assert mapping.as_utc(aware).utcoffset() == timedelta(0)
    assert mapping.as_utc(aware).hour == 13
    assert mapping.as_utc(naive) == datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
    assert mapping.as_utc(None) is None


def test_utc_now_has_no_microseconds():
    now = mapping.utc_now()

    assert now.utcoffset() == timedelta(0)
    assert now.microsecond == 0
    assert abs(datetime.now(timezone.utc) - now) < timedelta(seconds=5)


# --- Daily ---


@pytest.mark.parametrize(
    "domain",
    ["acme", "acme.daily.co", "https://acme.daily.co/", "https://acme.daily.co", " acme "],
)
def test_daily_room_url_normalizes_domain(domain):
    assert mapping.daily_room_url(domain, "sala-1") == "https://acme.daily.co/sala-1"


def test_daily_room_url_without_room_is_empty():
    assert mapping.daily_room_url("acme", None) == ""
    assert mapping.daily_room_url("acme", "") == ""


# --- Helpers con conexión (sqlite que emula lo necesario del esquema) ---


def _btrim(value):
    return None if value is None else value.strip()


def _greatest(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _clock_timestamp():
    return datetime.now(timezone.utc).isoformat()


_EMULATED_SCHEMA = [
    """CREATE TABLE connects_senior.usuario (
        id_usuario INTEGER PRIMARY KEY AUTOINCREMENT,
        firebase_uid TEXT NOT NULL UNIQUE CHECK (length(btrim(firebase_uid)) > 0),
        nombre TEXT NOT NULL CHECK (length(btrim(nombre)) > 0),
        apellido TEXT NOT NULL CHECK (length(btrim(apellido)) > 0),
        correo TEXT NOT NULL CHECK (length(btrim(correo)) > 0),
        correo_verificado BOOLEAN NOT NULL DEFAULT false,
        creado_en TEXT NOT NULL,
        actualizado_en TEXT NOT NULL CHECK (actualizado_en >= creado_en)
    )""",
    "CREATE UNIQUE INDEX connects_senior.uq_usuario_correo_ci ON usuario (lower(btrim(correo)))",
    """CREATE TABLE connects_senior.adulto_contacto (
        id_adulto_contacto INTEGER PRIMARY KEY,
        correo TEXT
    )""",
    """CREATE TABLE connects_senior.idempotencia_operacion (
        id_idempotencia INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        operacion TEXT NOT NULL
            CHECK (operacion IN ('CREAR_COMUNICACION', 'CREAR_SOLICITUD_AYUDA')),
        clave_hash TEXT NOT NULL CHECK (length(clave_hash) >= 32),
        hash_solicitud TEXT NOT NULL CHECK (length(hash_solicitud) >= 32),
        estado TEXT NOT NULL CHECK (estado IN ('EN_PROCESO', 'COMPLETADA', 'FALLIDA')),
        creado_en TEXT NOT NULL,
        expira_en TEXT NOT NULL CHECK (expira_en > creado_en),
        completado_en TEXT CHECK (completado_en IS NULL OR completado_en >= creado_en),
        UNIQUE (usuario_id, operacion, clave_hash)
    )""",
]


@pytest.fixture
def conn(monkeypatch):
    # Adaptador explícito: el de fábrica de sqlite3 para datetime está obsoleto desde 3.12.
    monkeypatch.setitem(
        sqlite3.adapters, (datetime, sqlite3.PrepareProtocol), lambda value: value.isoformat()
    )
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        dbapi_connection.create_function("btrim", 1, _btrim, deterministic=True)
        dbapi_connection.create_function("greatest", -1, _greatest, deterministic=True)
        dbapi_connection.create_function("clock_timestamp", 0, _clock_timestamp)
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS connects_senior")

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        connection.exec_driver_sql("BEGIN")

    with engine.connect() as connection:
        with connection.begin():
            for statement in _EMULATED_SCHEMA:
                connection.exec_driver_sql(statement)
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()
    engine.dispose()


def _insert_user(conn, uid, nombre="Ana", apellido="Pérez", correo=None, verificado=False):
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return conn.execute(
        text(
            "INSERT INTO connects_senior.usuario "
            "(firebase_uid, nombre, apellido, correo, correo_verificado, creado_en, actualizado_en) "
            "VALUES (:uid, :nombre, :apellido, :correo, :verificado, :now, :now) "
            "RETURNING id_usuario"
        ),
        {
            "uid": uid,
            "nombre": nombre,
            "apellido": apellido,
            "correo": correo or f"{uid}@example.invalid",
            "verificado": verificado,
            "now": created,
        },
    ).scalar_one()


def _user_row(conn, uid):
    return conn.execute(
        text(
            "SELECT nombre, apellido, correo, correo_verificado, creado_en, actualizado_en "
            "FROM connects_senior.usuario WHERE firebase_uid = :uid"
        ),
        {"uid": uid},
    ).one()


def test_user_id_and_uid_round_trip(conn):
    user_id = _insert_user(conn, "uid-a")

    assert mapping.user_id_for_uid(conn, "uid-a") == user_id
    assert mapping.uid_for_user_id(conn, user_id) == "uid-a"
    assert mapping.user_id_for_uid(conn, "uid-inexistente") is None
    assert mapping.user_id_for_uid(conn, "") is None
    assert mapping.uid_for_user_id(conn, 999) is None
    assert mapping.uid_for_user_id(conn, None) is None


def test_ensure_user_id_creates_minimal_row_once(conn):
    user_id = mapping.ensure_user_id(conn, "uid-a")

    nombre, apellido, correo, verificado, creado_en, actualizado_en = _user_row(conn, "uid-a")
    assert (nombre, apellido, correo) == ("-", "-", "uid-a@sin-correo.local")
    assert not verificado
    assert creado_en == actualizado_en
    assert mapping.ensure_user_id(conn, "uid-a", "otro@example.invalid") == user_id
    assert _user_row(conn, "uid-a")[2] == "uid-a@sin-correo.local"


def test_ensure_user_id_returns_existing_row_untouched(conn):
    user_id = _insert_user(conn, "uid-a", correo="ana@example.invalid")

    assert mapping.ensure_user_id(conn, "uid-a", "nuevo@example.invalid") == user_id
    assert _user_row(conn, "uid-a")[:3] == ("Ana", "Pérez", "ana@example.invalid")


def test_ensure_user_id_normalizes_email_and_releases_stale_owner(conn, caplog):
    _insert_user(conn, "uid-viejo", correo="Ana@Example.invalid ", verificado=True)

    with caplog.at_level(logging.WARNING, logger="app.db.mapping"):
        new_id = mapping.ensure_user_id(conn, "uid-nuevo", "  ANA@example.invalid")

    assert mapping.user_id_for_uid(conn, "uid-nuevo") == new_id
    assert _user_row(conn, "uid-nuevo")[2] == "ana@example.invalid"
    _, _, correo_viejo, verificado_viejo, _, _ = _user_row(conn, "uid-viejo")
    assert correo_viejo == "uid-viejo@sin-correo.local"
    assert not verificado_viejo
    assert len(caplog.records) == 1
    assert "ana@" not in caplog.text.lower()
    assert "uid-" not in caplog.text


def test_ensure_user_id_rejects_empty_uid(conn):
    with pytest.raises(ValueError):
        mapping.ensure_user_id(conn, "")


def test_release_email_skips_empty_email_and_same_uid(conn):
    _insert_user(conn, "uid-a", correo="ana@example.invalid")

    assert mapping.release_email(conn, "", "uid-b") == 0
    assert mapping.release_email(conn, "ANA@example.invalid", "uid-a") == 0
    assert _user_row(conn, "uid-a")[2] == "ana@example.invalid"
    assert mapping.release_email(conn, "ANA@example.invalid", "uid-b") == 1
    assert _user_row(conn, "uid-a")[2] == "uid-a@sin-correo.local"


def test_user_full_name(conn):
    complete = _insert_user(conn, "uid-a", nombre="Ana", apellido="Pérez")
    only_name = _insert_user(conn, "uid-b", nombre="Luis", apellido="-")
    minimal = mapping.ensure_user_id(conn, "uid-c")

    assert mapping.user_full_name(conn, complete) == "Ana Pérez"
    assert mapping.user_full_name(conn, only_name) == "Luis"
    assert mapping.user_full_name(conn, minimal) == ""
    assert mapping.user_full_name(conn, 999) == ""
    assert mapping.user_full_name(conn, None) == ""


def test_new_idempotency_inserts_completed_row(conn):
    user_id = _insert_user(conn, "uid-a")

    first = mapping.new_idempotency(conn, user_id, mapping.OP_CREAR_COMUNICACION, "cuerpo")
    second = mapping.new_idempotency(conn, user_id, mapping.OP_CREAR_SOLICITUD_AYUDA, "cuerpo")

    assert first != second
    row = conn.execute(
        text(
            "SELECT usuario_id, operacion, clave_hash, hash_solicitud, estado, "
            "creado_en, expira_en, completado_en "
            "FROM connects_senior.idempotencia_operacion WHERE id_idempotencia = :id"
        ),
        {"id": first},
    ).one()
    usuario_id, operacion, clave_hash, hash_solicitud, estado, creado, expira, completado = row
    assert usuario_id == user_id
    assert operacion == "CREAR_COMUNICACION"
    assert len(clave_hash) == 32
    int(clave_hash, 16)
    assert hash_solicitud == hashlib.sha256(b"cuerpo").hexdigest()
    assert estado == "COMPLETADA"
    assert completado == creado
    assert datetime.fromisoformat(expira) - datetime.fromisoformat(creado) == timedelta(hours=24)
    assert datetime.fromisoformat(creado).microsecond == 0


def test_new_idempotency_rejects_unknown_operation(conn):
    user_id = _insert_user(conn, "uid-a")

    with pytest.raises(ValueError):
        mapping.new_idempotency(conn, user_id, "BORRAR_TODO", "cuerpo")


def test_contact_user_id(conn):
    user_id = _insert_user(conn, "uid-a", correo="ana@example.invalid")
    conn.execute(
        text(
            "INSERT INTO connects_senior.adulto_contacto (id_adulto_contacto, correo) "
            "VALUES (1, ' ANA@Example.invalid'), (2, NULL), (3, 'nadie@example.invalid')"
        )
    )

    assert mapping.contact_user_id(conn, 1) == user_id
    assert mapping.contact_user_id(conn, "1") == user_id
    assert mapping.contact_user_id(conn, 2) is None
    assert mapping.contact_user_id(conn, 3) is None
    assert mapping.contact_user_id(conn, 99) is None
    assert mapping.contact_user_id(conn, "abc") is None
    assert mapping.contact_user_id(conn, None) is None
