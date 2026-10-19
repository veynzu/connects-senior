"""Integración de SqlCallRepository contra PostgreSQL (plan ronda 3, D-R3-9, 5.4).

Solo con CS_SQL_TESTS=1. Todo corre dentro de la transacción revertida de ``sql_db``:
nada queda escrito en la base. Los asserts sobre listas se filtran por los ids creados
en cada test porque la base es compartida. Nunca ejecutar con -l ni --showlocals.
"""

import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import session as db_session
from app.db.mapping import (
    T_COMUNICACION,
    T_COMUNICACION_HISTORIAL_ESTADO,
    T_IDEMPOTENCIA_OPERACION,
)
from app.models.calls import CallRecord
from app.models.enums import CallStatus
from app.models.voice_messages import VoiceMessageRecord
from app.repositories import call_repository
from app.repositories.call_repository import SqlCallRepository
from app.repositories.voice_message_repository import SqlVoiceMessageRepository


pytestmark = pytest.mark.skipif(
    os.environ.get("CS_SQL_TESTS") != "1", reason="sin CS_SQL_TESTS=1"
)

BASE = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)
ONE_HOUR = timedelta(hours=1)


@pytest.fixture
def calls(sql_db):
    return SqlCallRepository(sql_db, "acme")


@pytest.fixture
def adult(sql_user):
    """(uid, id_usuario) de un adulto mayor de prueba."""
    return sql_user("Ana", "Pérez")


def _record(
    adult_uid: str,
    contact_id: int | str,
    *,
    started_at: datetime = BASE,
    audio_only: bool = False,
) -> CallRecord:
    room_name = f"sala-{uuid.uuid4().hex[:12]}"
    return CallRecord(
        id=str(uuid.uuid4()),
        adult_uid=adult_uid,
        contact_id=str(contact_id),
        started_at=started_at,
        status=CallStatus.NO_CONTESTADA,
        audio_only=audio_only,
        room_name=room_name,
        # Distinta de la que se reconstruye al leer: prueba que add la devuelve tal cual.
        room_url=f"https://recibida.example.invalid/{room_name}",
        room_expires_at=started_at + timedelta(minutes=37),
    )


def _voice_record(adult_uid: str, contact_id: int | str) -> VoiceMessageRecord:
    return VoiceMessageRecord(
        id=str(uuid.uuid4()),
        adult_uid=adult_uid,
        contact_id=str(contact_id),
        from_adult=True,
        from_name="Ana Pérez",
        file_name=f"{uuid.uuid4()}.webm",
        content_type="audio/webm",
        size_bytes=10,
        created_at=BASE + timedelta(minutes=30),
    )


def _row(sql_db, communication_id: str):
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT estado, tipo_comunicacion, fecha_inicio, fecha_fin, daily_sala_id, "
                "adulto_contacto_id, iniciado_por_id, id_idempotencia, creado_en, "
                f"actualizado_en FROM {T_COMUNICACION} WHERE id_comunicacion = :id"
            ),
            {"id": int(communication_id)},
        ).one()


def _history(sql_db, communication_id: str) -> list[tuple]:
    with sql_db.transaction() as conn:
        rows = conn.execute(
            text(
                "SELECT estado_nuevo, cambiado_por_id, cambiado_en "
                f"FROM {T_COMUNICACION_HISTORIAL_ESTADO} "
                "WHERE comunicacion_id = :id ORDER BY id_historial_estado"
            ),
            {"id": int(communication_id)},
        ).all()
    return [tuple(row) for row in rows]


def _count_for_contact(sql_db, contact_id: int) -> int:
    with sql_db.transaction() as conn:
        return conn.execute(
            text(f"SELECT count(*) FROM {T_COMUNICACION} WHERE adulto_contacto_id = :id"),
            {"id": contact_id},
        ).scalar_one()


# --- add y get ---


def test_add_devuelve_el_registro_recibido_con_id_numerico(calls, adult, sql_contact):
    uid, adult_id = adult
    record = _record(uid, sql_contact(adult_id))

    saved = calls.add(record)

    assert saved.id.isdigit()
    assert saved.id != record.id
    # Todo lo demás tal como vino, incluidos room_url y room_expires_at de Daily.
    assert saved == replace(record, id=saved.id)


def test_get_reconstruye_room_url_y_expiracion(calls, adult, sql_contact):
    uid, adult_id = adult
    contact_id = sql_contact(adult_id)
    record = _record(uid, contact_id)
    saved = calls.add(record)

    got = calls.get(uid, saved.id)

    assert got == CallRecord(
        id=saved.id,
        adult_uid=uid,
        contact_id=str(contact_id),
        started_at=BASE,
        status=CallStatus.NO_CONTESTADA,
        audio_only=False,
        room_name=record.room_name,
        room_url=f"https://acme.daily.co/{record.room_name}",
        room_expires_at=BASE + ONE_HOUR,
    )
    assert got.started_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "domain",
    ["acme", "acme.daily.co", "https://acme.daily.co/", "https://acme.daily.co"],
)
def test_room_url_se_reconstruye_con_el_dominio_normalizado(
    sql_db, adult, sql_contact, domain
):
    uid, adult_id = adult
    repository = SqlCallRepository(sql_db, domain)
    saved = repository.add(_record(uid, sql_contact(adult_id)))

    got = repository.get(uid, saved.id)

    assert got.room_url == f"https://acme.daily.co/{saved.room_name}"
    assert [r.room_url for r in repository.list_for_adult(uid) if r.id == saved.id] == [
        got.room_url
    ]


def test_add_guarda_iniciada_con_idempotencia_e_historial(calls, adult, sql_contact, sql_db):
    uid, adult_id = adult
    contact_id = sql_contact(adult_id)
    record = _record(uid, contact_id)

    saved = calls.add(record)

    row = _row(sql_db, saved.id)
    assert row.estado == "INICIADA"
    assert row.tipo_comunicacion == "VIDEOLLAMADA"
    assert row.fecha_inicio == BASE
    assert row.fecha_fin is None
    assert row.daily_sala_id == record.room_name
    assert row.adulto_contacto_id == contact_id
    assert row.iniciado_por_id == adult_id
    assert row.creado_en == BASE
    assert row.actualizado_en == BASE
    with sql_db.transaction() as conn:
        idempotency = conn.execute(
            text(
                "SELECT usuario_id, operacion, estado, length(clave_hash) AS clave, "
                "length(hash_solicitud) AS hash, expira_en - creado_en AS vigencia, "
                "completado_en - creado_en AS completado "
                f"FROM {T_IDEMPOTENCIA_OPERACION} WHERE id_idempotencia = :id"
            ),
            {"id": row.id_idempotencia},
        ).one()
    assert idempotency.usuario_id == adult_id
    assert idempotency.operacion == "CREAR_COMUNICACION"
    assert idempotency.estado == "COMPLETADA"
    assert idempotency.clave == 32
    assert idempotency.hash == 64
    assert idempotency.vigencia == timedelta(hours=24)
    assert idempotency.completado == timedelta(0)
    assert _history(sql_db, saved.id) == [("INICIADA", adult_id, BASE)]
    # INICIADA se lee como no_contestada.
    assert calls.get(uid, saved.id).status == CallStatus.NO_CONTESTADA


@pytest.mark.parametrize(
    ("audio_only", "tipo"), [(True, "LLAMADA_AUDIO"), (False, "VIDEOLLAMADA")]
)
def test_audio_only_ida_y_vuelta(calls, adult, sql_contact, sql_db, audio_only, tipo):
    uid, adult_id = adult
    saved = calls.add(_record(uid, sql_contact(adult_id), audio_only=audio_only))

    assert _row(sql_db, saved.id).tipo_comunicacion == tipo
    assert calls.get(uid, saved.id).audio_only is audio_only
    assert [r.audio_only for r in calls.list_for_adult(uid) if r.id == saved.id] == [
        audio_only
    ]


def test_add_con_contacto_de_otro_adulto_lanza_sin_escribir(
    calls, adult, sql_user, sql_contact, sql_db
):
    uid, _ = adult
    _, other_adult_id = sql_user("Otro", "Adulto")
    foreign_contact = sql_contact(other_adult_id)

    with pytest.raises(ValueError):
        calls.add(_record(uid, foreign_contact))

    assert _count_for_contact(sql_db, foreign_contact) == 0
    # El SAVEPOINT se revirtió y la conexión sigue utilizable.
    assert calls.list_for_adult(uid) == []


# --- update_status ---


def test_update_status_completada_fija_fecha_fin_e_historial(
    calls, adult, sql_contact, sql_db
):
    uid, adult_id = adult
    saved = calls.add(_record(uid, sql_contact(adult_id)))

    updated = calls.update_status(uid, saved.id, CallStatus.COMPLETADA)

    assert updated.status == CallStatus.COMPLETADA
    # Registro releído: room_url reconstruida y expiración = inicio + 1 h.
    assert updated == calls.get(uid, saved.id)
    assert updated.room_url == f"https://acme.daily.co/{saved.room_name}"
    assert updated.room_expires_at == BASE + ONE_HOUR
    row = _row(sql_db, saved.id)
    assert row.estado == "FINALIZADA"
    assert row.fecha_fin is not None
    assert row.fecha_fin >= row.fecha_inicio
    assert row.actualizado_en >= row.creado_en
    history = _history(sql_db, saved.id)
    assert [(state, by) for state, by, _ in history] == [
        ("INICIADA", adult_id),
        ("FINALIZADA", adult_id),
    ]
    assert history[1][2] == row.fecha_fin


@pytest.mark.parametrize(
    ("status", "db_state"),
    [
        (CallStatus.CANCELADA, "CANCELADA"),
        (CallStatus.NO_CONTESTADA, "NO_ATENDIDA"),
    ],
)
def test_update_status_cancelada_y_no_contestada_tambien_fijan_fecha_fin(
    calls, adult, sql_contact, sql_db, status, db_state
):
    uid, adult_id = adult
    saved = calls.add(_record(uid, sql_contact(adult_id)))

    updated = calls.update_status(uid, saved.id, status)

    assert updated.status == status
    assert calls.get(uid, saved.id).status == status
    row = _row(sql_db, saved.id)
    assert row.estado == db_state
    assert row.fecha_fin is not None
    assert row.fecha_fin >= row.fecha_inicio
    assert [state for state, _, _ in _history(sql_db, saved.id)] == ["INICIADA", db_state]


def test_llamada_de_otro_adulto_es_none_y_no_cambia(
    calls, adult, sql_user, sql_contact, sql_db
):
    uid, adult_id = adult
    other_uid, _ = sql_user("Otro", "Adulto")
    saved = calls.add(_record(uid, sql_contact(adult_id)))

    assert calls.get(other_uid, saved.id) is None
    assert calls.update_status(other_uid, saved.id, CallStatus.COMPLETADA) is None
    assert calls.update_status("uid-que-no-existe", saved.id, CallStatus.COMPLETADA) is None
    assert [r for r in calls.list_for_adult(other_uid) if r.id == saved.id] == []
    row = _row(sql_db, saved.id)
    assert row.estado == "INICIADA"
    assert row.fecha_fin is None
    assert len(_history(sql_db, saved.id)) == 1


def test_id_de_un_mensaje_de_voz_es_none_en_get_y_update_status(
    calls, adult, sql_contact, sql_db
):
    uid, adult_id = adult
    contact_id = sql_contact(adult_id)
    voice = SqlVoiceMessageRepository(sql_db).add(_voice_record(uid, contact_id))

    assert calls.get(uid, voice.id) is None
    assert calls.update_status(uid, voice.id, CallStatus.CANCELADA) is None
    row = _row(sql_db, voice.id)
    assert row.tipo_comunicacion == "MENSAJE_VOZ"
    assert row.estado == "FINALIZADA"
    assert _history(sql_db, voice.id) == []


# --- list_for_adult ---


def test_list_excluye_voz_ordena_desc_con_empate_por_id_asc_e_incluye_contacto_inactivo(
    calls, adult, sql_contact, sql_db
):
    uid, adult_id = adult
    contact_id = sql_contact(adult_id)
    inactive_contact = sql_contact(adult_id, first_name="Baja", estado="INACTIVO")
    first = calls.add(_record(uid, contact_id, started_at=BASE))
    tie_a = calls.add(_record(uid, contact_id, started_at=BASE + timedelta(minutes=10)))
    tie_b = calls.add(
        _record(uid, contact_id, started_at=BASE + timedelta(minutes=10), audio_only=True)
    )
    from_inactive = calls.add(
        _record(uid, inactive_contact, started_at=BASE + timedelta(minutes=20))
    )
    voice = SqlVoiceMessageRepository(sql_db).add(_voice_record(uid, contact_id))
    created = {first.id, tie_a.id, tie_b.id, from_inactive.id, voice.id}

    listed = [record for record in calls.list_for_adult(uid) if record.id in created]

    assert int(tie_a.id) < int(tie_b.id)
    assert [record.id for record in listed] == [
        from_inactive.id,
        tie_a.id,
        tie_b.id,
        first.id,
    ]
    assert listed[0].contact_id == str(inactive_contact)
    assert all(record.adult_uid == uid for record in listed)
    assert [record.room_expires_at for record in listed] == [
        record.started_at + ONE_HOUR for record in listed
    ]


def test_list_de_otro_adulto_no_incluye_las_llamadas(calls, adult, sql_user, sql_contact):
    uid, adult_id = adult
    other_uid, _ = sql_user("Otro", "Adulto")
    saved = calls.add(_record(uid, sql_contact(adult_id)))

    assert [r.id for r in calls.list_for_adult(uid) if r.id == saved.id] == [saved.id]
    assert [r for r in calls.list_for_adult(other_uid) if r.id == saved.id] == []
    assert calls.list_for_adult("uid-que-no-existe") == []
    assert calls.list_for_adult("") == []


@pytest.mark.parametrize("bad_id", ["abc", "", "-1", "1e3", "²", " 12", str(2**63)])
def test_id_no_numerico_es_none(calls, adult, sql_contact, bad_id):
    uid, adult_id = adult
    calls.add(_record(uid, sql_contact(adult_id)))

    assert calls.get(uid, bad_id) is None
    assert calls.update_status(uid, bad_id, CallStatus.COMPLETADA) is None


def test_id_numerico_inexistente_es_none(calls, adult):
    uid, _ = adult

    assert calls.get(uid, str(2**62)) is None
    assert calls.update_status(uid, str(2**62), CallStatus.COMPLETADA) is None


# --- proveedor (D-R3-2) ---


def test_proveedor_elige_sql_solo_con_postgres(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(call_repository, "_sql_call_repository", lambda: sentinel)

    monkeypatch.setattr(db_session, "sql_enabled", lambda: True)
    assert call_repository.get_call_repository() is sentinel

    monkeypatch.setattr(db_session, "sql_enabled", lambda: False)
    assert (
        call_repository.get_call_repository()
        is call_repository._default_call_repository
    )
