"""Integración de SqlContactRepository contra PostgreSQL (plan ronda 3, D-R3-7, D-R3-26d; 5.3).

Solo con CS_SQL_TESTS=1: cada test corre dentro de la transacción revertida de sql_db
(conftest.py), así que nada queda escrito en la base. La base es compartida: los asserts
sobre listas se limitan a los ids creados en el test. Nunca ejecutar con -l ni --showlocals.
"""
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import session as db_session
from app.db.mapping import T_ADULTO_CONTACTO, T_CONTACTO, T_USUARIO, id_str
from app.models.contacts import Contact, ContactPermissions
from app.repositories import contact_repository
from app.repositories.contact_repository import (
    FAVORITE_ORDER_CONSTRAINT,
    InMemoryContactRepository,
    SqlContactRepository,
)
from app.repositories.errors import ContactOrderConflictError


pytestmark = pytest.mark.skipif(
    os.environ.get("CS_SQL_TESTS") != "1", reason="sin CS_SQL_TESTS=1"
)

ALL_PERMISSIONS = ContactPermissions(
    calls=True, video_calls=True, messages=True, help_alerts=True
)
# Cada booleano distinto de sus vecinos para detectar columnas cruzadas.
MIXED_PERMISSIONS = ContactPermissions(
    calls=True, video_calls=False, messages=False, help_alerts=True
)
OTHER_PERMISSIONS = ContactPermissions(
    calls=False, video_calls=True, messages=True, help_alerts=False
)
BAD_IDS = ["abc", "", "-1", "1e3", "²", " 12", str(2**63)]


@pytest.fixture
def repo(sql_db) -> SqlContactRepository:
    return SqlContactRepository(sql_db)


def _contact(
    first_name: str = "Ana",
    last_name: str = "Pérez",
    *,
    contact_id: str = "id-del-servicio",
    relationship: str = "Hija",
    phone: str | None = "+573001234567",
    email: str | None = "ana@example.invalid",
    photo_url: str | None = "https://example.invalid/ana.png",
    is_favorite: bool = False,
    favorite_order: int = 0,
    permissions: ContactPermissions = MIXED_PERMISSIONS,
) -> Contact:
    return Contact(
        id=contact_id,
        first_name=first_name,
        last_name=last_name,
        relationship=relationship,
        phone=phone,
        email=email,
        photo_url=photo_url,
        is_favorite=is_favorite,
        favorite_order=favorite_order,
        permissions=permissions.model_copy(),
    )


def _row(sql_db, contact_id: str) -> dict:
    """Fila de adulto_contacto con las columnas de su contacto (prefijo contacto_).

    foto_url es la de adulto_contacto (V004); contacto_foto_url la de contacto (queda NULL).
    """
    with sql_db.transaction() as conn:
        row = conn.execute(
            text(
                "SELECT ac.*, c.usuario_id AS contacto_usuario_id, "
                "c.foto_url AS contacto_foto_url, c.creado_en AS contacto_creado_en, "
                "c.actualizado_en AS contacto_actualizado_en "
                f"FROM {T_ADULTO_CONTACTO} ac "
                f"JOIN {T_CONTACTO} c ON c.id_contacto = ac.contacto_id "
                "WHERE ac.id_adulto_contacto = :id"
            ),
            {"id": int(contact_id)},
        ).mappings().one()
    return dict(row)


def _ids(contacts: list[Contact], wanted: list[str]) -> list[str]:
    """Ids de la lista que pertenecen a este test, en el orden en que llegan."""
    return [contact.id for contact in contacts if contact.id in set(wanted)]


# --- add / get ---


def test_add_returns_numeric_id_and_keeps_received_fields(repo, sql_db, sql_user):
    adult_uid, adult_id = sql_user()
    received = _contact("Ángela", "Muñoz", is_favorite=True, favorite_order=2)

    created = repo.add(adult_uid, received)

    assert created.id.isascii() and created.id.isdigit()
    assert created.model_dump(exclude={"id"}) == received.model_dump(exclude={"id"})
    assert received.id == "id-del-servicio"
    assert repo.get(adult_uid, created.id) == created

    row = _row(sql_db, created.id)
    assert row["adulto_mayor_id"] == adult_id
    assert row["configurado_por_id"] == adult_id
    assert row["estado"] == "ACTIVO"
    assert (row["nombre"], row["apellido"], row["relacion"]) == ("Ángela", "Muñoz", "Hija")
    assert (row["telefono"], row["correo"]) == ("+573001234567", "ana@example.invalid")
    assert row["contacto_usuario_id"] is None
    assert row["foto_url"] == "https://example.invalid/ana.png"
    assert row["contacto_foto_url"] is None
    assert (
        row["permite_llamadas"],
        row["permite_videollamadas"],
        row["permite_mensajes"],
        row["es_confianza"],
    ) == (True, False, False, True)
    assert (row["es_favorito"], row["orden_favorito"]) == (True, 2)
    assert row["creado_en"] == row["actualizado_en"]
    assert row["contacto_creado_en"] == row["contacto_actualizado_en"]


def test_add_does_not_share_objects_with_the_received_contact(repo, sql_user):
    adult_uid, _ = sql_user()
    received = _contact()

    created = repo.add(adult_uid, received)
    created.permissions.calls = False

    assert received.permissions.calls is True


def test_add_for_adult_without_profile_creates_minimal_user(repo, sql_db):
    adult_uid = f"test-{uuid.uuid4()}"

    created = repo.add(adult_uid, _contact())

    with sql_db.transaction() as conn:
        user = conn.execute(
            text(
                f"SELECT id_usuario, nombre, apellido, correo FROM {T_USUARIO} "
                "WHERE firebase_uid = :uid"
            ),
            {"uid": adult_uid},
        ).mappings().one()
    assert (user["nombre"], user["apellido"]) == ("-", "-")
    assert user["correo"] == f"{adult_uid}@sin-correo.local"
    assert _row(sql_db, created.id)["configurado_por_id"] == user["id_usuario"]
    assert repo.find(created.id) == (adult_uid, repo.get(adult_uid, created.id))


def test_relationship_of_100_chars_fits(repo, sql_db, sql_user):
    # relacion es varchar(100) en V004 (D-R3-26d).
    adult_uid, _ = sql_user()
    created = repo.add(adult_uid, _contact(relationship="r" * 100))

    assert repo.get(adult_uid, created.id).relationship == "r" * 100
    assert _row(sql_db, created.id)["relacion"] == "r" * 100

    updated = repo.update(adult_uid, created.model_copy(update={"relationship": "s" * 100}))

    assert updated.relationship == "s" * 100
    assert _row(sql_db, created.id)["relacion"] == "s" * 100


def test_blank_phone_and_email_are_stored_as_null(repo, sql_db, sql_user):
    adult_uid, _ = sql_user()

    created = repo.add(adult_uid, _contact(phone="  ", email="", photo_url=None))

    row = _row(sql_db, created.id)
    assert (row["telefono"], row["correo"], row["foto_url"]) == (None, None, None)
    assert row["contacto_foto_url"] is None
    stored = repo.get(adult_uid, created.id)
    assert (stored.phone, stored.email, stored.photo_url) == (None, None, None)


# --- favorite_order ---


def test_favorite_order_zero_is_stored_as_null_and_read_as_zero(repo, sql_db, sql_user):
    adult_uid, _ = sql_user()

    favorite_zero = repo.add(adult_uid, _contact("Zoe", is_favorite=True, favorite_order=0))
    plain_five = repo.add(adult_uid, _contact("Beto", is_favorite=False, favorite_order=5))

    assert _row(sql_db, favorite_zero.id)["orden_favorito"] is None
    assert _row(sql_db, favorite_zero.id)["es_favorito"] is True
    assert _row(sql_db, plain_five.id)["orden_favorito"] is None
    # add devuelve lo recibido (D-R3-4); la lectura deriva 0 (AV-09e).
    assert plain_five.favorite_order == 5
    read_zero = repo.get(adult_uid, favorite_zero.id)
    read_five = repo.get(adult_uid, plain_five.id)
    assert (read_zero.is_favorite, read_zero.favorite_order) == (True, 0)
    assert (read_five.is_favorite, read_five.favorite_order) == (False, 0)


def test_favorite_with_order_3_round_trip(repo, sql_db, sql_user):
    adult_uid, _ = sql_user()

    created = repo.add(adult_uid, _contact(is_favorite=True, favorite_order=3))

    assert _row(sql_db, created.id)["orden_favorito"] == 3
    stored = repo.get(adult_uid, created.id)
    assert (stored.is_favorite, stored.favorite_order) == (True, 3)
    assert repo.list_for_adult(adult_uid) == [stored]


def _favorite_order_index_exists(sql_db) -> bool:
    """El índice único de favoritos estaba en V003; V004 lo quitó (D-R3-26d)."""
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                "WHERE schemaname = 'connects_senior' AND indexname = :name)"
            ),
            {"name": FAVORITE_ORDER_CONSTRAINT},
        ).scalar_one()


def test_duplicate_favorite_order_follows_the_schema_and_transaction_stays_usable(
    repo, sql_db, sql_user
):
    adult_uid, _ = sql_user()
    other_adult_uid, _ = sql_user()
    first = repo.add(adult_uid, _contact("Ana", is_favorite=True, favorite_order=1))
    second = repo.add(adult_uid, _contact("Beto"))
    duplicate = second.model_copy(update={"is_favorite": True, "favorite_order": 1})

    if _favorite_order_index_exists(sql_db):
        # Esquema V003: uq_adulto_contacto_orden_favorito -> ContactOrderConflictError.
        with pytest.raises(ContactOrderConflictError) as exc_info:
            repo.update(adult_uid, duplicate)
        # Sin la causa SQL encadenada (trae la sentencia y el DETAIL de Postgres).
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True
        # El SAVEPOINT se revirtió: nada cambió.
        assert repo.get(adult_uid, second.id) == second
        with pytest.raises(ContactOrderConflictError):
            repo.add(adult_uid, _contact("Carla", is_favorite=True, favorite_order=1))
        assert len(repo.list_for_adult(adult_uid)) == 2
    else:
        # Esquema V004 sin el índice único: se guarda igual que InMemory.
        assert repo.update(adult_uid, duplicate) == duplicate
        assert repo.get(adult_uid, second.id) == duplicate

    # En ambos casos la conexión sigue utilizable.
    assert repo.get(adult_uid, first.id).favorite_order == 1
    moved = repo.update(
        adult_uid, second.model_copy(update={"is_favorite": True, "favorite_order": 2})
    )
    assert (moved.is_favorite, moved.favorite_order) == (True, 2)
    assert _ids(repo.list_for_adult(adult_uid), [first.id, second.id]) == [
        first.id,
        second.id,
    ]
    # Otro adulto puede usar el mismo orden con cualquier esquema.
    other = repo.add(other_adult_uid, _contact(is_favorite=True, favorite_order=1))
    assert repo.get(other_adult_uid, other.id).favorite_order == 1


def test_other_integrity_errors_propagate_and_transaction_stays_usable(repo, sql_user):
    adult_uid, _ = sql_user()
    created = repo.add(adult_uid, _contact())

    # ck_adulto_contacto_nombre (nombre en blanco) no es el índice de favoritos: sube tal cual.
    with pytest.raises(IntegrityError):
        repo.update(adult_uid, created.model_copy(update={"first_name": "   "}))
    with pytest.raises(IntegrityError):
        repo.add(adult_uid, _contact(first_name="   "))

    assert repo.get(adult_uid, created.id) == created
    assert _ids(repo.list_for_adult(adult_uid), [created.id]) == [created.id]
    assert len(repo.list_for_adult(adult_uid)) == 1


# --- list / find ---


def test_list_for_adult_in_insertion_order_and_only_that_adult(repo, sql_user):
    adult_uid, _ = sql_user()
    other_adult_uid, _ = sql_user()
    zoe = repo.add(adult_uid, _contact("Zoe"))
    foreign = repo.add(other_adult_uid, _contact("Eva"))
    ana = repo.add(adult_uid, _contact("Ana", is_favorite=True, favorite_order=1))
    beto = repo.add(adult_uid, _contact("Beto"))

    listed = repo.list_for_adult(adult_uid)

    assert _ids(listed, [zoe.id, ana.id, beto.id, foreign.id]) == [zoe.id, ana.id, beto.id]
    assert len(listed) == 3
    assert listed[1] == repo.get(adult_uid, ana.id)
    assert repo.list_for_adult(f"test-{uuid.uuid4()}") == []
    assert repo.list_for_adult("") == []


def test_find_returns_adult_uid_and_contact(repo, sql_user):
    adult_uid, _ = sql_user()
    other_adult_uid, _ = sql_user()
    created = repo.add(adult_uid, _contact(is_favorite=True, favorite_order=4))
    repo.add(other_adult_uid, _contact("Luis"))

    found = repo.find(created.id)

    assert found == (adult_uid, created)


def test_reads_fixture_rows_and_ignores_inactive(repo, sql_user, sql_contact):
    adult_uid, adult_id = sql_user()
    active_id = id_str(sql_contact(adult_id, first_name="Activo", help_alerts=True))
    inactive_id = id_str(sql_contact(adult_id, first_name="Baja", estado="INACTIVO"))

    assert _ids(repo.list_for_adult(adult_uid), [active_id, inactive_id]) == [active_id]
    assert repo.get(adult_uid, inactive_id) is None
    assert repo.find(inactive_id) is None
    assert repo.delete(adult_uid, inactive_id) is False
    active = repo.get(adult_uid, active_id)
    assert active.first_name == "Activo"
    assert active.relationship == "Amigo"
    assert active.permissions == ALL_PERMISSIONS
    assert (active.is_favorite, active.favorite_order) == (False, 0)
    assert (active.phone, active.email, active.photo_url) == (None, None, None)


# --- update ---


def test_update_changes_permissions_and_photo_and_keeps_id(repo, sql_db, sql_user):
    adult_uid, _ = sql_user()
    created = repo.add(adult_uid, _contact())
    before = _row(sql_db, created.id)
    changed = created.model_copy(
        update={
            "first_name": "Ana María",
            "relationship": "Sobrina",
            "phone": None,
            "email": "nueva@example.invalid",
            "photo_url": "https://example.invalid/nueva.png",
            "permissions": OTHER_PERMISSIONS.model_copy(),
            # No favorito con orden: se guarda NULL y se relee 0.
            "favorite_order": 7,
        }
    )

    updated = repo.update(adult_uid, changed)

    assert updated.id == created.id
    assert updated == changed.model_copy(update={"favorite_order": 0})
    assert repo.get(adult_uid, created.id) == updated
    after = _row(sql_db, created.id)
    assert after["foto_url"] == "https://example.invalid/nueva.png"
    assert after["contacto_foto_url"] is None
    assert after["telefono"] is None
    assert after["orden_favorito"] is None
    assert (
        after["permite_llamadas"],
        after["permite_videollamadas"],
        after["permite_mensajes"],
        after["es_confianza"],
    ) == (False, True, True, False)
    assert after["contacto_id"] == before["contacto_id"]
    assert after["actualizado_en"] >= after["creado_en"]
    # La fila de contacto no se actualiza (la foto ya no vive ahí).
    assert after["contacto_actualizado_en"] == before["contacto_actualizado_en"]


# --- foto_url (V004: por adulto en adulto_contacto) ---


def test_photo_url_round_trip_lives_in_adulto_contacto(repo, sql_db, sql_user):
    adult_uid, _ = sql_user()
    other_adult_uid, _ = sql_user()
    created = repo.add(adult_uid, _contact(photo_url="https://example.invalid/a.png"))
    other = repo.add(other_adult_uid, _contact(photo_url="https://example.invalid/b.png"))

    assert repo.get(adult_uid, created.id).photo_url == "https://example.invalid/a.png"
    assert repo.get(other_adult_uid, other.id).photo_url == "https://example.invalid/b.png"
    assert repo.find(created.id)[1].photo_url == "https://example.invalid/a.png"
    assert _row(sql_db, created.id)["foto_url"] == "https://example.invalid/a.png"
    assert _row(sql_db, other.id)["foto_url"] == "https://example.invalid/b.png"

    cleared = repo.update(adult_uid, created.model_copy(update={"photo_url": None}))

    assert cleared.photo_url is None
    assert repo.get(adult_uid, created.id).photo_url is None
    assert _row(sql_db, created.id)["foto_url"] is None

    replaced = repo.update(
        adult_uid, cleared.model_copy(update={"photo_url": "https://example.invalid/c.png"})
    )

    assert replaced.photo_url == "https://example.invalid/c.png"
    assert repo.list_for_adult(adult_uid) == [replaced]
    assert _row(sql_db, created.id)["foto_url"] == "https://example.invalid/c.png"
    # contacto.foto_url sigue en NULL en todas las filas creadas por el repositorio.
    assert _row(sql_db, created.id)["contacto_foto_url"] is None
    assert _row(sql_db, other.id)["contacto_foto_url"] is None
    # El otro adulto conserva su foto.
    assert _row(sql_db, other.id)["foto_url"] == "https://example.invalid/b.png"


def test_update_favorite_then_unfavorite(repo, sql_db, sql_user):
    adult_uid, _ = sql_user()
    created = repo.add(adult_uid, _contact())

    favorite = repo.update(
        adult_uid, created.model_copy(update={"is_favorite": True, "favorite_order": 2})
    )
    assert (favorite.is_favorite, favorite.favorite_order) == (True, 2)
    assert _row(sql_db, created.id)["orden_favorito"] == 2

    plain = repo.update(adult_uid, favorite.model_copy(update={"is_favorite": False}))

    assert (plain.is_favorite, plain.favorite_order) == (False, 0)
    assert _row(sql_db, created.id)["orden_favorito"] is None


def test_update_of_other_adult_or_missing_returns_none(repo, sql_user):
    adult_uid, _ = sql_user()
    other_adult_uid, _ = sql_user()
    created = repo.add(adult_uid, _contact())
    changed = created.model_copy(update={"first_name": "Cambiado"})

    assert repo.update(other_adult_uid, changed) is None
    assert repo.update(f"test-{uuid.uuid4()}", changed) is None
    assert repo.update(adult_uid, changed.model_copy(update={"id": str(2**63 - 1)})) is None
    assert repo.get(adult_uid, created.id) == created
    assert repo.get(other_adult_uid, created.id) is None


# --- delete ---


def test_delete_is_logical_and_frees_the_favorite_order(repo, sql_db, sql_user):
    adult_uid, _ = sql_user()
    other_adult_uid, _ = sql_user()
    created = repo.add(adult_uid, _contact(is_favorite=True, favorite_order=2))

    assert repo.delete(other_adult_uid, created.id) is False
    assert repo.get(adult_uid, created.id) == created
    assert repo.delete(adult_uid, created.id) is True

    assert repo.get(adult_uid, created.id) is None
    assert repo.find(created.id) is None
    assert _ids(repo.list_for_adult(adult_uid), [created.id]) == []
    assert repo.update(adult_uid, created) is None
    assert repo.delete(adult_uid, created.id) is False
    row = _row(sql_db, created.id)
    assert (row["estado"], row["es_favorito"], row["orden_favorito"]) == (
        "INACTIVO",
        False,
        None,
    )
    assert row["actualizado_en"] >= row["creado_en"]
    reused = repo.add(adult_uid, _contact("Beto", is_favorite=True, favorite_order=2))
    assert repo.get(adult_uid, reused.id).favorite_order == 2


# --- ids no numéricos ---


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_non_numeric_ids_are_missing_without_error(repo, sql_user, bad_id):
    adult_uid, _ = sql_user()
    repo.add(adult_uid, _contact())

    assert repo.get(adult_uid, bad_id) is None
    assert repo.find(bad_id) is None
    assert repo.delete(adult_uid, bad_id) is False
    assert repo.update(adult_uid, _contact(contact_id=bad_id)) is None


# --- proveedor ---


def test_provider_returns_sql_repository_when_postgres(monkeypatch):
    fake_database = object()
    monkeypatch.setattr(db_session, "sql_enabled", lambda: True)
    monkeypatch.setattr(db_session, "get_database", lambda: fake_database)
    monkeypatch.setattr(
        contact_repository,
        "_sql_contact_repository",
        db_session.lazy_sql_repository(SqlContactRepository),
    )

    provided = contact_repository.get_contact_repository()

    assert isinstance(provided, SqlContactRepository)
    assert provided._db is fake_database
    assert contact_repository.get_contact_repository() is provided

    monkeypatch.setattr(db_session, "sql_enabled", lambda: False)
    assert isinstance(contact_repository.get_contact_repository(), InMemoryContactRepository)
    assert (
        contact_repository.get_contact_repository()
        is contact_repository._default_contact_repository
    )
