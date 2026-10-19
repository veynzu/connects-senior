"""SqlUserRepository contra PostgreSQL (plan ronda 3, T-users, 5.2).

Solo con CS_SQL_TESTS=1: cada test corre dentro de la transacción revertida de la fixture
sql_db, así que nada queda escrito. La base es compartida: los uid y correos de cada test
son únicos y los asserts sobre listas se limitan a los datos del propio test. Nunca
ejecutar con -l ni --showlocals.
"""

import os
import uuid
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import text

from app.db import session as db_session
from app.db.mapping import T_ROL, T_USUARIO, T_USUARIO_ROL, T_VINCULO_USUARIO
from app.models.enums import Role
from app.models.users import (
    NO_CAREGIVER_PERMISSIONS,
    AdultLink,
    CaregiverPermissions,
    UserProfile,
)
from app.repositories import user_repository
from app.repositories.user_repository import SqlUserRepository, UserRepository


pytestmark = pytest.mark.skipif(
    os.environ.get("CS_SQL_TESTS") != "1", reason="sin CS_SQL_TESTS=1"
)

NO_PERMISSIONS = CaregiverPermissions(
    manage_contacts=False, manage_reminders=False, receive_alerts=False
)


def _uid() -> str:
    return f"test-{uuid.uuid4()}"


def _email() -> str:
    return f"test-{uuid.uuid4()}@example.invalid"


def _profile(uid: str | None = None, **overrides) -> UserProfile:
    values: dict[str, object] = {
        "uid": uid or _uid(),
        "first_name": "Ana",
        "last_name": "Pérez",
        "email": _email(),
        "role": Role.FAMILIAR,
    }
    values.update(overrides)
    return UserProfile(**values)


def _permissions(
    manage_contacts: bool = True,
    manage_reminders: bool = True,
    receive_alerts: bool = True,
) -> CaregiverPermissions:
    return CaregiverPermissions(
        manage_contacts=manage_contacts,
        manage_reminders=manage_reminders,
        receive_alerts=receive_alerts,
    )


@pytest.fixture
def repo(sql_db) -> SqlUserRepository:
    return SqlUserRepository(sql_db)


def _user_row(sql_db, uid: str):
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT id_usuario, nombre, apellido, correo, correo_verificado, "
                "telefono, direccion, creado_en, actualizado_en "
                f"FROM {T_USUARIO} WHERE firebase_uid = :uid"
            ),
            {"uid": uid},
        ).one_or_none()


def _role_rows(sql_db, uid: str) -> dict[str, bool]:
    with sql_db.transaction() as conn:
        rows = conn.execute(
            text(
                "SELECT r.codigo, ur.activo "
                f"FROM {T_USUARIO_ROL} ur "
                f"JOIN {T_ROL} r ON r.id_rol = ur.id_rol "
                f"JOIN {T_USUARIO} u ON u.id_usuario = ur.id_usuario "
                "WHERE u.firebase_uid = :uid"
            ),
            {"uid": uid},
        ).all()
    return {row.codigo: row.activo for row in rows}


def _role_catalog(sql_db) -> dict[str, str]:
    with sql_db.transaction() as conn:
        rows = conn.execute(text(f"SELECT codigo, nombre FROM {T_ROL}")).all()
    return {row.codigo: row.nombre for row in rows}


def _link_rows(sql_db, adult_uid: str) -> list:
    with sql_db.transaction() as conn:
        return conn.execute(
            text(
                "SELECT s.firebase_uid AS user_uid, v.tipo_vinculo, v.es_responsable, "
                "v.puede_gestionar_contactos, v.puede_gestionar_recordatorios, "
                "v.puede_recibir_alertas, v.estado, v.fecha_fin "
                f"FROM {T_VINCULO_USUARIO} v "
                f"JOIN {T_USUARIO} a ON a.id_usuario = v.adulto_mayor_id "
                f"JOIN {T_USUARIO} s ON s.id_usuario = v.usuario_apoyo_id "
                "WHERE a.firebase_uid = :uid ORDER BY v.id_vinculo"
            ),
            {"uid": adult_uid},
        ).all()


# --- Perfiles ---


def test_add_and_get_profile_round_trip_with_all_fields(repo, sql_db):
    token = uuid.uuid4().hex
    original = _profile(
        email=f"  Test-{token}@Example.INVALID ",
        role=Role.ADULTO_MAYOR,
        phone="+573001234567",
        birth_date=date(1950, 5, 1),
        address="Calle 1 # 2-3",
        photo_url="https://example.com/ana.png",
    )
    normalized = f"test-{token}@example.invalid"

    saved = repo.add_profile(original)

    assert saved == replace(original, email=normalized, email_verified=False)
    assert saved is not original
    assert original.email == f"  Test-{token}@Example.INVALID "

    fetched = repo.get_profile(original.uid)
    assert fetched == UserProfile(
        uid=original.uid,
        first_name="Ana",
        last_name="Pérez",
        email=normalized,
        role=Role.ADULTO_MAYOR,
        phone="+573001234567",
        birth_date=date(1950, 5, 1),
        address="Calle 1 # 2-3",
        photo_url="https://example.com/ana.png",
        email_verified=False,
    )

    fetched.first_name = "Cambiado"
    assert repo.get_profile(original.uid).first_name == "Ana"
    assert repo.get_profile(original.uid) is not repo.get_profile(original.uid)

    row = _user_row(sql_db, original.uid)
    assert row.correo == normalized
    assert row.correo_verificado is False
    assert row.creado_en == row.actualizado_en


def test_add_profile_overwrites_existing_profile(repo, sql_db):
    first = repo.add_profile(_profile(phone="+573001234567", address="Calle 1"))
    first_row = _user_row(sql_db, first.uid)

    repo.add_profile(_profile(uid=first.uid, first_name="María", email=first.email))

    stored = repo.get_profile(first.uid)
    assert stored.first_name == "María"
    assert stored.phone is None
    assert stored.address is None
    row = _user_row(sql_db, first.uid)
    assert row.id_usuario == first_row.id_usuario
    assert row.actualizado_en >= row.creado_en


def test_add_profile_keeps_verified_flag_when_email_is_the_same(repo):
    profile = repo.add_profile(_profile())
    repo.mark_email_verified(profile.uid, profile.email)

    saved = repo.add_profile(
        _profile(uid=profile.uid, email=f" {profile.email.upper()} ", email_verified=False)
    )

    assert saved.email == profile.email
    assert saved.email_verified is True
    assert repo.get_profile(profile.uid).email_verified is True
    assert repo.find_uid_by_verified_email(profile.email) == profile.uid


def test_add_profile_resets_verified_flag_when_email_changes(repo):
    profile = repo.add_profile(_profile())
    repo.mark_email_verified(profile.uid, profile.email)
    new_email = _email()

    saved = repo.add_profile(_profile(uid=profile.uid, email=new_email))

    assert saved.email_verified is False
    stored = repo.get_profile(profile.uid)
    assert stored.email == new_email
    assert stored.email_verified is False
    assert repo.find_uid_by_verified_email(profile.email) is None
    assert repo.find_uid_by_verified_email(new_email) is None


def test_role_change_keeps_a_single_active_row(repo, sql_db):
    profile = repo.add_profile(_profile(role=Role.FAMILIAR))
    assert _role_rows(sql_db, profile.uid) == {"FAMILIAR": True}

    repo.add_profile(replace(profile, role=Role.CUIDADOR))

    assert _role_rows(sql_db, profile.uid) == {"FAMILIAR": False, "CUIDADOR": True}
    assert repo.get_profile(profile.uid).role is Role.CUIDADOR

    repo.add_profile(replace(profile, role=Role.FAMILIAR))

    assert _role_rows(sql_db, profile.uid) == {"FAMILIAR": True, "CUIDADOR": False}
    assert repo.get_profile(profile.uid).role is Role.FAMILIAR


def test_role_catalog_seeds_only_the_code_used(repo, sql_db):
    before = _role_catalog(sql_db)

    repo.add_profile(_profile(role=Role.ADULTO_MAYOR))

    after = _role_catalog(sql_db)
    assert "ADULTO_MAYOR" in after
    assert set(after) - set(before) <= {"ADULTO_MAYOR"}
    if "ADULTO_MAYOR" not in before:
        assert after["ADULTO_MAYOR"] == "Adulto mayor"


def test_get_profile_of_unknown_uid_returns_none(repo):
    assert repo.get_profile(_uid()) is None
    assert repo.get_profile("") is None


def test_profile_without_role_has_role_none(repo, sql_db, sql_user):
    uid, _ = sql_user(first_name="Rosa", last_name="Gómez")

    stored = repo.get_profile(uid)

    assert stored.role is None
    assert stored.first_name == "Rosa"
    assert stored.last_name == "Gómez"
    assert stored.email.endswith("@example.invalid")

    no_role = repo.add_profile(_profile(role=None))
    assert repo.get_profile(no_role.uid).role is None
    assert _role_rows(sql_db, no_role.uid) == {}

    with_role = repo.add_profile(_profile(role=Role.FAMILIAR))
    repo.add_profile(replace(with_role, role=None))
    assert repo.get_profile(with_role.uid).role is None
    assert _role_rows(sql_db, with_role.uid) == {"FAMILIAR": False}


def test_update_profile_applies_changes_and_returns_the_stored_profile(repo, sql_db):
    profile = repo.add_profile(
        _profile(phone="+573001234567", photo_url="https://example.com/a.png")
    )

    updated = repo.update_profile(
        profile.uid,
        {
            "first_name": "María",
            "phone": None,
            "address": "Calle 2",
            "birth_date": date(1948, 1, 2),
            "photo_url": None,
        },
    )

    assert updated == UserProfile(
        uid=profile.uid,
        first_name="María",
        last_name="Pérez",
        email=profile.email,
        role=Role.FAMILIAR,
        phone=None,
        birth_date=date(1948, 1, 2),
        address="Calle 2",
        photo_url=None,
    )
    assert repo.get_profile(profile.uid) == updated
    updated.first_name = "Cambiado"
    assert repo.get_profile(profile.uid).first_name == "María"
    row = _user_row(sql_db, profile.uid)
    assert row.actualizado_en >= row.creado_en


def test_update_profile_ignores_unknown_keys(repo, sql_db):
    before = repo.add_profile(_profile())
    other_uid = _uid()

    updated = repo.update_profile(
        before.uid,
        {"uid": other_uid, "unknown": 1, "full_name": "X", "email": None},
    )

    assert updated == before
    assert repo.get_profile(before.uid) == before
    assert repo.get_profile(other_uid) is None
    assert _user_row(sql_db, other_uid) is None


def test_update_profile_of_unknown_uid_returns_none_without_writing(repo, sql_db):
    uid = _uid()

    assert repo.update_profile(uid, {"first_name": "Ana", "role": Role.FAMILIAR}) is None
    assert repo.get_profile(uid) is None
    assert _user_row(sql_db, uid) is None


def test_update_profile_email_keeps_or_resets_the_verified_flag(repo, sql_db):
    profile = repo.add_profile(_profile())
    repo.mark_email_verified(profile.uid, profile.email)

    same = repo.update_profile(profile.uid, {"email": f"  {profile.email.upper()} "})
    assert same.email == profile.email
    assert same.email_verified is True

    new_email = _email()
    changed = repo.update_profile(profile.uid, {"email": new_email.upper()})
    assert changed.email == new_email
    assert changed.email_verified is False
    assert repo.find_uid_by_verified_email(profile.email) is None
    assert repo.find_uid_by_verified_email(new_email) is None

    verified = repo.update_profile(profile.uid, {"email_verified": True})
    assert verified.email_verified is True
    assert repo.find_uid_by_verified_email(new_email) == profile.uid

    blank = repo.update_profile(profile.uid, {"email": "   "})
    assert blank.email == ""
    assert blank.email_verified is False
    assert _user_row(sql_db, profile.uid).correo == f"{profile.uid}@sin-correo.local"


def test_update_profile_changes_the_role(repo, sql_db):
    profile = repo.add_profile(_profile(role=Role.FAMILIAR))

    updated = repo.update_profile(profile.uid, {"role": Role.CUIDADOR})

    assert updated.role is Role.CUIDADOR
    assert _role_rows(sql_db, profile.uid) == {"FAMILIAR": False, "CUIDADOR": True}

    cleared = repo.update_profile(profile.uid, {"role": None})

    assert cleared.role is None
    assert repo.get_profile(profile.uid).role is None


# --- Correos verificados y fila mínima ---


def test_mark_email_verified_without_profile_creates_the_minimal_row(repo, sql_db):
    uid = _uid()
    email = _email()

    repo.mark_email_verified(uid, f"  {email.upper()} ")

    assert repo.get_profile(uid) is None
    row = _user_row(sql_db, uid)
    assert (row.nombre, row.apellido) == ("-", "-")
    assert row.correo == email
    assert row.correo_verificado is True

    assert repo.update_profile(uid, {"first_name": "Ana", "last_name": "Pérez"}) is None
    unchanged = _user_row(sql_db, uid)
    assert (unchanged.nombre, unchanged.apellido) == ("-", "-")
    assert unchanged.actualizado_en == row.actualizado_en
    assert repo.get_profile(uid) is None

    assert repo.find_uid_by_verified_email(email) == uid
    assert repo.find_uid_by_verified_email(f"  {email.upper()} ") == uid
    assert repo.find_uid_by_verified_email(_email()) is None


def test_add_profile_over_minimal_row_with_same_email_keeps_verified(repo):
    uid = _uid()
    email = _email()
    repo.mark_email_verified(uid, email)

    saved = repo.add_profile(_profile(uid=uid, email=email))

    assert saved.email_verified is True
    stored = repo.get_profile(uid)
    assert stored.first_name == "Ana"
    assert stored.email_verified is True
    assert repo.find_uid_by_verified_email(email) == uid


def test_add_profile_over_minimal_row_with_other_email_resets_verified(repo):
    uid = _uid()
    email = _email()
    repo.mark_email_verified(uid, email)
    other_email = _email()

    saved = repo.add_profile(_profile(uid=uid, email=other_email))

    assert saved.email_verified is False
    assert repo.get_profile(uid).email == other_email
    assert repo.find_uid_by_verified_email(email) is None
    assert repo.find_uid_by_verified_email(other_email) is None


def test_find_uid_by_verified_email_ignores_unverified_rows(repo, sql_user):
    unverified_email = _email()
    sql_user(email=unverified_email, verified=False)
    profile = repo.add_profile(_profile())
    verified_email = _email()
    verified_uid, _ = sql_user(email=verified_email, verified=True)

    assert repo.find_uid_by_verified_email(unverified_email) is None
    assert repo.find_uid_by_verified_email(profile.email) is None
    assert repo.find_uid_by_verified_email(verified_email) == verified_uid
    assert repo.find_uid_by_verified_email("") is None
    assert repo.find_uid_by_verified_email("   ") is None


def test_latest_verified_mark_wins(repo, sql_db):
    email = _email()
    old = repo.add_profile(_profile(email=email))
    repo.mark_email_verified(old.uid, email)
    new_uid = _uid()

    repo.mark_email_verified(new_uid, email.upper())

    assert repo.find_uid_by_verified_email(email) == new_uid
    old_row = _user_row(sql_db, old.uid)
    assert old_row.correo == f"{old.uid}@sin-correo.local"
    assert old_row.correo_verificado is False
    old_profile = repo.get_profile(old.uid)
    assert old_profile.email == ""
    assert old_profile.email_verified is False

    other_email = _email()
    repo.mark_email_verified(new_uid, other_email)

    assert repo.find_uid_by_verified_email(email) is None
    assert repo.find_uid_by_verified_email(other_email) == new_uid


def test_add_profile_releases_the_email_of_an_obsolete_profile(repo):
    email = _email()
    old = repo.add_profile(_profile(email=email))

    new = repo.add_profile(_profile(email=email.upper()))

    assert new.email == email
    assert repo.get_profile(new.uid).email == email
    assert repo.get_profile(old.uid).email == ""


def test_empty_email_is_stored_as_placeholder(repo, sql_db):
    first = repo.add_profile(_profile(email="   "))
    second = repo.add_profile(_profile(email=""))

    assert first.email == ""
    assert repo.get_profile(first.uid).email == ""
    assert repo.get_profile(second.uid).email == ""
    assert _user_row(sql_db, first.uid).correo == f"{first.uid}@sin-correo.local"
    assert _user_row(sql_db, second.uid).correo == f"{second.uid}@sin-correo.local"


def test_mark_email_verified_ignores_blank_email(repo, sql_db):
    uid = _uid()
    repo.mark_email_verified(uid, "   ")
    assert _user_row(sql_db, uid) is None

    profile = repo.add_profile(_profile())
    repo.mark_email_verified(profile.uid, "")
    assert repo.get_profile(profile.uid).email_verified is False


def test_blank_names_and_optional_texts_follow_the_checks(repo, sql_db):
    profile = repo.add_profile(_profile(last_name="", phone="  ", address=""))

    row = _user_row(sql_db, profile.uid)
    assert row.nombre == "Ana"
    assert row.apellido == "-"
    assert row.telefono is None
    assert row.direccion is None
    stored = repo.get_profile(profile.uid)
    assert stored.last_name == ""
    assert stored.phone is None
    assert stored.address is None
    assert stored.full_name == "Ana"


# --- Vínculos ---


def test_link_adult_upsert_does_not_duplicate(repo, sql_db):
    family_uid, adult_uid = _uid(), _uid()

    repo.link_adult(family_uid, adult_uid, Role.FAMILIAR)
    repo.link_adult(family_uid, adult_uid, Role.FAMILIAR)

    assert len(_link_rows(sql_db, adult_uid)) == 1
    assert repo.is_linked(family_uid, adult_uid) is True
    assert repo.adults_of(family_uid) == [adult_uid]


def test_link_adult_creates_minimal_rows_for_unknown_uids(repo, sql_db):
    family_uid, adult_uid = _uid(), _uid()

    repo.link_adult(family_uid, adult_uid, Role.FAMILIAR)

    assert repo.get_profile(family_uid) is None
    assert repo.get_profile(adult_uid) is None
    assert (_user_row(sql_db, adult_uid).nombre, _user_row(sql_db, adult_uid).apellido) == (
        "-",
        "-",
    )
    assert repo.link_of(family_uid, adult_uid) == AdultLink(
        user_uid=family_uid, adult_uid=adult_uid, role=Role.FAMILIAR, permissions=None
    )


def test_link_caregiver_stores_permissions_and_defaults_to_false(repo):
    caregiver_uid, adult_uid = _uid(), _uid()
    permissions = _permissions(manage_contacts=True, manage_reminders=False)

    repo.link_adult(caregiver_uid, adult_uid, Role.CUIDADOR, permissions)
    permissions.manage_reminders = True

    link = repo.link_of(caregiver_uid, adult_uid)
    assert link == AdultLink(
        user_uid=caregiver_uid,
        adult_uid=adult_uid,
        role=Role.CUIDADOR,
        permissions=_permissions(manage_contacts=True, manage_reminders=False),
    )
    assert isinstance(link.permissions, CaregiverPermissions)
    link.permissions.receive_alerts = False
    assert repo.link_of(caregiver_uid, adult_uid).permissions.receive_alerts is True

    repo.link_adult(caregiver_uid, adult_uid, Role.CUIDADOR)

    assert repo.link_of(caregiver_uid, adult_uid).permissions == NO_PERMISSIONS
    [caregiver] = repo.caregivers_of(adult_uid)
    assert caregiver.permissions == NO_PERMISSIONS
    assert NO_CAREGIVER_PERMISSIONS == NO_PERMISSIONS


def test_link_adult_accepts_role_value(repo):
    caregiver_uid, adult_uid = _uid(), _uid()

    repo.link_adult(caregiver_uid, adult_uid, "cuidador")

    link = repo.link_of(caregiver_uid, adult_uid)
    assert link.role is Role.CUIDADOR
    assert link.permissions == NO_PERMISSIONS


def test_link_family_never_stores_permissions(repo, sql_db):
    family_uid, adult_uid = _uid(), _uid()

    repo.link_adult(family_uid, adult_uid, Role.FAMILIAR, _permissions())

    assert repo.link_of(family_uid, adult_uid).permissions is None
    [row] = _link_rows(sql_db, adult_uid)
    assert row.tipo_vinculo == "FAMILIAR"
    assert (
        row.puede_gestionar_contactos,
        row.puede_gestionar_recordatorios,
        row.puede_recibir_alertas,
    ) == (False, False, False)


@pytest.mark.parametrize(
    "role", [Role.ADMINISTRADOR, Role.ADULTO_MAYOR, "administrador", "invalido"]
)
def test_link_adult_rejects_roles_other_than_family_or_caregiver(repo, sql_db, role):
    user_uid, adult_uid = _uid(), _uid()

    with pytest.raises(ValueError):
        repo.link_adult(user_uid, adult_uid, role)

    assert _user_row(sql_db, user_uid) is None
    assert _user_row(sql_db, adult_uid) is None


def test_link_adult_rejects_linking_a_user_with_itself(repo, sql_db):
    uid = _uid()

    with pytest.raises(ValueError):
        repo.link_adult(uid, uid, Role.FAMILIAR)

    assert _user_row(sql_db, uid) is None


def test_responsible_family_is_the_first_family_link(repo, sql_db):
    adult_uid, other_adult_uid = _uid(), _uid()
    caregiver_uid, first_family, second_family = _uid(), _uid(), _uid()
    repo.link_adult(caregiver_uid, adult_uid, Role.CUIDADOR)

    assert repo.responsible_family_of(adult_uid) is None

    repo.link_adult(first_family, adult_uid, Role.FAMILIAR)
    repo.link_adult(second_family, adult_uid, Role.FAMILIAR)
    repo.link_adult(second_family, other_adult_uid, Role.FAMILIAR)

    assert repo.responsible_family_of(adult_uid) == first_family
    assert repo.responsible_family_of(other_adult_uid) == second_family

    repo.link_adult(first_family, adult_uid, Role.FAMILIAR)

    assert repo.responsible_family_of(adult_uid) == first_family
    flags = {row.user_uid: row.es_responsable for row in _link_rows(sql_db, adult_uid)}
    assert flags == {caregiver_uid: False, first_family: True, second_family: False}
    assert repo.responsible_family_of(_uid()) is None


def test_responsible_flag_is_cleared_when_family_becomes_caregiver(repo, sql_db):
    adult_uid, first_family, second_family = _uid(), _uid(), _uid()
    repo.link_adult(first_family, adult_uid, Role.FAMILIAR)
    repo.link_adult(second_family, adult_uid, Role.FAMILIAR)

    repo.link_adult(first_family, adult_uid, Role.CUIDADOR, _permissions())

    rows = {row.user_uid: row for row in _link_rows(sql_db, adult_uid)}
    assert rows[first_family].tipo_vinculo == "CUIDADOR"
    assert rows[first_family].es_responsable is False
    assert repo.responsible_family_of(adult_uid) == second_family
    assert [link.user_uid for link in repo.caregivers_of(adult_uid)] == [first_family]


def test_caregivers_of_returns_only_caregivers_in_link_order(repo, sql_user):
    adult_uid, other_adult_uid = _uid(), _uid()
    # El primer cuidador tiene el id_usuario más bajo: el orden debe ser el del vínculo.
    first_created, _ = sql_user()
    second_created, _ = sql_user()
    family_uid = _uid()
    second_permissions = _permissions(receive_alerts=True)

    repo.link_adult(second_created, adult_uid, Role.CUIDADOR, second_permissions)
    repo.link_adult(family_uid, adult_uid, Role.FAMILIAR)
    repo.link_adult(first_created, adult_uid, Role.CUIDADOR)
    repo.link_adult(first_created, other_adult_uid, Role.CUIDADOR, _permissions())

    caregivers = repo.caregivers_of(adult_uid)

    assert [link.user_uid for link in caregivers] == [second_created, first_created]
    assert all(link.adult_uid == adult_uid for link in caregivers)
    assert all(link.role is Role.CUIDADOR for link in caregivers)
    assert caregivers[0].permissions == second_permissions
    assert caregivers[1].permissions == NO_PERMISSIONS
    assert repo.caregivers_of(_uid()) == []


def test_adults_of_follows_link_order(repo, sql_user):
    first_created, _ = sql_user()
    second_created, _ = sql_user()
    family_uid = _uid()

    repo.link_adult(family_uid, second_created, Role.FAMILIAR)
    repo.link_adult(family_uid, first_created, Role.FAMILIAR)
    repo.link_adult(family_uid, second_created, Role.FAMILIAR)

    assert repo.adults_of(family_uid) == [second_created, first_created]
    assert repo.adults_of(_uid()) == []


def test_is_linked_and_link_of_without_link(repo):
    family_uid, adult_uid, other_adult_uid = _uid(), _uid(), _uid()
    repo.link_adult(family_uid, adult_uid, Role.FAMILIAR)

    assert repo.is_linked(family_uid, other_adult_uid) is False
    assert repo.is_linked(adult_uid, family_uid) is False
    assert repo.is_linked(_uid(), adult_uid) is False
    assert repo.link_of(family_uid, other_adult_uid) is None
    assert repo.link_of(adult_uid, family_uid) is None
    assert repo.link_of(_uid(), adult_uid) is None


def test_revoked_links_are_ignored_and_relinking_reactivates(repo, sql_db):
    caregiver_uid, family_uid, adult_uid = _uid(), _uid(), _uid()
    repo.link_adult(family_uid, adult_uid, Role.FAMILIAR)
    repo.link_adult(caregiver_uid, adult_uid, Role.CUIDADOR)
    with sql_db.transaction() as conn:
        conn.execute(
            text(
                f"UPDATE {T_VINCULO_USUARIO} v SET estado = 'REVOCADO', "
                "fecha_fin = v.fecha_inicio "
                f"FROM {T_USUARIO} a WHERE a.id_usuario = v.adulto_mayor_id "
                "AND a.firebase_uid = :uid"
            ),
            {"uid": adult_uid},
        )

    assert repo.is_linked(caregiver_uid, adult_uid) is False
    assert repo.link_of(caregiver_uid, adult_uid) is None
    assert repo.adults_of(caregiver_uid) == []
    assert repo.caregivers_of(adult_uid) == []
    assert repo.responsible_family_of(adult_uid) is None

    repo.link_adult(caregiver_uid, adult_uid, Role.CUIDADOR, _permissions())

    assert repo.is_linked(caregiver_uid, adult_uid) is True
    assert repo.link_of(caregiver_uid, adult_uid).permissions == _permissions()
    rows = {row.user_uid: row for row in _link_rows(sql_db, adult_uid)}
    assert len(rows) == 2
    assert rows[caregiver_uid].estado == "ACTIVO"
    assert rows[caregiver_uid].fecha_fin is None
    assert rows[family_uid].estado == "REVOCADO"


def test_relinking_a_revoked_responsible_does_not_violate_the_unique_index(repo, sql_db):
    # V004: uq_vinculo_responsable_activo_por_adulto admite un solo responsable ACTIVO
    # por adulto; el vínculo revocado conserva es_responsable y no debe recuperarlo.
    adult_uid, first_family, second_family = _uid(), _uid(), _uid()
    repo.link_adult(first_family, adult_uid, Role.FAMILIAR)
    with sql_db.transaction() as conn:
        revoked = conn.execute(
            text(
                f"UPDATE {T_VINCULO_USUARIO} v SET estado = 'REVOCADO', "
                "fecha_fin = GREATEST(clock_timestamp(), v.fecha_inicio) "
                f"FROM {T_USUARIO} a, {T_USUARIO} s "
                "WHERE a.id_usuario = v.adulto_mayor_id "
                "AND s.id_usuario = v.usuario_apoyo_id "
                "AND a.firebase_uid = :adult_uid AND s.firebase_uid = :user_uid"
            ),
            {"adult_uid": adult_uid, "user_uid": first_family},
        )
        assert revoked.rowcount == 1

    repo.link_adult(second_family, adult_uid, Role.FAMILIAR)

    assert repo.responsible_family_of(adult_uid) == second_family
    rows = {row.user_uid: row for row in _link_rows(sql_db, adult_uid)}
    assert (rows[first_family].estado, rows[first_family].es_responsable) == (
        "REVOCADO",
        True,
    )
    assert (rows[second_family].estado, rows[second_family].es_responsable) == (
        "ACTIVO",
        True,
    )

    repo.link_adult(first_family, adult_uid, Role.FAMILIAR)

    rows = {row.user_uid: row for row in _link_rows(sql_db, adult_uid)}
    assert {uid: row.es_responsable for uid, row in rows.items()} == {
        first_family: False,
        second_family: True,
    }
    assert {uid: row.estado for uid, row in rows.items()} == {
        first_family: "ACTIVO",
        second_family: "ACTIVO",
    }
    assert rows[first_family].fecha_fin is None
    assert repo.is_linked(first_family, adult_uid) is True
    assert repo.responsible_family_of(adult_uid) == second_family


def test_blank_and_unknown_uids_do_not_break(repo):
    assert repo.get_profile("") is None
    assert repo.update_profile("", {"first_name": "Ana"}) is None
    assert repo.is_linked("", "") is False
    assert repo.adults_of("") == []
    assert repo.caregivers_of("") == []
    assert repo.responsible_family_of("") is None
    assert repo.link_of("no-existe", "tampoco") is None
    assert repo.find_uid_by_verified_email("no-es-un-correo") is None


# --- Proveedor ---


def test_provider_returns_sql_repository_only_when_enabled(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(user_repository, "_sql_user_repository", lambda: sentinel)

    monkeypatch.setattr(db_session, "sql_enabled", lambda: True)
    assert user_repository.get_user_repository() is sentinel

    monkeypatch.setattr(db_session, "sql_enabled", lambda: False)
    assert (
        user_repository.get_user_repository()
        is user_repository._default_user_repository
    )


def test_sql_repository_satisfies_the_protocol(repo):
    typed: UserRepository = repo
    for name in (
        "add_profile",
        "get_profile",
        "update_profile",
        "mark_email_verified",
        "find_uid_by_verified_email",
        "link_adult",
        "is_linked",
        "adults_of",
        "link_of",
        "caregivers_of",
        "responsible_family_of",
    ):
        assert callable(getattr(typed, name))
