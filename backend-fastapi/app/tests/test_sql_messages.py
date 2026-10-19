"""Integración de SqlMessageRepository con PostgreSQL (ronda 3, D-R3-12, D-R3-26a).

Solo con CS_SQL_TESTS=1: todo corre dentro de la transacción revertida de la fixture
sql_db, así que nada queda escrito. La base es compartida: cada test crea sus propios
usuarios y contactos y filtra por esos ids. Nunca ejecutar con -l ni --showlocals.
"""
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db.mapping import T_MENSAJE_TEXTO, T_USUARIO, id_str
from app.models.messages import MessageRecord
from app.repositories.errors import RecipientNotLinkedError
from app.repositories.message_repository import SqlMessageRepository


pytestmark = pytest.mark.skipif(
    os.environ.get("CS_SQL_TESTS") != "1", reason="sin CS_SQL_TESTS=1"
)

# Lejos de "ahora": prueba que enviado_en sale del registro y no del default de la base.
T0 = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
BOGOTA = timezone(timedelta(hours=-5))


def _email() -> str:
    return f"contacto-{uuid.uuid4()}@example.invalid"


def _record(
    adult_uid: str,
    contact_id: str,
    *,
    from_adult: bool,
    created_at: datetime = T0,
    text_value: str = "Hola",
) -> MessageRecord:
    return MessageRecord(
        id=str(uuid.uuid4()),
        adult_uid=adult_uid,
        contact_id=contact_id,
        text=text_value,
        created_at=created_at,
        from_adult=from_adult,
    )


def _stored_row(sql_db, message_id: str):
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT adulto_contacto_id, emisor_usuario_id, destinatario_usuario_id, "
                f"contenido, enviado_en FROM {T_MENSAJE_TEXTO} "
                "WHERE id_mensaje_texto = :id"
            ),
            {"id": int(message_id)},
        ).one()


def _count_for_contact(sql_db, adulto_contacto_id: int) -> int:
    with sql_db.transaction() as conn:
        return conn.execute(
            text(f"SELECT count(*) FROM {T_MENSAJE_TEXTO} WHERE adulto_contacto_id = :id"),
            {"id": adulto_contacto_id},
        ).scalar_one()


@pytest.fixture
def repo(sql_db) -> SqlMessageRepository:
    return SqlMessageRepository(sql_db)


@pytest.fixture
def pair(sql_user, sql_contact):
    """Adulto y un contacto suyo cuyo correo es el de otro usuario de la plataforma.

    El contacto guarda el correo con mayúsculas y espacios: se compara normalizado.
    """
    adult_uid, adult_id = sql_user(first_name="Rosa", last_name="Gómez")
    email = _email()
    contact_uid, contact_user_id = sql_user(first_name="Ana", last_name="Pérez", email=email)
    adulto_contacto_id = sql_contact(adult_id, email=f"  {email.upper()} ")
    return {
        "adult_uid": adult_uid,
        "adult_id": adult_id,
        "contact_uid": contact_uid,
        "contact_user_id": contact_user_id,
        "ac_id": adulto_contacto_id,
        "contact_id": id_str(adulto_contacto_id),
    }


def test_add_adult_to_contact_sender_adult_recipient_contact_user(sql_db, repo, pair):
    record = _record(pair["adult_uid"], pair["contact_id"], from_adult=True)

    saved = repo.add(record)

    assert saved.id.isascii() and saved.id.isdigit()
    assert saved == replace(record, id=saved.id)
    row = _stored_row(sql_db, saved.id)
    assert row.adulto_contacto_id == pair["ac_id"]
    assert row.emisor_usuario_id == pair["adult_id"]
    assert row.destinatario_usuario_id == pair["contact_user_id"]
    assert row.contenido == "Hola"
    listed = repo.list_conversation(pair["adult_uid"], pair["contact_id"])
    assert listed == [saved]
    assert listed[0].from_adult is True


def test_add_contact_to_adult_reverses_sender_and_reads_from_adult_false(
    sql_db, repo, pair
):
    record = _record(pair["adult_uid"], pair["contact_id"], from_adult=False)

    saved = repo.add(record)

    assert saved == replace(record, id=saved.id)
    row = _stored_row(sql_db, saved.id)
    assert row.emisor_usuario_id == pair["contact_user_id"]
    assert row.destinatario_usuario_id == pair["adult_id"]
    listed = repo.list_conversation(pair["adult_uid"], pair["contact_id"])
    assert [(item.id, item.from_adult) for item in listed] == [(saved.id, False)]


def test_add_stores_created_at_exactly_and_reads_it_in_utc(sql_db, repo, pair):
    created_at = datetime(2001, 2, 3, 8, 30, 15, tzinfo=BOGOTA)

    saved = repo.add(
        _record(pair["adult_uid"], pair["contact_id"], from_adult=True, created_at=created_at)
    )

    assert saved.created_at == created_at
    assert saved.created_at.utcoffset() == timedelta(hours=-5)
    assert _stored_row(sql_db, saved.id).enviado_en == created_at
    listed = repo.list_conversation(pair["adult_uid"], pair["contact_id"])
    assert listed[0].created_at == created_at
    assert listed[0].created_at.utcoffset() == timedelta(0)


def test_list_conversation_ascending_with_tie_broken_by_id(repo, pair):
    adult_uid, contact_id = pair["adult_uid"], pair["contact_id"]
    late = repo.add(
        _record(adult_uid, contact_id, from_adult=False, created_at=T0 + timedelta(minutes=2))
    )
    first = repo.add(_record(adult_uid, contact_id, from_adult=True, created_at=T0))
    tie_a = repo.add(
        _record(adult_uid, contact_id, from_adult=False, created_at=T0 + timedelta(minutes=1))
    )
    tie_b = repo.add(
        _record(adult_uid, contact_id, from_adult=True, created_at=T0 + timedelta(minutes=1))
    )
    assert int(tie_a.id) < int(tie_b.id)

    listed = repo.list_conversation(adult_uid, contact_id)

    assert [(item.id, item.from_adult) for item in listed] == [
        (first.id, True),
        (tie_a.id, False),
        (tie_b.id, True),
        (late.id, False),
    ]
    assert all(item.adult_uid == adult_uid for item in listed)
    assert all(item.contact_id == contact_id for item in listed)


def test_list_conversation_empty(repo, pair):
    assert repo.list_conversation(pair["adult_uid"], pair["contact_id"]) == []


def _adult_and_contact_without_other_user(sql_user, sql_contact, kind: str):
    """Adulto y un contacto suyo sin usuario de la plataforma distinto del adulto.

    kind "none": contacto sin correo; "unknown": correo que no tiene ningún usuario;
    "adult_own": el correo del propio adulto (con mayúsculas).
    """
    adult_email = _email()
    adult_uid, adult_id = sql_user(email=adult_email)
    contact_email = {
        "none": None,
        "unknown": _email(),
        "adult_own": adult_email.upper(),
    }[kind]
    return adult_uid, adult_id, sql_contact(adult_id, email=contact_email)


@pytest.mark.parametrize("kind", ["none", "unknown", "adult_own"])
def test_add_from_adult_contact_without_other_user_stores_null_recipient(
    sql_db, repo, sql_user, sql_contact, kind
):
    # D-R3-26a (V004): destinatario_usuario_id admite NULL; el adulto escribe a cualquier
    # contacto con permiso de mensajes, igual que con InMemory.
    adult_uid, adult_id, adulto_contacto_id = _adult_and_contact_without_other_user(
        sql_user, sql_contact, kind
    )
    record = _record(adult_uid, id_str(adulto_contacto_id), from_adult=True)

    saved = repo.add(record)

    assert saved == replace(record, id=saved.id)
    row = _stored_row(sql_db, saved.id)
    assert row.adulto_contacto_id == adulto_contacto_id
    assert row.emisor_usuario_id == adult_id
    assert row.destinatario_usuario_id is None
    assert row.contenido == "Hola"
    assert row.enviado_en == T0
    assert _count_for_contact(sql_db, adulto_contacto_id) == 1
    listed = repo.list_conversation(adult_uid, id_str(adulto_contacto_id))
    assert listed == [saved]
    assert listed[0].from_adult is True


@pytest.mark.parametrize("kind", ["none", "unknown", "adult_own"])
def test_add_from_contact_without_resolvable_sender_raises_and_inserts_nothing(
    sql_db, repo, sql_user, sql_contact, kind
):
    # emisor_usuario_id sigue siendo obligatorio: sin usuario del contacto distinto del
    # adulto no se puede guardar un mensaje del contacto.
    adult_uid, _, adulto_contacto_id = _adult_and_contact_without_other_user(
        sql_user, sql_contact, kind
    )

    with pytest.raises(RecipientNotLinkedError):
        repo.add(_record(adult_uid, id_str(adulto_contacto_id), from_adult=False))

    assert _count_for_contact(sql_db, adulto_contacto_id) == 0
    assert repo.list_conversation(adult_uid, id_str(adulto_contacto_id)) == []


def test_null_recipient_message_listed_with_later_reply_of_new_user(
    sql_db, repo, sql_user, sql_contact
):
    # El adulto escribe antes de que el contacto tenga cuenta; cuando la crea y responde,
    # la conversación muestra ambos mensajes y el primero conserva el destinatario NULL.
    adult_uid, adult_id = sql_user()
    email = _email()
    adulto_contacto_id = sql_contact(adult_id, email=email)
    contact_id = id_str(adulto_contacto_id)
    first = repo.add(_record(adult_uid, contact_id, from_adult=True, created_at=T0))
    _, contact_user_id = sql_user(email=email)

    reply = repo.add(
        _record(adult_uid, contact_id, from_adult=False, created_at=T0 + timedelta(minutes=1))
    )

    assert _stored_row(sql_db, first.id).destinatario_usuario_id is None
    reply_row = _stored_row(sql_db, reply.id)
    assert reply_row.emisor_usuario_id == contact_user_id
    assert reply_row.destinatario_usuario_id == adult_id
    listed = repo.list_conversation(adult_uid, contact_id)
    assert [(item.id, item.from_adult) for item in listed] == [
        (first.id, True),
        (reply.id, False),
    ]


def test_contact_of_other_adult_lists_empty_and_add_raises(
    sql_db, repo, pair, sql_user, sql_contact
):
    other_uid, other_id = sql_user()
    email = _email()
    sql_user(email=email)
    other_contact = sql_contact(other_id, email=email)
    other_message = repo.add(_record(other_uid, id_str(other_contact), from_adult=True))

    assert repo.list_conversation(pair["adult_uid"], id_str(other_contact)) == []
    assert [item.id for item in repo.list_conversation(other_uid, id_str(other_contact))] == [
        other_message.id
    ]
    with pytest.raises(RecipientNotLinkedError):
        repo.add(_record(pair["adult_uid"], id_str(other_contact), from_adult=True))
    assert _count_for_contact(sql_db, other_contact) == 1


def test_add_unknown_adult_raises_and_leaves_nothing_written(sql_db, repo, pair):
    # ensure_user_id crea la fila mínima del adulto, pero el error revierte todo.
    unknown_uid = f"test-{uuid.uuid4()}"

    with pytest.raises(RecipientNotLinkedError):
        repo.add(_record(unknown_uid, pair["contact_id"], from_adult=True))

    with sql_db.transaction() as conn:
        users_with_uid = conn.execute(
            text(f"SELECT count(*) FROM {T_USUARIO} WHERE firebase_uid = :uid"),
            {"uid": unknown_uid},
        ).scalar_one()
    assert users_with_uid == 0
    assert _count_for_contact(sql_db, pair["ac_id"]) == 0


@pytest.mark.parametrize(
    "bad_id",
    ["abc", "", "-1", "1e3", "²", " 12", str(2**63)],
    ids=["letters", "empty", "negative", "exponent", "superscript", "space", "too_big"],
)
def test_non_numeric_contact_id_lists_empty_and_add_raises(repo, pair, bad_id):
    assert repo.list_conversation(pair["adult_uid"], bad_id) == []
    with pytest.raises(RecipientNotLinkedError):
        repo.add(_record(pair["adult_uid"], bad_id, from_adult=True))


def test_list_conversation_unknown_adult_returns_empty(repo, pair):
    repo.add(_record(pair["adult_uid"], pair["contact_id"], from_adult=True))

    assert repo.list_conversation(f"test-{uuid.uuid4()}", pair["contact_id"]) == []
    assert repo.list_conversation("", pair["contact_id"]) == []
