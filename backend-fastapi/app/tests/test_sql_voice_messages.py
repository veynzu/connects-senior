"""Integración de SqlVoiceMessageRepository contra PostgreSQL (plan ronda 3, D-R3-13, 5.4).

Solo con CS_SQL_TESTS=1. Todo corre dentro de la transacción revertida de ``sql_db``:
nada queda escrito en la base. Los asserts sobre listas se filtran por los ids creados
en cada test porque la base es compartida. Nunca ejecutar con -l ni --showlocals.
"""

import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.db import session as db_session
from app.db.mapping import (
    T_ADULTO_CONTACTO,
    T_COMUNICACION,
    T_IDEMPOTENCIA_OPERACION,
    T_MENSAJE_VOZ,
    T_USUARIO,
)
from app.models.calls import CallRecord
from app.models.enums import CallStatus
from app.models.voice_messages import VoiceMessageRecord
from app.repositories import voice_message_repository
from app.repositories.call_repository import SqlCallRepository
from app.repositories.errors import RecipientNotLinkedError
from app.repositories.voice_message_repository import SqlVoiceMessageRepository


pytestmark = pytest.mark.skipif(
    os.environ.get("CS_SQL_TESTS") != "1", reason="sin CS_SQL_TESTS=1"
)

BASE = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)
ADULT_NAME = "Ana Pérez"
CONTACT_NAME = "Carlitos Hijo"


@pytest.fixture
def voices(sql_db):
    return SqlVoiceMessageRepository(sql_db)


@pytest.fixture
def world(sql_user, sql_contact):
    """Adulto con un contacto cuyo correo es el de otro usuario de la plataforma."""
    adult_uid, adult_id = sql_user("Ana", "Pérez")
    email = f"carlos-{uuid.uuid4().hex}@example.invalid"
    contact_uid, contact_user_id = sql_user("Carlos", "Ruiz", email=email, verified=True)
    # El adulto guardó al contacto con otro nombre: from_name usa el de adulto_contacto.
    contact_id = sql_contact(adult_id, first_name="Carlitos", last_name="Hijo", email=email)
    return SimpleNamespace(
        adult_uid=adult_uid,
        adult_id=adult_id,
        contact_uid=contact_uid,
        contact_user_id=contact_user_id,
        contact_id=str(contact_id),
    )


def _record(
    adult_uid: str,
    contact_id: int | str,
    *,
    from_adult: bool,
    created_at: datetime = BASE,
) -> VoiceMessageRecord:
    return VoiceMessageRecord(
        id=str(uuid.uuid4()),
        adult_uid=adult_uid,
        contact_id=str(contact_id),
        from_adult=from_adult,
        # Distinto del derivado al leer: prueba que add lo devuelve tal cual.
        from_name="Nombre recibido",
        file_name=f"{uuid.uuid4()}",
        content_type="audio/webm",
        size_bytes=1234,
        created_at=created_at,
    )


def _communication(sql_db, voice_id: str):
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT tipo_comunicacion, estado, adulto_contacto_id, iniciado_por_id, "
                "id_idempotencia, fecha_inicio, fecha_fin, daily_sala_id, creado_en, "
                f"actualizado_en FROM {T_COMUNICACION} WHERE id_comunicacion = :id"
            ),
            {"id": int(voice_id)},
        ).one()


def _count_for_contact(sql_db, contact_id: int | str) -> int:
    with sql_db.transaction() as conn:
        return conn.execute(
            text(f"SELECT count(*) FROM {T_COMUNICACION} WHERE adulto_contacto_id = :id"),
            {"id": int(contact_id)},
        ).scalar_one()


def _ids(records, created) -> list[str]:
    return [record.id for record in records if record.id in created]


# --- add ---


def test_add_del_adulto_crea_idempotencia_comunicacion_y_mensaje_voz(voices, world, sql_db):
    record = _record(world.adult_uid, world.contact_id, from_adult=True)

    saved = voices.add(record)

    assert saved.id.isdigit()
    assert saved == replace(record, id=saved.id)
    row = _communication(sql_db, saved.id)
    assert row.tipo_comunicacion == "MENSAJE_VOZ"
    assert row.estado == "FINALIZADA"
    assert row.adulto_contacto_id == int(world.contact_id)
    assert row.iniciado_por_id == world.adult_id
    assert row.fecha_inicio == BASE
    assert row.fecha_fin == BASE
    assert row.creado_en == BASE
    assert row.actualizado_en == BASE
    assert row.daily_sala_id is None
    with sql_db.transaction() as conn:
        voice = conn.execute(
            text(
                "SELECT referencia_archivo, duracion_segundos, tipo_mime, tamano_bytes, "
                f"creado_en FROM {T_MENSAJE_VOZ} WHERE comunicacion_id = :id"
            ),
            {"id": int(saved.id)},
        ).one()
        idempotency = conn.execute(
            text(
                "SELECT usuario_id, operacion, estado, length(clave_hash) AS clave, "
                "length(hash_solicitud) AS hash, expira_en - creado_en AS vigencia "
                f"FROM {T_IDEMPOTENCIA_OPERACION} WHERE id_idempotencia = :id"
            ),
            {"id": row.id_idempotencia},
        ).one()
    assert tuple(voice) == (record.file_name, None, "audio/webm", 1234, BASE)
    assert idempotency.usuario_id == world.adult_id
    assert idempotency.operacion == "CREAR_COMUNICACION"
    assert idempotency.estado == "COMPLETADA"
    assert idempotency.clave == 32
    assert idempotency.hash == 64
    assert idempotency.vigencia == timedelta(hours=24)


def test_add_del_contacto_usa_al_usuario_del_contacto_como_emisor(voices, world, sql_db):
    saved = voices.add(_record(world.adult_uid, world.contact_id, from_adult=False))

    row = _communication(sql_db, saved.id)
    assert row.iniciado_por_id == world.contact_user_id
    with sql_db.transaction() as conn:
        owner = conn.execute(
            text(
                f"SELECT usuario_id FROM {T_IDEMPOTENCIA_OPERACION} "
                "WHERE id_idempotencia = :id"
            ),
            {"id": row.id_idempotencia},
        ).scalar_one()
    assert owner == world.contact_user_id


def test_contacto_sin_correo_y_desde_el_adulto_se_guarda(voices, world, sql_contact, sql_db):
    no_account = sql_contact(world.adult_id, first_name="Sin", last_name="Cuenta")

    saved = voices.add(_record(world.adult_uid, no_account, from_adult=True))

    assert _communication(sql_db, saved.id).iniciado_por_id == world.adult_id
    assert _ids(voices.list_sent_to_contact(world.adult_uid, str(no_account)), {saved.id}) == [
        saved.id
    ]


@pytest.mark.parametrize("email", [None, "nadie-registrado@example.invalid"])
def test_contacto_sin_usuario_y_desde_el_contacto_lanza_sin_escribir(
    voices, world, sql_contact, sql_db, email
):
    if email is not None:
        email = email.replace("@", f"-{uuid.uuid4().hex}@")
    no_account = sql_contact(world.adult_id, first_name="Sin", last_name="Cuenta", email=email)

    with pytest.raises(RecipientNotLinkedError):
        voices.add(_record(world.adult_uid, no_account, from_adult=False))

    assert _count_for_contact(sql_db, no_account) == 0
    # La conexión sigue utilizable después del error.
    assert voices.list_received_by_adult(world.adult_uid) == []


def test_contacto_con_el_correo_del_propio_adulto_lanza_sin_escribir(
    voices, sql_user, sql_contact, sql_db
):
    email = f"ana-{uuid.uuid4().hex}@example.invalid"
    adult_uid, adult_id = sql_user("Ana", "Pérez", email=email, verified=True)
    # Mismo correo con mayúsculas y espacios: la comparación es lower(btrim()).
    self_contact = sql_contact(adult_id, email=f"  {email.upper()} ")

    with pytest.raises(RecipientNotLinkedError):
        voices.add(_record(adult_uid, self_contact, from_adult=False))

    assert _count_for_contact(sql_db, self_contact) == 0


def test_add_con_contacto_de_otro_adulto_lanza_sin_escribir(
    voices, world, sql_user, sql_contact, sql_db
):
    _, other_adult_id = sql_user("Otro", "Adulto")
    foreign_contact = sql_contact(other_adult_id)

    with pytest.raises(ValueError):
        voices.add(_record(world.adult_uid, foreign_contact, from_adult=True))

    assert _count_for_contact(sql_db, foreign_contact) == 0


# --- get ---


def test_get_deriva_from_name_en_ambos_sentidos(voices, world):
    from_adult = voices.add(_record(world.adult_uid, world.contact_id, from_adult=True))
    from_contact = voices.add(_record(world.adult_uid, world.contact_id, from_adult=False))

    assert voices.get(from_adult.id) == replace(from_adult, from_name=ADULT_NAME)
    assert voices.get(from_contact.id) == replace(from_contact, from_name=CONTACT_NAME)


def test_from_name_del_adulto_es_su_nombre_actual(voices, world, sql_db):
    saved = voices.add(_record(world.adult_uid, world.contact_id, from_adult=True))
    with sql_db.transaction() as conn:
        conn.execute(
            text(f"UPDATE {T_USUARIO} SET nombre = 'Anita' WHERE id_usuario = :id"),
            {"id": world.adult_id},
        )

    assert voices.get(saved.id).from_name == "Anita Pérez"


def test_get_con_id_de_una_llamada_es_none(voices, world, sql_db):
    call = SqlCallRepository(sql_db, "acme").add(
        CallRecord(
            id=str(uuid.uuid4()),
            adult_uid=world.adult_uid,
            contact_id=world.contact_id,
            started_at=BASE,
            status=CallStatus.NO_CONTESTADA,
            audio_only=True,
            room_name="sala-prueba",
            room_url="https://acme.daily.co/sala-prueba",
            room_expires_at=BASE + timedelta(hours=1),
        )
    )

    assert voices.get(call.id) is None
    assert _ids(voices.list_received_by_adult(world.adult_uid), {call.id}) == []
    assert _ids(voices.list_sent_to_contact(world.adult_uid, world.contact_id), {call.id}) == []


@pytest.mark.parametrize("bad_id", ["abc", "", "-1", "1e3", "²", " 12", str(2**63)])
def test_id_no_numerico_es_none_o_lista_vacia(voices, world, bad_id):
    voices.add(_record(world.adult_uid, world.contact_id, from_adult=True))

    assert voices.get(bad_id) is None
    assert voices.list_sent_to_contact(world.adult_uid, bad_id) == []


def test_id_numerico_inexistente_es_none(voices):
    assert voices.get(str(2**62)) is None


# --- listas ---


def test_list_received_by_adult_solo_los_del_contacto(voices, world):
    sent = voices.add(_record(world.adult_uid, world.contact_id, from_adult=True))
    received_old = voices.add(
        _record(world.adult_uid, world.contact_id, from_adult=False, created_at=BASE)
    )
    received_new = voices.add(
        _record(
            world.adult_uid,
            world.contact_id,
            from_adult=False,
            created_at=BASE + timedelta(minutes=5),
        )
    )
    created = {sent.id, received_old.id, received_new.id}

    listed = [
        record
        for record in voices.list_received_by_adult(world.adult_uid)
        if record.id in created
    ]

    assert [record.id for record in listed] == [received_new.id, received_old.id]
    assert all(not record.from_adult for record in listed)
    assert all(record.from_name == CONTACT_NAME for record in listed)
    assert all(record.adult_uid == world.adult_uid for record in listed)
    assert all(record.contact_id == world.contact_id for record in listed)


def test_list_sent_to_contact_solo_los_del_adulto_a_ese_contacto(voices, world, sql_contact):
    other_contact = str(sql_contact(world.adult_id, first_name="Otro"))
    sent_old = voices.add(
        _record(world.adult_uid, world.contact_id, from_adult=True, created_at=BASE)
    )
    sent_new = voices.add(
        _record(
            world.adult_uid,
            world.contact_id,
            from_adult=True,
            created_at=BASE + timedelta(minutes=5),
        )
    )
    to_other = voices.add(_record(world.adult_uid, other_contact, from_adult=True))
    received = voices.add(_record(world.adult_uid, world.contact_id, from_adult=False))
    created = {sent_old.id, sent_new.id, to_other.id, received.id}

    listed = [
        record
        for record in voices.list_sent_to_contact(world.adult_uid, world.contact_id)
        if record.id in created
    ]

    assert [record.id for record in listed] == [sent_new.id, sent_old.id]
    assert all(record.from_adult for record in listed)
    assert all(record.from_name == ADULT_NAME for record in listed)
    assert _ids(voices.list_sent_to_contact(world.adult_uid, other_contact), created) == [
        to_other.id
    ]


def test_orden_desc_con_empate_por_id_desc(voices, world):
    later = BASE + timedelta(minutes=5)
    received = [
        voices.add(_record(world.adult_uid, world.contact_id, from_adult=False, created_at=at))
        for at in (BASE, later, later)
    ]
    sent = [
        voices.add(_record(world.adult_uid, world.contact_id, from_adult=True, created_at=at))
        for at in (BASE, later, later)
    ]

    assert int(received[1].id) < int(received[2].id)
    assert _ids(
        voices.list_received_by_adult(world.adult_uid), {r.id for r in received}
    ) == [received[2].id, received[1].id, received[0].id]
    assert _ids(
        voices.list_sent_to_contact(world.adult_uid, world.contact_id), {r.id for r in sent}
    ) == [sent[2].id, sent[1].id, sent[0].id]


def test_listas_incluyen_contactos_dados_de_baja(voices, world, sql_db):
    received = voices.add(_record(world.adult_uid, world.contact_id, from_adult=False))
    sent = voices.add(_record(world.adult_uid, world.contact_id, from_adult=True))
    with sql_db.transaction() as conn:
        conn.execute(
            text(
                f"UPDATE {T_ADULTO_CONTACTO} SET estado = 'INACTIVO' "
                "WHERE id_adulto_contacto = :id"
            ),
            {"id": int(world.contact_id)},
        )

    assert _ids(voices.list_received_by_adult(world.adult_uid), {received.id}) == [received.id]
    assert _ids(voices.list_sent_to_contact(world.adult_uid, world.contact_id), {sent.id}) == [
        sent.id
    ]


def test_listas_de_otro_adulto_no_incluyen_los_mensajes(voices, world, sql_user):
    other_uid, _ = sql_user("Otro", "Adulto")
    received = voices.add(_record(world.adult_uid, world.contact_id, from_adult=False))
    sent = voices.add(_record(world.adult_uid, world.contact_id, from_adult=True))
    created = {received.id, sent.id}

    assert _ids(voices.list_received_by_adult(other_uid), created) == []
    assert _ids(voices.list_sent_to_contact(other_uid, world.contact_id), created) == []
    # El usuario del contacto tampoco es "el adulto" de esos mensajes.
    assert _ids(voices.list_received_by_adult(world.contact_uid), created) == []
    assert voices.list_received_by_adult("uid-que-no-existe") == []
    assert voices.list_received_by_adult("") == []


# --- proveedor (D-R3-2) ---


def test_proveedor_elige_sql_solo_con_postgres(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        voice_message_repository, "_sql_voice_message_repository", lambda: sentinel
    )

    monkeypatch.setattr(db_session, "sql_enabled", lambda: True)
    assert voice_message_repository.get_voice_message_repository() is sentinel

    monkeypatch.setattr(db_session, "sql_enabled", lambda: False)
    assert (
        voice_message_repository.get_voice_message_repository()
        is voice_message_repository._default_voice_message_repository
    )
