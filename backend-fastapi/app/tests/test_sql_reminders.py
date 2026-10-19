"""Integración de SqlReminderRepository contra PostgreSQL (plan ronda 3, D-R3-11, 5.6,
D-R3-26(c): baja lógica con eliminado_en y offset en desfase_utc).

Solo corren con CS_SQL_TESTS=1; todo ocurre dentro de la transacción revertida de la
fixture sql_db. La base es compartida: los asserts de listados se filtran por los ids
creados en cada test. Nunca ejecutar con -l ni --showlocals.
"""
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.db.mapping import T_RECORDATORIO, T_RECORDATORIO_HISTORIAL_ESTADO
from app.models.enums import ReminderKind, ReminderRepeat, ReminderStatus
from app.models.reminders import ReminderRecord
from app.repositories.reminder_repository import SqlReminderRepository


pytestmark = pytest.mark.skipif(
    os.environ.get("CS_SQL_TESTS") != "1", reason="sin CS_SQL_TESTS=1"
)

BOGOTA_OFFSET = timezone(timedelta(hours=-5))
KOLKATA_OFFSET = timezone(timedelta(hours=5, minutes=30))
# 2030-01-15 08:30 en Bogotá = 13:30 UTC.
T0 = datetime(2030, 1, 15, 8, 30, tzinfo=BOGOTA_OFFSET)
CREATED_AT = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
DESCRIPTION = "Tomar la pastilla de la presión"
DEFAULT_ZONE = "America/Bogota"


@pytest.fixture
def repo(sql_db):
    return SqlReminderRepository(sql_db)


@pytest.fixture
def adult(sql_user):
    return sql_user("Ana", "Pérez")


def _record(
    adult_uid: str,
    scheduled_at: datetime = T0,
    *,
    status: ReminderStatus = ReminderStatus.PENDIENTE,
    repeat: ReminderRepeat = ReminderRepeat.NINGUNA,
    description: str = DESCRIPTION,
    notified_at: datetime | None = None,
    next_created: bool = False,
) -> ReminderRecord:
    return ReminderRecord(
        id="ignorado-por-sql",
        adult_uid=adult_uid,
        kind=ReminderKind.MEDICAMENTO,
        description=description,
        scheduled_at=scheduled_at,
        repeat=repeat,
        status=status,
        created_at=CREATED_AT,
        notified_at=notified_at,
        next_created=next_created,
    )


def _row(sql_db, reminder_id: str):
    """Fila cruda de recordatorio, incluida una dada de baja (sin filtrar eliminado_en)."""
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT programado_para, desfase_utc, zona_horaria, estado, descripcion, "
                "notificado_en, siguiente_ocurrencia_creada, recordatorio_origen_id, "
                "adulto_mayor_id, creado_por_id, creado_en, actualizado_en, eliminado_en "
                f"FROM {T_RECORDATORIO} WHERE id_recordatorio = :id"
            ),
            {"id": int(reminder_id)},
        ).one_or_none()


def _history(sql_db, reminder_id: str):
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT evento, estado_nuevo, cambiado_por_id, registrado_en, "
                "programado_para_nuevo "
                f"FROM {T_RECORDATORIO_HISTORIAL_ESTADO} "
                "WHERE recordatorio_id = :id ORDER BY id_historial_estado"
            ),
            {"id": int(reminder_id)},
        ).all()


def _events(sql_db, reminder_id: str) -> list[str]:
    return [row.evento for row in _history(sql_db, reminder_id)]


def _insert_raw(sql_db, adult_id: int, *, desfase_utc: str | None, zona_horaria: str) -> str:
    """Recordatorio escrito con SQL directo (como lo dejaría otra herramienta)."""
    with sql_db.transaction() as conn:
        reminder_id = conn.execute(
            text(
                f"INSERT INTO {T_RECORDATORIO} "
                "(adulto_mayor_id, creado_por_id, tipo, descripcion, programado_para, "
                "desfase_utc, zona_horaria, repeticion, estado, "
                "siguiente_ocurrencia_creada, creado_en, actualizado_en) "
                "VALUES (:adulto, :adulto, 'medicamento', :descripcion, :programado, "
                ":desfase, :zona, 'diaria', 'pendiente', false, :creado, :creado) "
                "RETURNING id_recordatorio"
            ),
            {
                "adulto": adult_id,
                "descripcion": DESCRIPTION,
                "programado": T0.astimezone(timezone.utc),
                "desfase": desfase_utc,
                "zona": zona_horaria,
                "creado": CREATED_AT,
            },
        ).scalar_one()
    return str(reminder_id)


# --- add, get y desfase_utc ---


def test_add_and_get_roundtrip_keeps_minus_five_offset(sql_db, repo, adult):
    adult_uid, adult_id = adult
    record = _record(adult_uid)

    added = repo.add(record)

    assert added.id.isdigit()
    assert added == replace(record, id=added.id)
    loaded = repo.get(added.id)
    assert loaded == replace(record, id=added.id)
    assert loaded.scheduled_at == T0
    assert loaded.scheduled_at.utcoffset() == timedelta(hours=-5)
    assert loaded.created_at == CREATED_AT
    assert loaded.notified_at is None
    assert loaded.next_created is False
    row = _row(sql_db, added.id)
    assert row.desfase_utc == "-05:00"
    assert row.zona_horaria == DEFAULT_ZONE
    assert row.eliminado_en is None
    assert row.programado_para == T0
    assert row.programado_para.utcoffset() in (None, timedelta(0))
    assert row.adulto_mayor_id == adult_id
    assert row.creado_por_id == adult_id
    assert row.creado_en == row.actualizado_en == CREATED_AT
    assert row.recordatorio_origen_id is None


def test_add_and_get_roundtrip_keeps_plus_five_thirty_offset(sql_db, repo, adult):
    adult_uid, _ = adult
    scheduled_at = datetime(2030, 1, 15, 19, 0, tzinfo=KOLKATA_OFFSET)

    added = repo.add(_record(adult_uid, scheduled_at))

    loaded = repo.get(added.id)
    assert loaded.scheduled_at == scheduled_at
    assert loaded.scheduled_at.utcoffset() == timedelta(hours=5, minutes=30)
    row = _row(sql_db, added.id)
    # desfase_utc manda sobre zona_horaria, que queda en su default.
    assert row.desfase_utc == "+05:30"
    assert row.zona_horaria == DEFAULT_ZONE


@pytest.mark.parametrize(
    ("offset", "expected_desfase", "expected_read_offset"),
    [
        (timedelta(hours=14), "+14:00", timedelta(hours=14)),
        (timedelta(hours=-14), "-14:00", timedelta(hours=-14)),
        (timedelta(hours=-5, seconds=-20), "-05:00", timedelta(hours=-5)),
        (timedelta(hours=5, minutes=30, seconds=40), "+05:31", timedelta(hours=5, minutes=31)),
        (timedelta(hours=15), "+00:00", timedelta(0)),
        (timedelta(hours=-23, minutes=-59), "+00:00", timedelta(0)),
    ],
)
def test_add_normalizes_offsets_to_desfase_check(
    sql_db, repo, adult, offset, expected_desfase, expected_read_offset
):
    adult_uid, _ = adult
    scheduled_at = datetime(2030, 1, 15, 8, 30, tzinfo=timezone(offset))

    added = repo.add(_record(adult_uid, scheduled_at))

    assert _row(sql_db, added.id).desfase_utc == expected_desfase
    loaded = repo.get(added.id)
    # El instante nunca cambia; solo el offset con el que se devuelve.
    assert loaded.scheduled_at == scheduled_at
    assert loaded.scheduled_at.utcoffset() == expected_read_offset


def test_null_desfase_with_iana_zone_is_read_with_zoneinfo(sql_db, repo, adult):
    adult_uid, adult_id = adult
    reminder_id = _insert_raw(sql_db, adult_id, desfase_utc=None, zona_horaria=DEFAULT_ZONE)

    loaded = repo.get(reminder_id)

    assert loaded.adult_uid == adult_uid
    assert loaded.repeat == ReminderRepeat.DIARIA
    assert loaded.scheduled_at == T0
    assert loaded.scheduled_at.tzinfo == ZoneInfo(DEFAULT_ZONE)
    assert loaded.scheduled_at.utcoffset() == timedelta(hours=-5)

    # Sin cambiar la hora (marca del planificador) desfase_utc sigue NULL.
    marked = repo.update(replace(loaded, notified_at=CREATED_AT))
    assert marked.notified_at == CREATED_AT
    assert marked.scheduled_at.tzinfo == ZoneInfo(DEFAULT_ZONE)
    row = _row(sql_db, reminder_id)
    assert row.desfase_utc is None
    assert row.zona_horaria == DEFAULT_ZONE

    # Al cambiar la hora (R6) se escribe desfase_utc y zona_horaria no se toca.
    snoozed = repo.update(
        replace(marked, scheduled_at=marked.scheduled_at + timedelta(minutes=10))
    )
    assert snoozed.scheduled_at == T0 + timedelta(minutes=10)
    assert snoozed.scheduled_at.utcoffset() == timedelta(hours=-5)
    row = _row(sql_db, reminder_id)
    assert row.desfase_utc == "-05:00"
    assert row.zona_horaria == DEFAULT_ZONE


def test_desfase_takes_priority_over_other_iana_zone(sql_db, repo, adult):
    _, adult_id = adult
    reminder_id = _insert_raw(
        sql_db, adult_id, desfase_utc="+05:30", zona_horaria="Europe/Madrid"
    )

    loaded = repo.get(reminder_id)

    assert loaded.scheduled_at == T0
    assert loaded.scheduled_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_add_inserts_created_history(sql_db, repo, adult):
    adult_uid, adult_id = adult

    added = repo.add(_record(adult_uid))

    history = _history(sql_db, added.id)
    assert len(history) == 1
    created = history[0]
    assert created.evento == "CREADO"
    assert created.estado_nuevo == "pendiente"
    assert created.cambiado_por_id == adult_id
    assert created.registrado_en == CREATED_AT
    assert created.programado_para_nuevo == T0


def test_add_creates_minimal_user_for_unknown_adult(sql_db, repo):
    uid = f"test-{uuid.uuid4()}"

    added = repo.add(_record(uid))

    assert repo.get(added.id).adult_uid == uid
    assert [record.id for record in repo.list_for_adult(uid)] == [added.id]


def test_next_created_roundtrip(repo, adult):
    adult_uid, _ = adult

    added = repo.add(_record(adult_uid, next_created=True))
    assert repo.get(added.id).next_created is True

    cleared = repo.update(replace(added, next_created=False))
    assert cleared.next_created is False
    assert repo.get(added.id).next_created is False


# --- update y update_if_status ---


def test_update_changes_description_and_date_inserts_rescheduled(sql_db, repo, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    new_time = datetime(2030, 1, 16, 15, 0, tzinfo=timezone.utc)

    saved = repo.update(
        replace(added, description="Nueva descripción", scheduled_at=new_time)
    )

    assert saved.description == "Nueva descripción"
    assert saved.scheduled_at == new_time
    assert saved.scheduled_at.utcoffset() == timedelta(0)
    assert saved.status == ReminderStatus.PENDIENTE
    assert repo.get(added.id) == saved
    row = _row(sql_db, added.id)
    assert row.desfase_utc == "+00:00"
    assert row.zona_horaria == DEFAULT_ZONE
    assert row.actualizado_en >= row.creado_en
    history = _history(sql_db, added.id)
    assert [item.evento for item in history] == ["CREADO", "REPROGRAMADO"]
    rescheduled = history[1]
    assert rescheduled.estado_nuevo == "pendiente"
    assert rescheduled.programado_para_nuevo == new_time
    assert rescheduled.cambiado_por_id is None
    assert rescheduled.registrado_en >= CREATED_AT


def test_update_with_other_offset_rewrites_desfase(sql_db, repo, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    later = datetime(2030, 1, 15, 21, 0, tzinfo=KOLKATA_OFFSET)

    saved = repo.update(replace(added, scheduled_at=later))

    assert saved.scheduled_at == later
    assert saved.scheduled_at.utcoffset() == timedelta(hours=5, minutes=30)
    row = _row(sql_db, added.id)
    assert row.desfase_utc == "+05:30"
    assert row.zona_horaria == DEFAULT_ZONE


def test_update_same_instant_other_offset_rewrites_desfase_without_event(
    sql_db, repo, adult
):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    same_instant_utc = T0.astimezone(timezone.utc)

    saved = repo.update(replace(added, scheduled_at=same_instant_utc))

    assert saved.scheduled_at == T0
    assert saved.scheduled_at.utcoffset() == timedelta(0)
    row = _row(sql_db, added.id)
    assert row.desfase_utc == "+00:00"
    assert row.zona_horaria == DEFAULT_ZONE
    assert _events(sql_db, added.id) == ["CREADO"]


def test_update_description_only_inserts_no_event(sql_db, repo, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))

    saved = repo.update(replace(added, description="Solo texto"))

    assert saved.description == "Solo texto"
    assert saved.scheduled_at.utcoffset() == timedelta(hours=-5)
    assert _row(sql_db, added.id).desfase_utc == "-05:00"
    assert _events(sql_db, added.id) == ["CREADO"]


def test_update_returns_reread_record_keeping_owner_and_created_at(repo, adult, sql_user):
    adult_uid, _ = adult
    other_uid, _ = sql_user("Otro", "Adulto")
    added = repo.add(_record(adult_uid))

    saved = repo.update(
        replace(
            added,
            adult_uid=other_uid,
            created_at=CREATED_AT + timedelta(days=3),
            description="Cambio",
        )
    )

    assert saved.adult_uid == adult_uid
    assert saved.created_at == CREATED_AT
    assert saved.description == "Cambio"
    assert repo.list_for_adult(other_uid) == []


def test_update_if_status_confirmado_inserts_confirmed(sql_db, repo, adult):
    adult_uid, adult_id = adult
    added = repo.add(_record(adult_uid, repeat=ReminderRepeat.DIARIA))

    saved = repo.update_if_status(
        replace(added, status=ReminderStatus.CONFIRMADO, next_created=True),
        ReminderStatus.PENDIENTE,
    )

    assert saved.status == ReminderStatus.CONFIRMADO
    assert saved.next_created is True
    assert repo.get(added.id).status == ReminderStatus.CONFIRMADO
    history = _history(sql_db, added.id)
    assert [item.evento for item in history] == ["CREADO", "CONFIRMADO"]
    assert history[1].estado_nuevo == "confirmado"
    assert history[1].cambiado_por_id == adult_id
    assert history[1].programado_para_nuevo is None


def test_update_if_status_omitido_inserts_missed(sql_db, repo, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid, notified_at=CREATED_AT))

    saved = repo.update_if_status(
        replace(added, status=ReminderStatus.OMITIDO), ReminderStatus.PENDIENTE
    )

    assert saved.status == ReminderStatus.OMITIDO
    history = _history(sql_db, added.id)
    assert [item.evento for item in history] == ["CREADO", "OMITIDO"]
    assert history[1].estado_nuevo == "omitido"
    assert history[1].cambiado_por_id is None


def test_notified_at_inserts_notified(sql_db, repo, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    notified_at = datetime(2030, 1, 15, 13, 30, tzinfo=timezone.utc)

    saved = repo.update_if_status(
        replace(added, notified_at=notified_at), ReminderStatus.PENDIENTE
    )

    assert saved.notified_at == notified_at
    assert saved.status == ReminderStatus.PENDIENTE
    history = _history(sql_db, added.id)
    assert [item.evento for item in history] == ["CREADO", "NOTIFICADO"]
    assert history[1].estado_nuevo == "pendiente"
    assert history[1].cambiado_por_id is None
    assert _row(sql_db, added.id).notificado_en == notified_at

    # Volver a guardar con la misma marca no repite el evento.
    repo.update(replace(saved, description="Otra"))
    assert _events(sql_db, added.id) == ["CREADO", "NOTIFICADO"]


def test_snooze_like_update_from_confirmed_inserts_rescheduled(sql_db, repo, adult):
    adult_uid, _ = adult
    added = repo.add(
        _record(adult_uid, status=ReminderStatus.CONFIRMADO, notified_at=CREATED_AT)
    )

    saved = repo.update(
        replace(
            added,
            scheduled_at=added.scheduled_at + timedelta(minutes=10),
            status=ReminderStatus.PENDIENTE,
            notified_at=None,
        )
    )

    assert saved.scheduled_at == T0 + timedelta(minutes=10)
    assert saved.scheduled_at.utcoffset() == timedelta(hours=-5)
    assert saved.status == ReminderStatus.PENDIENTE
    assert saved.notified_at is None
    history = _history(sql_db, added.id)
    assert [item.evento for item in history] == ["CREADO", "REPROGRAMADO"]
    assert history[0].estado_nuevo == "confirmado"
    assert history[1].programado_para_nuevo == T0 + timedelta(minutes=10)


def test_update_if_status_with_other_status_returns_none_and_changes_nothing(
    sql_db, repo, adult
):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid, status=ReminderStatus.CONFIRMADO))

    result = repo.update_if_status(
        replace(added, status=ReminderStatus.OMITIDO, description="No"),
        ReminderStatus.PENDIENTE,
    )

    assert result is None
    loaded = repo.get(added.id)
    assert loaded.status == ReminderStatus.CONFIRMADO
    assert loaded.description == DESCRIPTION
    assert _events(sql_db, added.id) == ["CREADO"]


def test_update_missing_or_non_numeric_ids_return_none(repo, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    missing = str(int(added.id) + 1_000_000_000)

    for reminder_id in ("abc", "", "-1", "1e3", "²", str(2**63), missing):
        assert repo.get(reminder_id) is None
        assert repo.update(replace(added, id=reminder_id)) is None
        assert (
            repo.update_if_status(replace(added, id=reminder_id), ReminderStatus.PENDIENTE)
            is None
        )
        assert repo.delete(reminder_id) is False
    assert repo.get(added.id) is not None


# --- delete (baja lógica, D-R3-26(c)) ---


def test_delete_is_logical_and_keeps_history(sql_db, repo, adult):
    adult_uid, _ = adult
    deleted = repo.add(_record(adult_uid, repeat=ReminderRepeat.DIARIA))
    repo.update_if_status(
        replace(deleted, status=ReminderStatus.CONFIRMADO, next_created=True),
        ReminderStatus.PENDIENTE,
    )
    assert _events(sql_db, deleted.id) == ["CREADO", "CONFIRMADO"]

    assert repo.delete(deleted.id) is True

    assert repo.get(deleted.id) is None
    row = _row(sql_db, deleted.id)
    assert row is not None
    assert row.eliminado_en is not None
    assert row.eliminado_en >= row.creado_en
    assert row.actualizado_en >= row.creado_en
    assert row.estado == "confirmado"
    assert row.descripcion == DESCRIPTION
    assert _events(sql_db, deleted.id) == ["CREADO", "CONFIRMADO"]

    # Segundo delete: ya estaba dado de baja.
    assert repo.delete(deleted.id) is False
    assert _row(sql_db, deleted.id).eliminado_en == row.eliminado_en


def test_deleted_reminder_leaves_both_lists(repo, adult):
    adult_uid, _ = adult
    kept = repo.add(_record(adult_uid, T0 + timedelta(hours=1)))
    deleted = repo.add(_record(adult_uid, T0))

    assert repo.delete(deleted.id) is True

    assert [record.id for record in repo.list_for_adult(adult_uid)] == [kept.id]
    pending_ids = [record.id for record in repo.list_pending()]
    assert kept.id in pending_ids
    assert deleted.id not in pending_ids


def test_update_on_deleted_reminder_returns_none_and_writes_nothing(sql_db, repo, adult):
    adult_uid, _ = adult
    added = repo.add(_record(adult_uid))
    assert repo.delete(added.id) is True
    before = tuple(_row(sql_db, added.id))

    assert (
        repo.update(
            replace(added, description="No", scheduled_at=T0 + timedelta(hours=1))
        )
        is None
    )
    assert (
        repo.update_if_status(
            replace(added, status=ReminderStatus.CONFIRMADO), ReminderStatus.PENDIENTE
        )
        is None
    )

    assert tuple(_row(sql_db, added.id)) == before
    assert _events(sql_db, added.id) == ["CREADO"]


def test_delete_does_not_touch_origin_of_following_reminder(sql_db, repo, adult):
    adult_uid, _ = adult
    original = repo.add(_record(adult_uid, repeat=ReminderRepeat.DIARIA))
    following = repo.add(_record(adult_uid, T0 + timedelta(days=1)))
    with sql_db.transaction() as conn:
        conn.execute(
            text(
                f"UPDATE {T_RECORDATORIO} SET recordatorio_origen_id = :origen "
                "WHERE id_recordatorio = :id"
            ),
            {"origen": int(original.id), "id": int(following.id)},
        )

    assert repo.delete(original.id) is True

    assert _row(sql_db, following.id).recordatorio_origen_id == int(original.id)
    assert repo.get(following.id) is not None
    assert _events(sql_db, following.id) == ["CREADO"]


# --- listados ---


def test_list_for_adult_ascending_with_tie_by_id(repo, adult, sql_user):
    adult_uid, _ = adult
    other_uid, _ = sql_user("Otro", "Adulto")
    later = repo.add(_record(adult_uid, T0 + timedelta(hours=1)))
    earliest = repo.add(_record(adult_uid, T0))
    # Mismo instante que "later" expresado con otro offset: empata y gana el id menor.
    tie = repo.add(_record(adult_uid, (T0 + timedelta(hours=1)).astimezone(KOLKATA_OFFSET)))
    repo.add(_record(other_uid, T0 - timedelta(hours=1)))

    listed = repo.list_for_adult(adult_uid)

    assert [record.id for record in listed] == [earliest.id, later.id, tie.id]
    assert int(later.id) < int(tie.id)
    assert listed[2].scheduled_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert all(record.adult_uid == adult_uid for record in listed)


def test_list_for_adult_unknown_uid_is_empty(repo):
    assert repo.list_for_adult("test-uid-que-no-existe") == []
    assert repo.list_for_adult("") == []


def test_list_pending_excludes_closed_and_keeps_order(repo, adult, sql_user):
    adult_uid, _ = adult
    other_uid, _ = sql_user("Otro", "Adulto")
    pending_late = repo.add(_record(adult_uid, T0 + timedelta(hours=2)))
    confirmed = repo.add(_record(adult_uid, T0, status=ReminderStatus.CONFIRMADO))
    pending_other = repo.add(_record(other_uid, T0 + timedelta(hours=1)))
    missed = repo.add(_record(adult_uid, T0, status=ReminderStatus.OMITIDO))
    pending_early = repo.add(_record(adult_uid, T0))
    confirmed_later = repo.add(_record(adult_uid, T0 + timedelta(hours=3)))
    repo.update_if_status(
        replace(confirmed_later, status=ReminderStatus.CONFIRMADO),
        ReminderStatus.PENDIENTE,
    )
    created_ids = {
        pending_late.id,
        confirmed.id,
        pending_other.id,
        missed.id,
        pending_early.id,
        confirmed_later.id,
    }

    pending = [record for record in repo.list_pending() if record.id in created_ids]

    assert [record.id for record in pending] == [
        pending_early.id,
        pending_other.id,
        pending_late.id,
    ]
    assert all(record.status == ReminderStatus.PENDIENTE for record in pending)
