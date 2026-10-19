"""Integración de SqlAlertRepository contra PostgreSQL (plan ronda 3, D-R3-10, D-R3-26b, 5.5).

Solo con CS_SQL_TESTS=1. Todo corre dentro de la transacción exterior de sql_db, que se
revierte al terminar. La base es compartida: los asserts sobre list_pending filtran por
los ids creados aquí. Nunca ejecutar con -l ni --showlocals.
"""
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db.mapping import (
    T_IDEMPOTENCIA_OPERACION,
    T_SOLICITUD_AYUDA,
    T_SOLICITUD_AYUDA_HISTORIAL_ESTADO,
    T_USUARIO,
)
from app.models.alerts import AlertRecord
from app.models.enums import AlertCategory, AlertStatus
from app.repositories.alert_repository import SqlAlertRepository


pytestmark = pytest.mark.skipif(
    os.environ.get("CS_SQL_TESTS") != "1", reason="sin CS_SQL_TESTS=1"
)

T0 = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
SERVICE_ADULT_NAME = "Nombre que puso el servicio"
MISSING_ID = str(2**63 - 1)


@pytest.fixture
def repo(sql_db) -> SqlAlertRepository:
    return SqlAlertRepository(sql_db)


@pytest.fixture
def adult(sql_user) -> tuple[str, int]:
    return sql_user("Rosa", "Gómez")


@pytest.fixture
def family(sql_user) -> tuple[str, int]:
    return sql_user("Ana", "Pérez")


def _record(
    adult_uid: str,
    *,
    category: AlertCategory | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    created_at: datetime = T0,
    status: AlertStatus = AlertStatus.PENDIENTE,
    adult_name: str = SERVICE_ADULT_NAME,
    has_trusted_contacts: bool = False,
) -> AlertRecord:
    """Registro como lo arma alert_service.create_alert (id uuid4 que add reemplaza)."""
    return AlertRecord(
        id=str(uuid.uuid4()),
        adult_uid=adult_uid,
        adult_name=adult_name,
        category=category,
        status=status,
        created_at=created_at,
        latitude=latitude,
        longitude=longitude,
        has_trusted_contacts=has_trusted_contacts,
        last_notified_at=created_at,
    )


def _alert_row(sql_db, alert_id: str):
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT adulto_mayor_id, id_idempotencia, categoria, estado, "
                "estado_ubicacion, latitud, longitud, precision_metros, "
                "ronda_notificacion, ultimo_envio_en, creada_en, "
                f"actualizada_en, cerrada_en FROM {T_SOLICITUD_AYUDA} "
                "WHERE id_solicitud_ayuda = :id"
            ),
            {"id": int(alert_id)},
        ).one()


def _history(sql_db, alert_id: str) -> list[tuple]:
    with sql_db.transaction() as conn:
        rows = conn.execute(
            text(
                "SELECT estado_nuevo, cambiado_por_id, cambiado_en "
                f"FROM {T_SOLICITUD_AYUDA_HISTORIAL_ESTADO} "
                "WHERE solicitud_ayuda_id = :id ORDER BY id_historial_estado"
            ),
            {"id": int(alert_id)},
        ).all()
    return [tuple(row) for row in rows]


def _execute(sql_db, statement: str, **params) -> None:
    with sql_db.transaction() as conn:
        conn.execute(text(statement), params)


def _acknowledge(repo: SqlAlertRepository, alert_id: str, uid: str) -> AlertRecord:
    """Mismo efecto que H4: pendiente -> en_atencion con quien la atiende."""
    current = repo.get(alert_id)
    stored = repo.update_if_status(
        replace(current, status=AlertStatus.EN_ATENCION, acknowledged_by=uid),
        AlertStatus.PENDIENTE,
    )
    assert stored is not None
    return stored


# --- add y lecturas derivadas ---


def test_add_without_location_stores_no_disponible_and_null_category(
    repo, sql_db, adult
):
    adult_uid, adult_id = adult
    record = _record(adult_uid)

    added = repo.add(record)

    assert added.id.isdigit()
    # El registro devuelto es el recibido con el id nuevo (category sigue None).
    assert added == replace(record, id=added.id)
    assert added.category is None

    row = _alert_row(sql_db, added.id)
    assert row.adulto_mayor_id == adult_id
    # V004: sin categoría se guarda NULL (D-R3-26b).
    assert row.categoria is None
    assert row.estado == "pendiente"
    assert row.estado_ubicacion == "NO_DISPONIBLE"
    assert (row.latitud, row.longitud, row.precision_metros) == (None, None, None)
    # Envío inicial: ronda 0 y sin último envío propio (se lee creada_en).
    assert row.ronda_notificacion == 0
    assert row.ultimo_envio_en is None
    assert row.creada_en == T0
    assert row.actualizada_en == T0
    assert row.cerrada_en is None

    with sql_db.transaction() as conn:
        idempotency = conn.execute(
            text(
                "SELECT usuario_id, operacion, estado, char_length(clave_hash), "
                f"char_length(hash_solicitud) FROM {T_IDEMPOTENCIA_OPERACION} "
                "WHERE id_idempotencia = :id"
            ),
            {"id": row.id_idempotencia},
        ).one()
    assert tuple(idempotency) == (
        adult_id,
        "CREAR_SOLICITUD_AYUDA",
        "COMPLETADA",
        32,
        64,
    )
    # Historial de creación: estado inicial con el adulto como autor.
    assert _history(sql_db, added.id) == [("pendiente", adult_id, T0)]

    fetched = repo.get(added.id)
    assert fetched.id == added.id
    assert fetched.adult_uid == adult_uid
    assert fetched.category is None
    assert fetched.status == AlertStatus.PENDIENTE
    assert fetched.created_at == T0
    assert fetched.created_at.utcoffset() == timedelta(0)
    assert fetched.latitude is None and fetched.longitude is None
    assert fetched.notification_round == 0
    assert fetched.last_notified_at == T0
    assert fetched.acknowledged_by is None


def test_add_with_location_stores_disponible_and_six_decimals(repo, sql_db, adult):
    adult_uid, _ = adult

    added = repo.add(
        _record(
            adult_uid,
            category=AlertCategory.COMPANIA,
            latitude=4.1234567,
            longitude=-74.654321,
        )
    )

    assert added.latitude == 4.1234567
    row = _alert_row(sql_db, added.id)
    assert row.estado_ubicacion == "DISPONIBLE"
    assert row.categoria == "compania"
    assert row.latitud == Decimal("4.123457")
    assert row.longitud == Decimal("-74.654321")
    assert row.precision_metros is None

    fetched = repo.get(added.id)
    assert fetched.category == AlertCategory.COMPANIA
    assert fetched.latitude == 4.123457
    assert fetched.longitude == -74.654321
    assert isinstance(fetched.latitude, float)


def test_add_writes_round_and_last_sent_columns(repo, sql_db, adult):
    adult_uid, _ = adult
    sent_at = T0 + timedelta(seconds=90)

    added = repo.add(
        replace(_record(adult_uid), notification_round=1, last_notified_at=sent_at)
    )

    row = _alert_row(sql_db, added.id)
    assert row.ronda_notificacion == 1
    assert row.ultimo_envio_en == sent_at
    fetched = repo.get(added.id)
    assert fetched.notification_round == 1
    assert fetched.last_notified_at == sent_at
    assert fetched.last_notified_at.utcoffset() == timedelta(0)

    # Un último envío anterior a la creación no rompe el CHECK: se guarda NULL y se lee
    # creada_en (D-R3-19).
    early = repo.add(
        replace(_record(adult_uid), last_notified_at=T0 - timedelta(minutes=5))
    )
    assert _alert_row(sql_db, early.id).ultimo_envio_en is None
    assert repo.get(early.id).last_notified_at == T0


def test_category_none_round_trip_in_add_and_update(repo, sql_db, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    assert repo.get(added.id).category is None

    categorized = repo.update(replace(repo.get(added.id), category=AlertCategory.PROBLEMA))

    assert categorized.category == AlertCategory.PROBLEMA
    assert _alert_row(sql_db, added.id).categoria == "problema"

    uncategorized = repo.update(replace(categorized, category=None))

    assert uncategorized.category is None
    assert _alert_row(sql_db, added.id).categoria is None
    assert repo.get(added.id).category is None
    assert repo.list_for_adult(adult_uid)[0].category is None


def test_adult_name_comes_from_current_profile(repo, sql_db, adult):
    adult_uid, adult_id = adult

    added = repo.add(_record(adult_uid))

    assert added.adult_name == SERVICE_ADULT_NAME
    assert repo.get(added.id).adult_name == "Rosa Gómez"

    _execute(
        sql_db,
        f"UPDATE {T_USUARIO} SET nombre = 'Rosalía', "
        "actualizado_en = GREATEST(clock_timestamp(), creado_en) "
        "WHERE id_usuario = :id",
        id=adult_id,
    )
    assert repo.get(added.id).adult_name == "Rosalía Gómez"

    # Adulto sin perfil: add crea la fila mínima y el nombre derivado queda vacío.
    bare_uid = f"test-{uuid.uuid4()}"
    bare = repo.add(_record(bare_uid, adult_name="Nombre del token"))
    fetched = repo.get(bare.id)
    assert fetched.adult_uid == bare_uid
    assert fetched.adult_name == ""


def test_has_trusted_contacts_is_recalculated(repo, adult, sql_contact):
    adult_uid, adult_id = adult

    added = repo.add(_record(adult_uid, has_trusted_contacts=True))

    assert added.has_trusted_contacts is True
    assert repo.get(added.id).has_trusted_contacts is False

    sql_contact(adult_id, help_alerts=False)
    sql_contact(adult_id, help_alerts=True, estado="INACTIVO")
    assert repo.get(added.id).has_trusted_contacts is False

    sql_contact(adult_id, help_alerts=True)
    assert repo.get(added.id).has_trusted_contacts is True
    assert repo.list_for_adult(adult_uid)[0].has_trusted_contacts is True


# --- update_if_status y update ---


def test_update_if_status_with_expected_status_replaces_and_returns(
    repo, sql_db, adult, family
):
    adult_uid, adult_id = adult
    family_uid, family_id = family
    added = repo.add(_record(adult_uid))

    stored = _acknowledge(repo, added.id, family_uid)

    assert stored.id == added.id
    assert stored.status == AlertStatus.EN_ATENCION
    assert stored.acknowledged_by == family_uid
    assert stored.notification_round == 0
    assert stored.adult_name == "Rosa Gómez"
    assert repo.get(added.id) == stored
    assert [(state, by) for state, by, _ in _history(sql_db, added.id)] == [
        ("pendiente", adult_id),
        ("en_atencion", family_id),
    ]
    row = _alert_row(sql_db, added.id)
    assert row.estado == "en_atencion"
    assert row.actualizada_en >= row.creada_en
    assert row.cerrada_en is None
    # Un cambio de estado sin reenvío no toca la ronda ni el último envío.
    assert row.ronda_notificacion == 0
    assert row.ultimo_envio_en is None
    assert stored.last_notified_at == T0


def test_update_if_status_with_other_status_returns_none_and_changes_nothing(
    repo, sql_db, adult, family
):
    adult_uid, _ = adult
    family_uid, _ = family
    added = repo.add(_record(adult_uid))
    acknowledged = _acknowledge(repo, added.id, family_uid)
    row_before = tuple(_alert_row(sql_db, added.id))
    history_before = _history(sql_db, added.id)

    result = repo.update_if_status(
        replace(
            acknowledged,
            status=AlertStatus.ATENDIDA,
            notification_round=2,
            last_notified_at=T0 + timedelta(minutes=5),
        ),
        AlertStatus.PENDIENTE,
    )

    assert result is None
    assert repo.get(added.id) == acknowledged
    assert tuple(_alert_row(sql_db, added.id)) == row_before
    assert _history(sql_db, added.id) == history_before
    assert (
        repo.update_if_status(replace(acknowledged, id=MISSING_ID), AlertStatus.EN_ATENCION)
        is None
    )


def test_acknowledged_by_round_trip(repo, sql_db, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    # Quien atiende puede no tener perfil todavía: se crea su fila mínima.
    helper_uid = f"test-{uuid.uuid4()}"

    acknowledged = _acknowledge(repo, added.id, helper_uid)
    closed = repo.update(replace(acknowledged, status=AlertStatus.ATENDIDA))

    assert acknowledged.acknowledged_by == helper_uid
    assert closed.acknowledged_by == helper_uid
    assert repo.get(added.id).acknowledged_by == helper_uid

    # H5 a en_atencion sin nadie registrado: la fila de historial no tiene usuario.
    other = repo.add(_record(adult_uid))
    updated = repo.update(
        replace(repo.get(other.id), status=AlertStatus.EN_ATENCION, acknowledged_by=None)
    )
    assert updated.status == AlertStatus.EN_ATENCION
    assert updated.acknowledged_by is None
    assert _history(sql_db, other.id)[-1][:2] == ("en_atencion", None)


def test_notification_round_and_last_sent_persist_in_columns(repo, sql_db, adult):
    adult_uid, adult_id = adult
    added = repo.add(_record(adult_uid))
    resent_at = T0 + timedelta(seconds=120)

    stored = repo.update_if_status(
        replace(repo.get(added.id), notification_round=2, last_notified_at=resent_at),
        AlertStatus.PENDIENTE,
    )

    assert stored.notification_round == 2
    assert stored.last_notified_at == resent_at
    assert stored.last_notified_at.microsecond == 0
    assert stored.last_notified_at.utcoffset() == timedelta(0)
    assert stored.status == AlertStatus.PENDIENTE
    row = _alert_row(sql_db, added.id)
    assert row.ronda_notificacion == 2
    assert row.ultimo_envio_en == resent_at
    assert row.estado == "pendiente"
    # D-R3-26b: las rondas ya no dejan filas 'pendiente' en el historial.
    assert _history(sql_db, added.id) == [("pendiente", adult_id, T0)]

    later = resent_at + timedelta(seconds=120)
    third = repo.update_if_status(
        replace(stored, notification_round=3, last_notified_at=later),
        AlertStatus.PENDIENTE,
    )
    assert third.notification_round == 3
    assert third.last_notified_at == later
    assert repo.get(added.id) == third
    row = _alert_row(sql_db, added.id)
    assert (row.ronda_notificacion, row.ultimo_envio_en) == (3, later)
    assert _history(sql_db, added.id) == [("pendiente", adult_id, T0)]

    # Un último envío anterior a la creación se ajusta a creada_en (CHECK, D-R3-19).
    clamped = repo.update(replace(third, last_notified_at=T0 - timedelta(hours=1)))
    assert clamped.last_notified_at == T0
    assert clamped.notification_round == 3
    assert _alert_row(sql_db, added.id).ultimo_envio_en == T0


def test_update_to_pendiente_does_not_change_round_or_last_sent(
    repo, sql_db, adult, family
):
    adult_uid, adult_id = adult
    family_uid, family_id = family
    added = repo.add(_record(adult_uid))
    resent_at = T0 + timedelta(seconds=120)
    repo.update_if_status(
        replace(repo.get(added.id), notification_round=1, last_notified_at=resent_at),
        AlertStatus.PENDIENTE,
    )
    acknowledged = _acknowledge(repo, added.id, family_uid)
    history_before = _history(sql_db, added.id)
    assert [(state, by) for state, by, _ in history_before] == [
        ("pendiente", adult_id),
        ("en_atencion", family_id),
    ]

    reopened = repo.update(replace(acknowledged, status=AlertStatus.PENDIENTE))

    assert reopened.status == AlertStatus.PENDIENTE
    assert reopened.notification_round == 1
    assert reopened.last_notified_at == resent_at
    assert reopened.acknowledged_by == family_uid
    # Sin fila 'pendiente' nueva en el historial (D-R3-26b).
    assert _history(sql_db, added.id) == history_before
    row = _alert_row(sql_db, added.id)
    assert row.estado == "pendiente"
    assert (row.ronda_notificacion, row.ultimo_envio_en) == (1, resent_at)


def test_atendida_sets_cerrada_en_and_leaving_it_clears_it(repo, sql_db, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))

    closed = repo.update(replace(repo.get(added.id), status=AlertStatus.ATENDIDA))

    assert closed.status == AlertStatus.ATENDIDA
    row = _alert_row(sql_db, added.id)
    assert row.estado == "atendida"
    assert row.cerrada_en is not None
    assert row.cerrada_en >= row.creada_en
    assert row.actualizada_en >= row.creada_en
    assert _history(sql_db, added.id)[-1][:2] == ("atendida", None)

    reopened = repo.update(replace(closed, status=AlertStatus.EN_ATENCION))

    assert reopened.status == AlertStatus.EN_ATENCION
    row = _alert_row(sql_db, added.id)
    assert row.estado == "en_atencion"
    assert row.cerrada_en is None


def test_cerrada_written_with_sql_reads_as_atendida(repo, sql_db, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    _execute(
        sql_db,
        f"UPDATE {T_SOLICITUD_AYUDA} SET estado = 'cerrada', cerrada_en = creada_en, "
        "actualizada_en = GREATEST(clock_timestamp(), creada_en) "
        "WHERE id_solicitud_ayuda = :id",
        id=int(added.id),
    )

    fetched = repo.get(added.id)

    assert fetched.status == AlertStatus.ATENDIDA
    assert added.id not in {record.id for record in repo.list_pending()}
    assert repo.update_if_status(fetched, AlertStatus.PENDIENTE) is None
    # Esperar atendida acepta 'cerrada' y, sin cambio de estado, el valor se conserva.
    kept = repo.update_if_status(fetched, AlertStatus.ATENDIDA)
    assert kept is not None
    assert kept.status == AlertStatus.ATENDIDA
    assert _alert_row(sql_db, added.id).estado == "cerrada"


def test_update_returns_reread_record_and_replaces_location(
    repo, sql_db, adult, family, sql_contact
):
    adult_uid, adult_id = adult
    family_uid, _ = family
    added = repo.add(_record(adult_uid, adult_name="Del servicio"))
    sql_contact(adult_id, help_alerts=True)

    stored = repo.update(
        replace(added, status=AlertStatus.EN_ATENCION, acknowledged_by=family_uid)
    )

    # Releído: category None sigue None (NULL en la fila); nombre y confianza derivados.
    assert stored.category is None
    assert _alert_row(sql_db, added.id).categoria is None
    assert stored.adult_name == "Rosa Gómez"
    assert stored.has_trusted_contacts is True
    assert stored.acknowledged_by == family_uid

    located = repo.update(replace(stored, latitude=1.5, longitude=-2.25))
    assert (located.latitude, located.longitude) == (1.5, -2.25)
    assert _alert_row(sql_db, added.id).estado_ubicacion == "DISPONIBLE"

    cleared = repo.update(replace(located, latitude=None, longitude=None))
    assert (cleared.latitude, cleared.longitude) == (None, None)
    row = _alert_row(sql_db, added.id)
    assert row.estado_ubicacion == "NO_DISPONIBLE"
    assert (row.latitud, row.longitud, row.precision_metros) == (None, None, None)
    assert repo.update(replace(stored, id=MISSING_ID)) is None


# --- Listas ---


def test_list_for_adult_desc_with_tie_by_id_desc(repo, adult, sql_user):
    adult_uid, _ = adult
    other_uid, _ = sql_user("Otro", "Adulto")
    first = repo.add(_record(adult_uid, created_at=T0))
    second = repo.add(_record(adult_uid, created_at=T0 + timedelta(minutes=1)))
    third = repo.add(_record(adult_uid, created_at=T0 + timedelta(minutes=1)))
    repo.add(_record(other_uid, created_at=T0 + timedelta(minutes=2)))

    listed = repo.list_for_adult(adult_uid)

    assert [record.id for record in listed] == [third.id, second.id, first.id]
    assert all(record.adult_name == "Rosa Gómez" for record in listed)
    assert repo.list_for_adult(f"test-{uuid.uuid4()}") == []
    assert repo.list_for_adult("") == []


def test_list_pending_only_pending_in_id_order(repo, adult, family, sql_user):
    adult_uid, _ = adult
    family_uid, _ = family
    other_uid, _ = sql_user("Otro", "Adulto")
    pending_1 = repo.add(_record(adult_uid))
    attended = repo.add(_record(adult_uid))
    repo.update(replace(repo.get(attended.id), status=AlertStatus.ATENDIDA))
    pending_2 = repo.add(_record(adult_uid, created_at=T0 - timedelta(hours=1)))
    in_progress = repo.add(_record(adult_uid))
    _acknowledge(repo, in_progress.id, family_uid)
    pending_3 = repo.add(_record(other_uid))
    ours = {pending_1.id, attended.id, pending_2.id, in_progress.id, pending_3.id}

    pending = [record for record in repo.list_pending() if record.id in ours]

    assert [record.id for record in pending] == [pending_1.id, pending_2.id, pending_3.id]
    assert all(record.status == AlertStatus.PENDIENTE for record in pending)


def test_non_numeric_or_missing_ids_return_none(repo, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    base = repo.get(added.id)

    for bad_id in ["abc", "", "-1", "1e3", "²", " 12", MISSING_ID]:
        assert repo.get(bad_id) is None
        assert repo.update(replace(base, id=bad_id)) is None
        assert repo.update_if_status(replace(base, id=bad_id), AlertStatus.PENDIENTE) is None

    assert repo.get(added.id) == base
