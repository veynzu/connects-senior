"""Integración de SqlDeviceRepository contra PostgreSQL (plan ronda 3, D-R3-8; 5.3).

Solo con CS_SQL_TESTS=1: cada test corre dentro de la transacción revertida de sql_db
(conftest.py), así que nada queda escrito en la base. La base es compartida: los tokens
llevan un uuid4 y los asserts se limitan a los uids creados en el test. Nunca ejecutar
con -l ni --showlocals.
"""
import os
import uuid

import pytest
from sqlalchemy import text

from app.db import session as db_session
from app.db.mapping import T_DISPOSITIVO_FCM, T_USUARIO
from app.repositories import device_repository
from app.repositories.device_repository import (
    InMemoryDeviceRepository,
    SqlDeviceRepository,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("CS_SQL_TESTS") != "1", reason="sin CS_SQL_TESTS=1"
)


@pytest.fixture
def repo(sql_db) -> SqlDeviceRepository:
    return SqlDeviceRepository(sql_db)


def _token(label: str) -> str:
    return f"test-token-{label}-{uuid.uuid4()}"


def _device_rows(sql_db, tokens: list[str]) -> dict[str, dict]:
    """Filas de dispositivo_fcm de esos tokens, por token."""
    with sql_db.transaction() as conn:
        rows = conn.execute(
            text(
                "SELECT id_dispositivo, usuario_id, token_fcm, activo, registrado_en, "
                f"actualizado_en FROM {T_DISPOSITIVO_FCM} "
                "WHERE token_fcm = ANY(:tokens)"
            ),
            {"tokens": tokens},
        ).mappings().all()
    return {row["token_fcm"]: dict(row) for row in rows}


def test_register_is_idempotent(repo, sql_db, sql_user):
    uid, user_id = sql_user()
    first, second = _token("1"), _token("2")
    assert repo.has_device(uid) is False
    assert repo.tokens_of([uid]) == []

    repo.register(uid, first)
    repo.register(uid, first)
    repo.register(uid, second)
    repo.register(uid, first)

    assert repo.tokens_of([uid]) == [first, second]
    assert repo.has_device(uid) is True
    rows = _device_rows(sql_db, [first, second])
    assert set(rows) == {first, second}
    assert rows[first]["id_dispositivo"] < rows[second]["id_dispositivo"]
    for row in rows.values():
        assert row["usuario_id"] == user_id
        assert row["activo"] is True
        assert row["actualizado_en"] >= row["registrado_en"]


def test_register_moves_token_between_uids(repo, sql_db, sql_user):
    family_uid, _ = sql_user()
    caregiver_uid, caregiver_id = sql_user()
    shared, own = _token("shared"), _token("own")
    repo.register(family_uid, shared)
    repo.register(family_uid, own)
    shared_device_id = _device_rows(sql_db, [shared])[shared]["id_dispositivo"]

    repo.register(caregiver_uid, shared)

    assert repo.tokens_of([family_uid]) == [own]
    assert repo.tokens_of([caregiver_uid]) == [shared]
    # Orden por id_dispositivo: el token movido conserva su fila (D-R3-8).
    assert repo.tokens_of([family_uid, caregiver_uid]) == [shared, own]
    row = _device_rows(sql_db, [shared])[shared]
    assert row["id_dispositivo"] == shared_device_id
    assert row["usuario_id"] == caregiver_id
    assert row["activo"] is True

    repo.remove_tokens([own])

    assert repo.has_device(family_uid) is False
    assert repo.has_device(caregiver_uid) is True


def test_tokens_of_orders_by_device_and_has_no_duplicates(repo, sql_user):
    adult_uid, _ = sql_user()
    family_uid, _ = sql_user()
    caregiver_uid, _ = sql_user()
    adult_token = _token("adult")
    family_first, family_second = _token("family-1"), _token("family-2")
    caregiver_token = _token("caregiver")
    repo.register(adult_uid, adult_token)
    repo.register(family_uid, family_first)
    repo.register(caregiver_uid, caregiver_token)
    repo.register(family_uid, family_second)

    assert repo.tokens_of(
        [family_uid, adult_uid, family_uid, f"test-{uuid.uuid4()}"]
    ) == [adult_token, family_first, family_second]
    assert repo.tokens_of(uid for uid in [caregiver_uid]) == [caregiver_token]
    assert repo.tokens_of([]) == []
    assert repo.tokens_of([f"test-{uuid.uuid4()}"]) == []


def test_remove_tokens_deactivates_and_has_device_false(repo, sql_db, sql_user):
    family_uid, _ = sql_user()
    caregiver_uid, _ = sql_user()
    first, second, third = _token("1"), _token("2"), _token("3")
    repo.register(family_uid, first)
    repo.register(family_uid, second)
    repo.register(caregiver_uid, third)

    repo.remove_tokens([first, _token("unknown"), third])

    assert repo.tokens_of([family_uid, caregiver_uid]) == [second]
    assert repo.has_device(caregiver_uid) is False
    rows = _device_rows(sql_db, [first, second, third])
    assert set(rows) == {first, second, third}
    assert (rows[first]["activo"], rows[second]["activo"], rows[third]["activo"]) == (
        False,
        True,
        False,
    )
    assert rows[first]["actualizado_en"] >= rows[first]["registrado_en"]

    repo.remove_tokens([])
    repo.remove_tokens(iter([second]))

    assert repo.has_device(family_uid) is False
    assert repo.tokens_of([family_uid]) == []


def test_remove_tokens_accepts_lazy_iterable_from_itself(repo, sql_user):
    family_uid, _ = sql_user()
    caregiver_uid, _ = sql_user()
    family_token, caregiver_token = _token("family"), _token("caregiver")
    repo.register(family_uid, family_token)
    repo.register(caregiver_uid, caregiver_token)
    lazy_tokens = (
        token for uid in [family_uid] for token in repo.tokens_of([uid])
    )

    repo.remove_tokens(lazy_tokens)

    assert repo.has_device(family_uid) is False
    assert repo.tokens_of([caregiver_uid]) == [caregiver_token]


def test_register_again_reactivates_the_same_row(repo, sql_db, sql_user):
    uid, _ = sql_user()
    token = _token("again")
    repo.register(uid, token)
    device_id = _device_rows(sql_db, [token])[token]["id_dispositivo"]
    repo.remove_tokens([token])
    assert repo.has_device(uid) is False

    repo.register(uid, token)

    assert repo.has_device(uid) is True
    assert repo.tokens_of([uid]) == [token]
    row = _device_rows(sql_db, [token])[token]
    assert (row["id_dispositivo"], row["activo"]) == (device_id, True)
    assert row["actualizado_en"] >= row["registrado_en"]


def test_register_uid_without_profile_creates_minimal_user(repo, sql_db):
    uid = f"test-{uuid.uuid4()}"
    token = _token("minimal")

    repo.register(uid, token)

    with sql_db.transaction() as conn:
        user = conn.execute(
            text(
                f"SELECT id_usuario, nombre, apellido, correo, correo_verificado "
                f"FROM {T_USUARIO} WHERE firebase_uid = :uid"
            ),
            {"uid": uid},
        ).mappings().one()
    assert (user["nombre"], user["apellido"]) == ("-", "-")
    assert user["correo"] == f"{uid}@sin-correo.local"
    assert user["correo_verificado"] is False
    assert _device_rows(sql_db, [token])[token]["usuario_id"] == user["id_usuario"]
    assert repo.has_device(uid) is True
    assert repo.tokens_of([uid]) == [token]


def test_reads_do_not_create_users(repo, sql_db):
    uid = f"test-{uuid.uuid4()}"

    assert repo.has_device(uid) is False
    assert repo.has_device("") is False
    assert repo.tokens_of([uid, ""]) == []

    with sql_db.transaction() as conn:
        count = conn.execute(
            text(f"SELECT count(*) FROM {T_USUARIO} WHERE firebase_uid = :uid"),
            {"uid": uid},
        ).scalar_one()
    assert count == 0


def test_provider_returns_sql_repository_when_postgres(monkeypatch):
    fake_database = object()
    monkeypatch.setattr(db_session, "sql_enabled", lambda: True)
    monkeypatch.setattr(db_session, "get_database", lambda: fake_database)
    monkeypatch.setattr(
        device_repository,
        "_sql_device_repository",
        db_session.lazy_sql_repository(SqlDeviceRepository),
    )

    provided = device_repository.get_device_repository()

    assert isinstance(provided, SqlDeviceRepository)
    assert provided._db is fake_database
    assert device_repository.get_device_repository() is provided

    monkeypatch.setattr(db_session, "sql_enabled", lambda: False)
    assert isinstance(device_repository.get_device_repository(), InMemoryDeviceRepository)
    assert (
        device_repository.get_device_repository()
        is device_repository._default_device_repository
    )
