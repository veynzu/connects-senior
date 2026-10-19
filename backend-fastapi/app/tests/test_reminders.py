from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api import reminders_router
from app.api.reminders_router import REMINDER_NOT_FOUND_DETAIL, REMINDER_TICK_NAME
from app.core.config import get_settings
from app.core.scheduler import registered_ticks
from app.core.security import FORBIDDEN_DETAIL, get_current_user
from app.integrations import fcm_client
from app.integrations.fcm_client import FcmResult, FcmUnavailableError
from app.main import app
from app.models.auth import AuthenticatedUser
from app.models.enums import ReminderKind, ReminderRepeat, ReminderStatus, Role
from app.models.reminders import (
    ReminderCreateRequest,
    ReminderRecord,
    ReminderUpdateRequest,
)
from app.models.users import CaregiverPermissions
from app.repositories.device_repository import (
    InMemoryDeviceRepository,
    get_device_repository,
)
from app.repositories.reminder_repository import (
    InMemoryReminderRepository,
    get_reminder_repository,
)
from app.repositories.user_repository import (
    InMemoryUserRepository,
    get_user_repository,
)
from app.services.reminder_service import (
    REMINDER_PUSH_TITLE,
    ReminderNotFoundError,
    ReminderPermissionDeniedError,
    confirm_reminder,
    create_reminder,
    delete_reminder,
    next_occurrence,
    process_due_reminders,
    snooze_reminder,
    update_reminder,
)


REMINDERS_PATH = "/api/reminders"
ADULT_UID = "adult-uid"
OTHER_ADULT_UID = "other-adult-uid"
FAMILY_UID = "family-uid"
CAREGIVER_UID = "caregiver-uid"
ADMIN_UID = "admin-uid"
NO_ROLE_UID = "no-role-uid"
REMINDER_ID = "reminder-1"
ADULT_TOKEN = "fcm-token-adult"
DESCRIPTION = "Tomar la pastilla de la presión"
BEARER_DETAIL = "Debes enviar un token Bearer."
ADULT_ID_REQUIRED_DETAIL = "Datos inválidos: adult_id: es obligatorio"
REMINDER_KEYS = {"id", "kind", "description", "scheduled_at", "repeat", "status"}
MISSED_MINUTES = 15
BOGOTA = timezone(timedelta(hours=-5))
# 2026-10-20 08:30 en Bogotá = 13:30 UTC.
T0 = datetime(2026, 10, 20, 8, 30, tzinfo=BOGOTA)
T0_TEXT = "2026-10-20T08:30:00-05:00"


class FakeSendToTokens:
    """Sustituto de fcm_client.send_to_tokens: nunca hace red y registra cada envío."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, tokens, title, body, data):
        self.calls.append(
            {"tokens": list(tokens), "title": title, "body": body, "data": dict(data)}
        )
        if self.error is not None:
            raise self.error
        return FcmResult(success_count=len(tokens), failure_count=0, invalid_tokens=[])


@pytest.fixture(autouse=True)
def state(monkeypatch):
    shared = SimpleNamespace(
        reminders=InMemoryReminderRepository(),
        users=InMemoryUserRepository(),
        devices=InMemoryDeviceRepository(),
        fcm=FakeSendToTokens(),
    )
    monkeypatch.setattr(fcm_client, "send_to_tokens", shared.fcm)
    app.dependency_overrides[get_reminder_repository] = lambda: shared.reminders
    app.dependency_overrides[get_user_repository] = lambda: shared.users
    app.dependency_overrides[get_device_repository] = lambda: shared.devices
    yield shared
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _user(role: Role | None, uid: str = ADULT_UID) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=f"{uid}@example.com",
        email_verified=True,
        display_name="Usuario de prueba",
        role=role,
    )


def _as_user(role: Role | None, uid: str = ADULT_UID) -> None:
    user = _user(role, uid)
    app.dependency_overrides[get_current_user] = lambda: user


def _reminder_path(reminder_id: str = REMINDER_ID, suffix: str = "") -> str:
    return f"{REMINDERS_PATH}/{reminder_id}{suffix}"


def _record(
    reminder_id: str = REMINDER_ID,
    scheduled_at: datetime = T0,
    *,
    adult_uid: str = ADULT_UID,
    kind: ReminderKind = ReminderKind.MEDICAMENTO,
    description: str = DESCRIPTION,
    repeat: ReminderRepeat = ReminderRepeat.NINGUNA,
    status: ReminderStatus = ReminderStatus.PENDIENTE,
    notified_at: datetime | None = None,
    next_created: bool = False,
) -> ReminderRecord:
    return ReminderRecord(
        id=reminder_id,
        adult_uid=adult_uid,
        kind=kind,
        description=description,
        scheduled_at=scheduled_at,
        repeat=repeat,
        status=status,
        created_at=T0 - timedelta(days=1),
        notified_at=notified_at,
        next_created=next_created,
    )


def _create_body(**overrides) -> dict:
    body = {
        "kind": "medicamento",
        "description": DESCRIPTION,
        "scheduled_at": T0_TEXT,
        "repeat": "diaria",
    }
    body.update(overrides)
    return body


def _future() -> datetime:
    """Fecha futura respecto al reloj real, para rutas que usan la hora actual."""
    return datetime.now(BOGOTA).replace(microsecond=0) + timedelta(days=2)


def _link_caregiver(state, *, manage_reminders: bool | None) -> None:
    """Vincula al cuidador con ADULT_UID; manage_reminders None = sin permisos explícitos."""
    permissions = None
    if manage_reminders is not None:
        permissions = CaregiverPermissions(
            manage_contacts=True,
            manage_reminders=manage_reminders,
            receive_alerts=True,
        )
    state.users.link_adult(CAREGIVER_UID, ADULT_UID, Role.CUIDADOR, permissions)


# --- R2: crear ---


def test_create_reminder_adult_200_exact_body(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(
        REMINDERS_PATH, json=_create_body(description=f"  {DESCRIPTION}  ")
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == REMINDER_KEYS
    UUID(body["id"])
    assert body == {
        "id": body["id"],
        "kind": "medicamento",
        "description": DESCRIPTION,
        "scheduled_at": T0_TEXT,
        "repeat": "diaria",
        "status": "pendiente",
    }
    stored = state.reminders.get(body["id"])
    assert stored is not None
    assert stored.adult_uid == ADULT_UID
    assert stored.scheduled_at == T0
    assert stored.status == ReminderStatus.PENDIENTE
    assert stored.notified_at is None
    assert stored.next_created is False
    assert stored.created_at.tzinfo is not None


def test_create_reminder_adult_may_send_own_adult_id(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(REMINDERS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 200
    assert state.reminders.get(response.json()["id"]).adult_uid == ADULT_UID


def test_create_reminder_family_with_adult_id(state, client):
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.post(
        REMINDERS_PATH,
        json=_create_body(adult_id=ADULT_UID, kind="cita_medica", repeat="ninguna"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "cita_medica"
    assert body["repeat"] == "ninguna"
    assert body["status"] == "pendiente"
    assert state.reminders.get(body["id"]).adult_uid == ADULT_UID


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_create_reminder_family_requires_adult_id_422(state, client, role):
    state.users.link_adult(FAMILY_UID, ADULT_UID, role)
    _as_user(role, FAMILY_UID)

    response = client.post(REMINDERS_PATH, json=_create_body())

    assert response.status_code == 422
    assert response.json() == {"detail": ADULT_ID_REQUIRED_DETAIL}
    assert state.reminders.list_for_adult(ADULT_UID) == []


def test_create_reminder_unlinked_adult_403(state, client):
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, Role.FAMILIAR)
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.post(REMINDERS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.list_for_adult(ADULT_UID) == []


@pytest.mark.parametrize("method", ["get", "post"])
def test_adult_sends_foreign_adult_id_403(state, client, method):
    state.reminders.add(_record(adult_uid=OTHER_ADULT_UID))
    _as_user(Role.ADULTO_MAYOR)

    if method == "get":
        response = client.get(REMINDERS_PATH, params={"adult_id": OTHER_ADULT_UID})
    else:
        response = client.post(
            REMINDERS_PATH, json=_create_body(adult_id=OTHER_ADULT_UID)
        )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert len(state.reminders.list_for_adult(OTHER_ADULT_UID)) == 1


@pytest.mark.parametrize("manage_reminders", [False, None])
def test_create_reminder_caregiver_without_manage_reminders_403(
    state, client, manage_reminders
):
    _link_caregiver(state, manage_reminders=manage_reminders)
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.post(REMINDERS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.list_for_adult(ADULT_UID) == []


def test_create_reminder_caregiver_with_manage_reminders(state, client):
    _link_caregiver(state, manage_reminders=True)
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.post(REMINDERS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 200
    assert state.reminders.get(response.json()["id"]).adult_uid == ADULT_UID


def test_create_reminder_admin_403(state, client):
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)
    _as_user(Role.ADMINISTRADOR, ADMIN_UID)

    response = client.post(REMINDERS_PATH, json=_create_body(adult_id=ADULT_UID))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.list_for_adult(ADULT_UID) == []


def test_create_reminder_invalid_kind_422(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(REMINDERS_PATH, json=_create_body(kind="cita"))

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: kind: no es válido"}
    assert state.reminders.list_for_adult(ADULT_UID) == []


def test_create_reminder_invalid_repeat_422(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(REMINDERS_PATH, json=_create_body(repeat="mensual"))

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: repeat: no es válido"}
    assert state.reminders.list_for_adult(ADULT_UID) == []


def test_create_reminder_naive_scheduled_at_422(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(
        REMINDERS_PATH, json=_create_body(scheduled_at="2026-10-20T08:30:00")
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: scheduled_at: no es válido"}
    assert state.reminders.list_for_adult(ADULT_UID) == []


def test_create_reminder_blank_description_422(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(REMINDERS_PATH, json=_create_body(description="   "))

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Datos inválidos: description: es demasiado corto"
    }
    assert state.reminders.list_for_adult(ADULT_UID) == []


def test_create_reminder_missing_fields_422(state, client):
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(REMINDERS_PATH, json={"description": DESCRIPTION})

    assert response.status_code == 422
    detail = response.json()["detail"]
    for field in ("kind", "scheduled_at", "repeat"):
        assert f"{field}: es obligatorio" in detail


# --- R1: listar ---


def test_list_reminders_adult_implicit_ascending(state, client):
    state.reminders.add(_record("late", T0 + timedelta(hours=2)))
    state.reminders.add(_record("tie-first", T0 + timedelta(hours=1)))
    state.reminders.add(_record("early", T0, repeat=ReminderRepeat.SEMANAL))
    state.reminders.add(_record("tie-second", T0 + timedelta(hours=1)))
    state.reminders.add(_record("foreign", T0 - timedelta(hours=1), adult_uid=OTHER_ADULT_UID))
    _as_user(Role.ADULTO_MAYOR)

    response = client.get(REMINDERS_PATH)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == ["early", "tie-first", "tie-second", "late"]
    assert all(set(item) == REMINDER_KEYS for item in body)
    assert body[0] == {
        "id": "early",
        "kind": "medicamento",
        "description": DESCRIPTION,
        "scheduled_at": T0_TEXT,
        "repeat": "semanal",
        "status": "pendiente",
    }


def test_list_reminders_family_with_adult_id(state, client):
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    state.reminders.add(_record("foreign", adult_uid=OTHER_ADULT_UID))
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.get(REMINDERS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [REMINDER_ID]


@pytest.mark.parametrize("role", [Role.FAMILIAR, Role.CUIDADOR])
def test_list_reminders_family_requires_adult_id_422(state, client, role):
    state.users.link_adult(FAMILY_UID, ADULT_UID, role)
    _as_user(role, FAMILY_UID)

    response = client.get(REMINDERS_PATH)

    assert response.status_code == 422
    assert response.json() == {"detail": ADULT_ID_REQUIRED_DETAIL}


def test_list_reminders_caregiver_reads_without_manage_reminders(state, client):
    _link_caregiver(state, manage_reminders=None)
    state.reminders.add(_record())
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.get(REMINDERS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [REMINDER_ID]


def test_list_reminders_unlinked_403(state, client):
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.get(REMINDERS_PATH, params={"adult_id": ADULT_UID})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


# --- R3: actualizar ---


def test_update_reminder_new_scheduled_at_resets_pending(state, client):
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(
        _record(status=ReminderStatus.CONFIRMADO, notified_at=T0, next_created=True)
    )
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.patch(
        _reminder_path(), json={"scheduled_at": "2026-10-21T09:00:00-05:00"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": REMINDER_ID,
        "kind": "medicamento",
        "description": DESCRIPTION,
        "scheduled_at": "2026-10-21T09:00:00-05:00",
        "repeat": "ninguna",
        "status": "pendiente",
    }
    stored = state.reminders.get(REMINDER_ID)
    assert stored.status == ReminderStatus.PENDIENTE
    assert stored.notified_at is None
    assert stored.next_created is True


def test_update_reminder_same_scheduled_at_keeps_state(state, client):
    state.reminders.add(_record(status=ReminderStatus.CONFIRMADO, notified_at=T0))
    _as_user(Role.ADULTO_MAYOR)

    # Mismo instante expresado en UTC: no es un cambio de hora.
    response = client.patch(
        _reminder_path(),
        json={
            "scheduled_at": "2026-10-20T13:30:00Z",
            "description": "Tomar dos pastillas",
            "kind": "cita_medica",
            "repeat": "semanal",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmado"
    assert body["description"] == "Tomar dos pastillas"
    assert body["kind"] == "cita_medica"
    assert body["repeat"] == "semanal"
    stored = state.reminders.get(REMINDER_ID)
    assert stored.status == ReminderStatus.CONFIRMADO
    assert stored.notified_at == T0
    assert stored.scheduled_at == T0


def test_update_reminder_without_scheduled_at_keeps_state(state, client):
    state.reminders.add(_record(status=ReminderStatus.OMITIDO, notified_at=T0))
    _as_user(Role.ADULTO_MAYOR)

    response = client.patch(_reminder_path(), json={"description": "Otra"})

    assert response.status_code == 200
    stored = state.reminders.get(REMINDER_ID)
    assert stored.status == ReminderStatus.OMITIDO
    assert stored.notified_at == T0
    assert stored.description == "Otra"
    assert stored.scheduled_at == T0


@pytest.mark.parametrize("field", ["kind", "description", "scheduled_at", "repeat"])
def test_update_reminder_null_scheduled_at_422(state, client, field):
    state.reminders.add(_record())
    _as_user(Role.ADULTO_MAYOR)

    response = client.patch(_reminder_path(), json={field: None})

    assert response.status_code == 422
    assert response.json() == {"detail": f"Datos inválidos: {field}: no es válido"}
    assert state.reminders.get(REMINDER_ID) == _record()


def test_update_reminder_invalid_values_422(state, client):
    state.reminders.add(_record())
    _as_user(Role.ADULTO_MAYOR)

    response = client.patch(
        _reminder_path(), json={"scheduled_at": "2026-10-21T09:00:00", "kind": "x"}
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Datos inválidos: kind: no es válido; scheduled_at: no es válido"
    }
    assert state.reminders.get(REMINDER_ID) == _record()


def test_update_reminder_ignores_adult_id(state, client):
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.patch(
        _reminder_path(),
        params={"adult_id": OTHER_ADULT_UID},
        json={"adult_id": OTHER_ADULT_UID, "description": "Cambio"},
    )

    assert response.status_code == 200
    assert response.json()["description"] == "Cambio"
    stored = state.reminders.get(REMINDER_ID)
    assert stored.adult_uid == ADULT_UID
    assert stored.description == "Cambio"


@pytest.mark.parametrize("reminder_id", [REMINDER_ID, "missing"])
def test_update_reminder_unlinked_adult_404(state, client, reminder_id):
    state.users.link_adult(FAMILY_UID, OTHER_ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.patch(_reminder_path(reminder_id), json={"description": "x"})

    assert response.status_code == 404
    assert response.json() == {"detail": REMINDER_NOT_FOUND_DETAIL}
    assert state.reminders.get(REMINDER_ID) == _record()


def test_update_reminder_other_adult_404(state, client):
    state.reminders.add(_record(adult_uid=OTHER_ADULT_UID))
    _as_user(Role.ADULTO_MAYOR)

    response = client.patch(_reminder_path(), json={"description": "x"})

    assert response.status_code == 404
    assert response.json() == {"detail": REMINDER_NOT_FOUND_DETAIL}
    assert state.reminders.get(REMINDER_ID).description == DESCRIPTION


@pytest.mark.parametrize("manage_reminders", [False, None])
def test_update_reminder_caregiver_without_manage_reminders_403(
    state, client, manage_reminders
):
    _link_caregiver(state, manage_reminders=manage_reminders)
    state.reminders.add(_record())
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.patch(_reminder_path(), json={"description": "x"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.get(REMINDER_ID) == _record()


def test_update_reminder_caregiver_with_manage_reminders(state, client):
    _link_caregiver(state, manage_reminders=True)
    state.reminders.add(_record())
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.patch(_reminder_path(), json={"description": "Cambio"})

    assert response.status_code == 200
    assert state.reminders.get(REMINDER_ID).description == "Cambio"


def test_update_reminder_admin_403(state, client):
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(Role.ADMINISTRADOR, ADMIN_UID)

    response = client.patch(_reminder_path(), json={"description": "x"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.get(REMINDER_ID) == _record()


# --- R4: borrar ---


def test_delete_reminder_204_and_gone(state, client):
    state.reminders.add(_record())
    state.reminders.add(_record("other"))
    _as_user(Role.ADULTO_MAYOR)

    response = client.delete(_reminder_path())

    assert response.status_code == 204
    assert response.content == b""
    assert state.reminders.get(REMINDER_ID) is None
    assert state.reminders.get("other") is not None


def test_delete_reminder_family_204(state, client):
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.delete(_reminder_path())

    assert response.status_code == 204
    assert state.reminders.get(REMINDER_ID) is None


@pytest.mark.parametrize("reminder_id", ["missing", REMINDER_ID])
def test_delete_reminder_not_found_404(state, client, reminder_id):
    # REMINDER_ID pertenece a otro adulto: 404 sin revelar que existe (P18).
    state.reminders.add(_record(adult_uid=OTHER_ADULT_UID))
    _as_user(Role.ADULTO_MAYOR)

    response = client.delete(_reminder_path(reminder_id))

    assert response.status_code == 404
    assert response.json() == {"detail": REMINDER_NOT_FOUND_DETAIL}
    assert state.reminders.get(REMINDER_ID) is not None


@pytest.mark.parametrize("manage_reminders", [False, None])
def test_delete_reminder_caregiver_without_manage_reminders_403(
    state, client, manage_reminders
):
    _link_caregiver(state, manage_reminders=manage_reminders)
    state.reminders.add(_record())
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.delete(_reminder_path())

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.get(REMINDER_ID) is not None


def test_delete_reminder_caregiver_with_manage_reminders_204(state, client):
    _link_caregiver(state, manage_reminders=True)
    state.reminders.add(_record())
    _as_user(Role.CUIDADOR, CAREGIVER_UID)

    response = client.delete(_reminder_path())

    assert response.status_code == 204
    assert state.reminders.get(REMINDER_ID) is None


def test_delete_reminder_without_role_403(state, client):
    state.users.link_adult(NO_ROLE_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(None, NO_ROLE_UID)

    response = client.delete(_reminder_path())

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.get(REMINDER_ID) is not None


# --- R5: confirmar ---


def test_confirm_reminder_sets_confirmado(state, client):
    state.reminders.add(_record(notified_at=T0))
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(_reminder_path(suffix="/confirm"))

    assert response.status_code == 200
    assert response.json() == {
        "id": REMINDER_ID,
        "kind": "medicamento",
        "description": DESCRIPTION,
        "scheduled_at": T0_TEXT,
        "repeat": "ninguna",
        "status": "confirmado",
    }
    stored = state.reminders.get(REMINDER_ID)
    assert stored.status == ReminderStatus.CONFIRMADO
    # repeat ninguna: no hay siguiente ocurrencia.
    assert len(state.reminders.list_for_adult(ADULT_UID)) == 1
    assert stored.next_created is False


@pytest.mark.parametrize(
    ("repeat", "interval"),
    [
        (ReminderRepeat.DIARIA, timedelta(days=1)),
        (ReminderRepeat.SEMANAL, timedelta(days=7)),
    ],
)
def test_confirm_daily_and_weekly_create_next_occurrence(state, client, repeat, interval):
    scheduled_at = _future()
    state.reminders.add(_record(scheduled_at=scheduled_at, repeat=repeat, notified_at=T0))
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(_reminder_path(suffix="/confirm"))

    assert response.status_code == 200
    assert response.json()["status"] == "confirmado"
    records = state.reminders.list_for_adult(ADULT_UID)
    assert len(records) == 2
    original, following = records
    assert original.id == REMINDER_ID
    assert original.status == ReminderStatus.CONFIRMADO
    assert original.next_created is True
    assert following.id != REMINDER_ID
    UUID(following.id)
    assert following.adult_uid == ADULT_UID
    assert following.scheduled_at == scheduled_at + interval
    assert following.status == ReminderStatus.PENDIENTE
    assert following.notified_at is None
    assert following.next_created is False
    assert following.kind == original.kind
    assert following.description == original.description
    assert following.repeat == repeat


def test_confirm_twice_creates_single_occurrence(state, client):
    state.reminders.add(_record(scheduled_at=_future(), repeat=ReminderRepeat.DIARIA))
    _as_user(Role.ADULTO_MAYOR)

    first = client.post(_reminder_path(suffix="/confirm"))
    second = client.post(_reminder_path(suffix="/confirm"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(state.reminders.list_for_adult(ADULT_UID)) == 2


def test_confirm_after_omitido_does_not_duplicate(state):
    state.reminders.add(_record(repeat=ReminderRepeat.DIARIA))
    process_due_reminders(state.reminders, state.devices, MISSED_MINUTES, now=T0)
    omitted_at = T0 + timedelta(minutes=MISSED_MINUTES)
    assert process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=omitted_at
    ) == (0, 1)
    assert len(state.reminders.list_for_adult(ADULT_UID)) == 2

    result = confirm_reminder(
        _user(Role.ADULTO_MAYOR),
        REMINDER_ID,
        state.reminders,
        now=omitted_at + timedelta(minutes=5),
    )

    assert result.status == ReminderStatus.CONFIRMADO
    records = state.reminders.list_for_adult(ADULT_UID)
    assert len(records) == 2
    assert state.reminders.get(REMINDER_ID).status == ReminderStatus.CONFIRMADO
    assert state.reminders.get(REMINDER_ID).next_created is True


def test_snooze_after_confirm_then_confirm_does_not_duplicate(state):
    adult = _user(Role.ADULTO_MAYOR)
    state.reminders.add(_record(repeat=ReminderRepeat.DIARIA))

    confirm_reminder(adult, REMINDER_ID, state.reminders, now=T0)
    snoozed = snooze_reminder(adult, REMINDER_ID, 10, state.reminders)
    confirmed = confirm_reminder(
        adult, REMINDER_ID, state.reminders, now=T0 + timedelta(minutes=12)
    )

    assert snoozed.status == ReminderStatus.PENDIENTE
    assert confirmed.status == ReminderStatus.CONFIRMADO
    assert confirmed.scheduled_at == T0 + timedelta(minutes=10)
    records = state.reminders.list_for_adult(ADULT_UID)
    assert len(records) == 2
    assert [record.scheduled_at for record in records] == [
        T0 + timedelta(minutes=10),
        T0 + timedelta(days=1),
    ]


def test_confirm_by_family_403(state, client):
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.post(_reminder_path(suffix="/confirm"))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.get(REMINDER_ID) == _record()


@pytest.mark.parametrize("reminder_id", [REMINDER_ID, "missing"])
def test_confirm_other_adult_404(state, client, reminder_id):
    state.reminders.add(_record(adult_uid=OTHER_ADULT_UID))
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(_reminder_path(reminder_id, "/confirm"))

    assert response.status_code == 404
    assert response.json() == {"detail": REMINDER_NOT_FOUND_DETAIL}
    assert state.reminders.get(REMINDER_ID) == _record(adult_uid=OTHER_ADULT_UID)


# --- R6: posponer ---


def test_snooze_adds_minutes_and_resets_pending(state, client):
    state.reminders.add(
        _record(
            repeat=ReminderRepeat.DIARIA,
            status=ReminderStatus.OMITIDO,
            notified_at=T0,
            next_created=True,
        )
    )
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(_reminder_path(suffix="/snooze"), json={"minutes": 10})

    assert response.status_code == 200
    assert response.json() == {
        "id": REMINDER_ID,
        "kind": "medicamento",
        "description": DESCRIPTION,
        "scheduled_at": "2026-10-20T08:40:00-05:00",
        "repeat": "diaria",
        "status": "pendiente",
    }
    stored = state.reminders.get(REMINDER_ID)
    assert stored.scheduled_at == T0 + timedelta(minutes=10)
    assert stored.status == ReminderStatus.PENDIENTE
    assert stored.notified_at is None
    assert stored.next_created is True


def test_snooze_by_family_403(state, client):
    state.users.link_adult(FAMILY_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(Role.FAMILIAR, FAMILY_UID)

    response = client.post(_reminder_path(suffix="/snooze"), json={"minutes": 10})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.get(REMINDER_ID) == _record()


@pytest.mark.parametrize("reminder_id", [REMINDER_ID, "missing"])
def test_snooze_other_adult_404(state, client, reminder_id):
    state.reminders.add(_record(adult_uid=OTHER_ADULT_UID))
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(
        _reminder_path(reminder_id, "/snooze"), json={"minutes": 10}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": REMINDER_NOT_FOUND_DETAIL}
    assert state.reminders.get(REMINDER_ID) == _record(adult_uid=OTHER_ADULT_UID)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"minutes": 0}, "Datos inválidos: minutes: no es válido"),
        ({"minutes": -5}, "Datos inválidos: minutes: no es válido"),
        ({"minutes": 1441}, "Datos inválidos: minutes: no es válido"),
        ({"minutes": "diez"}, "Datos inválidos: minutes: no es válido"),
        ({"minutes": 10.5}, "Datos inválidos: minutes: no es válido"),
        ({}, "Datos inválidos: minutes: es obligatorio"),
    ],
)
def test_snooze_invalid_minutes_422(state, client, body, detail):
    state.reminders.add(_record())
    _as_user(Role.ADULTO_MAYOR)

    response = client.post(_reminder_path(suffix="/snooze"), json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert state.reminders.get(REMINDER_ID) == _record()


# --- Planificador (RF-28) ---


def test_process_due_notifies_once(state):
    state.devices.register(ADULT_UID, ADULT_TOKEN)
    state.reminders.add(_record())
    state.reminders.add(_record("future", T0 + timedelta(hours=3)))
    first_run = T0 + timedelta(minutes=1)

    first = process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=first_run
    )
    second = process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=first_run + timedelta(minutes=1)
    )

    assert first == (1, 0)
    assert second == (0, 0)
    assert state.fcm.calls == [
        {
            "tokens": [ADULT_TOKEN],
            "title": REMINDER_PUSH_TITLE,
            "body": DESCRIPTION,
            "data": {
                "type": "reminder",
                "reminder_id": REMINDER_ID,
                "kind": "medicamento",
                "scheduled_at": T0_TEXT,
            },
        }
    ]
    assert all(isinstance(value, str) for value in state.fcm.calls[0]["data"].values())
    stored = state.reminders.get(REMINDER_ID)
    assert stored.status == ReminderStatus.PENDIENTE
    assert stored.notified_at == first_run
    assert state.reminders.get("future").notified_at is None


def test_process_due_does_not_notify_future_or_closed_reminders(state):
    state.devices.register(ADULT_UID, ADULT_TOKEN)
    state.reminders.add(_record("future", T0 + timedelta(seconds=1)))
    state.reminders.add(_record("confirmed", status=ReminderStatus.CONFIRMADO))
    state.reminders.add(_record("omitted", status=ReminderStatus.OMITIDO))

    result = process_due_reminders(state.reminders, state.devices, MISSED_MINUTES, now=T0)

    assert result == (0, 0)
    assert state.fcm.calls == []


def test_process_due_without_devices_still_marks_notified(state):
    state.reminders.add(_record())

    result = process_due_reminders(state.reminders, state.devices, MISSED_MINUTES, now=T0)

    assert result == (1, 0)
    assert state.fcm.calls == []
    assert state.reminders.get(REMINDER_ID).notified_at == T0


def test_process_due_push_failure_still_marks_notified(state, caplog):
    state.devices.register(ADULT_UID, ADULT_TOKEN)
    state.fcm.error = FcmUnavailableError("El servicio de notificaciones no está disponible.")
    state.reminders.add(_record())

    result = process_due_reminders(state.reminders, state.devices, MISSED_MINUTES, now=T0)

    assert result == (1, 0)
    assert len(state.fcm.calls) == 1
    assert state.reminders.get(REMINDER_ID).notified_at == T0
    assert ADULT_TOKEN not in caplog.text


def test_process_due_marks_omitido_after_missed_minutes_from_notification_and_creates_next(
    state,
):
    state.reminders.add(_record(repeat=ReminderRepeat.DIARIA))
    # La push sale 5 minutos tarde (por ejemplo, el servidor estaba detenido).
    notified_at = T0 + timedelta(minutes=5)
    assert process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=notified_at
    ) == (1, 0)

    # 15 minutos después de scheduled_at, pero solo 10 desde la push: sigue pendiente.
    assert process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=T0 + timedelta(minutes=15)
    ) == (0, 0)
    assert process_due_reminders(
        state.reminders,
        state.devices,
        MISSED_MINUTES,
        now=notified_at + timedelta(minutes=MISSED_MINUTES - 1),
    ) == (0, 0)
    assert state.reminders.get(REMINDER_ID).status == ReminderStatus.PENDIENTE

    omitted_at = notified_at + timedelta(minutes=MISSED_MINUTES)
    result = process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=omitted_at
    )

    assert result == (0, 1)
    original = state.reminders.get(REMINDER_ID)
    assert original.status == ReminderStatus.OMITIDO
    assert original.next_created is True
    records = state.reminders.list_for_adult(ADULT_UID)
    assert len(records) == 2
    following = records[1]
    assert following.id != REMINDER_ID
    assert following.scheduled_at == T0 + timedelta(days=1)
    assert following.status == ReminderStatus.PENDIENTE
    assert following.notified_at is None
    assert following.next_created is False
    assert following.created_at == omitted_at

    # Más recorridos no omiten de nuevo ni crean otra ocurrencia.
    assert process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=omitted_at + timedelta(minutes=30)
    ) == (0, 0)
    assert len(state.reminders.list_for_adult(ADULT_UID)) == 2


def test_process_due_omits_one_time_reminder_without_next(state):
    state.reminders.add(_record(notified_at=T0))

    result = process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=T0 + timedelta(minutes=MISSED_MINUTES)
    )

    assert result == (0, 1)
    assert state.reminders.get(REMINDER_ID).status == ReminderStatus.OMITIDO
    assert state.reminders.get(REMINDER_ID).next_created is False
    assert len(state.reminders.list_for_adult(ADULT_UID)) == 1


def test_snoozed_late_reminder_gets_full_missed_window(state):
    adult = _user(Role.ADULTO_MAYOR)
    state.reminders.add(_record())
    assert process_due_reminders(state.reminders, state.devices, MISSED_MINUTES, now=T0) == (1, 0)

    # A los 12 minutos el adulto pospone 10 minutos: la nueva hora (T0 + 10) ya pasó.
    snooze_reminder(adult, REMINDER_ID, 10, state.reminders)
    renotified_at = T0 + timedelta(minutes=12, seconds=30)
    assert process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=renotified_at
    ) == (1, 0)
    assert state.reminders.get(REMINDER_ID).notified_at == renotified_at

    # La ventana original (T0 + 15) ya no aplica: se cuenta desde la nueva push.
    assert process_due_reminders(
        state.reminders, state.devices, MISSED_MINUTES, now=T0 + timedelta(minutes=20)
    ) == (0, 0)
    assert state.reminders.get(REMINDER_ID).status == ReminderStatus.PENDIENTE

    assert process_due_reminders(
        state.reminders,
        state.devices,
        MISSED_MINUTES,
        now=renotified_at + timedelta(minutes=MISSED_MINUTES),
    ) == (0, 1)
    assert state.reminders.get(REMINDER_ID).status == ReminderStatus.OMITIDO


def test_next_occurrence_skips_past_dates():
    daily = _record(repeat=ReminderRepeat.DIARIA)
    weekly = _record(repeat=ReminderRepeat.SEMANAL)
    now = T0 + timedelta(days=3, hours=2)

    following = next_occurrence(daily, now=now)

    assert following is not None
    assert following.scheduled_at == T0 + timedelta(days=4)
    assert following.id != daily.id
    assert following.created_at == now
    assert following.status == ReminderStatus.PENDIENTE
    assert following.notified_at is None
    assert following.next_created is False
    # Exactamente en la hora de una ocurrencia: la siguiente debe quedar en el futuro.
    assert next_occurrence(daily, now=T0 + timedelta(days=2)).scheduled_at == T0 + timedelta(days=3)
    # Antes de la hora programada: la siguiente es la del intervalo.
    assert next_occurrence(daily, now=T0 - timedelta(hours=1)).scheduled_at == T0 + timedelta(days=1)
    assert next_occurrence(weekly, now=T0 + timedelta(days=15)).scheduled_at == T0 + timedelta(days=21)
    assert next_occurrence(weekly, now=T0).scheduled_at == T0 + timedelta(days=7)
    assert next_occurrence(_record(repeat=ReminderRepeat.NINGUNA), now=now) is None
    assert next_occurrence(replace(daily, next_created=True), now=now) is None


class StaleListReminderRepository(InMemoryReminderRepository):
    """list_pending devuelve una foto vieja: simula una ruta que cambió el estado
    entre la lectura del planificador y su escritura."""

    def __init__(self) -> None:
        super().__init__()
        self.stale: list[ReminderRecord] | None = None

    def list_pending(self) -> list[ReminderRecord]:
        if self.stale is not None:
            return [replace(record) for record in self.stale]
        return super().list_pending()


def test_process_due_does_not_override_confirmed(state):
    adult = _user(Role.ADULTO_MAYOR)
    reminders = StaleListReminderRepository()
    state.devices.register(ADULT_UID, ADULT_TOKEN)
    reminders.add(_record("due", repeat=ReminderRepeat.DIARIA))
    reminders.add(_record("notified", notified_at=T0 - timedelta(hours=1)))
    reminders.stale = reminders.list_pending()
    confirm_reminder(adult, "due", reminders, now=T0)
    confirm_reminder(adult, "notified", reminders, now=T0)
    assert reminders.update_if_status(
        reminders.get("due"), ReminderStatus.PENDIENTE
    ) is None

    result = process_due_reminders(reminders, state.devices, MISSED_MINUTES, now=T0)

    assert result == (0, 0)
    assert state.fcm.calls == []
    due = reminders.get("due")
    assert due.status == ReminderStatus.CONFIRMADO
    assert due.notified_at is None
    notified = reminders.get("notified")
    assert notified.status == ReminderStatus.CONFIRMADO
    # Solo la ocurrencia creada al confirmar "due"; el planificador no creó otra.
    assert len(reminders.list_for_adult(ADULT_UID)) == 3


def test_reminder_tick_registered_and_uses_default_providers(monkeypatch):
    captured: list[dict] = []

    def fake_process_due_reminders(reminders, devices, missed_minutes, now=None):
        captured.append(
            {
                "reminders": reminders,
                "devices": devices,
                "missed_minutes": missed_minutes,
                "now": now,
            }
        )
        return (0, 0)

    monkeypatch.setattr(
        reminders_router, "process_due_reminders", fake_process_due_reminders
    )

    assert registered_ticks()[REMINDER_TICK_NAME] is reminders_router.reminder_tick
    reminders_router.reminder_tick()

    assert len(captured) == 1
    assert captured[0]["reminders"] is get_reminder_repository()
    assert captured[0]["devices"] is get_device_repository()
    assert captured[0]["missed_minutes"] == get_settings().reminder_missed_minutes
    assert captured[0]["now"] is None


# --- Autenticación y roles en todas las rutas ---

ROUTES = [
    pytest.param("get", REMINDERS_PATH, None, id="R1"),
    pytest.param("post", REMINDERS_PATH, _create_body(adult_id=ADULT_UID), id="R2"),
    pytest.param("patch", f"{REMINDERS_PATH}/{REMINDER_ID}", {"description": "x"}, id="R3"),
    pytest.param("delete", f"{REMINDERS_PATH}/{REMINDER_ID}", None, id="R4"),
    pytest.param("post", f"{REMINDERS_PATH}/{REMINDER_ID}/confirm", None, id="R5"),
    pytest.param("post", f"{REMINDERS_PATH}/{REMINDER_ID}/snooze", {"minutes": 10}, id="R6"),
]


def _send(client: TestClient, method: str, path: str, body: dict | None):
    if body is None:
        return client.request(method.upper(), path, params={"adult_id": ADULT_UID})
    return client.request(method.upper(), path, json=body)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_routes_require_bearer_401(state, client, method, path, body):
    state.reminders.add(_record())

    response = _send(client, method, path, body)

    assert response.status_code == 401
    assert response.json() == {"detail": BEARER_DETAIL}
    assert state.reminders.get(REMINDER_ID) == _record()


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_reminder_routes_admin_403(state, client, method, path, body):
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(Role.ADMINISTRADOR, ADMIN_UID)

    response = _send(client, method, path, body)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.list_for_adult(ADULT_UID) == [_record()]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_reminder_routes_role_none_403(state, client, method, path, body):
    state.users.link_adult(NO_ROLE_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    _as_user(None, NO_ROLE_UID)

    response = _send(client, method, path, body)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert state.reminders.list_for_adult(ADULT_UID) == [_record()]


# --- Servicio: defensa en profundidad ---


def test_service_create_reminder_rejects_unlinked_and_admin(state):
    payload = ReminderCreateRequest.model_validate(_create_body())
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)

    with pytest.raises(ReminderPermissionDeniedError):
        create_reminder(
            _user(Role.FAMILIAR, FAMILY_UID), ADULT_UID, payload, state.reminders, state.users
        )
    with pytest.raises(ReminderPermissionDeniedError):
        create_reminder(
            _user(Role.ADMINISTRADOR, ADMIN_UID), ADULT_UID, payload, state.reminders, state.users
        )
    with pytest.raises(ReminderPermissionDeniedError):
        create_reminder(
            _user(Role.ADULTO_MAYOR), OTHER_ADULT_UID, payload, state.reminders, state.users
        )
    assert state.reminders.list_for_adult(ADULT_UID) == []


def test_service_create_reminder_uses_injected_now(state):
    payload = ReminderCreateRequest.model_validate(_create_body())

    result = create_reminder(
        _user(Role.ADULTO_MAYOR), ADULT_UID, payload, state.reminders, state.users, now=T0
    )

    stored = state.reminders.get(result.id)
    assert stored.created_at == T0
    assert stored.status == ReminderStatus.PENDIENTE


def test_service_update_and_delete_out_of_scope_raise_not_found(state):
    state.users.link_adult(ADMIN_UID, ADULT_UID, Role.FAMILIAR)
    state.reminders.add(_record())
    payload = ReminderUpdateRequest(description="x")

    for user in (
        _user(Role.ADMINISTRADOR, ADMIN_UID),
        _user(None, ADMIN_UID),
        _user(Role.ADULTO_MAYOR, OTHER_ADULT_UID),
        _user(Role.FAMILIAR, FAMILY_UID),
    ):
        with pytest.raises(ReminderNotFoundError):
            update_reminder(user, REMINDER_ID, payload, state.reminders, state.users)
        with pytest.raises(ReminderNotFoundError):
            delete_reminder(user, REMINDER_ID, state.reminders, state.users)
    assert state.reminders.get(REMINDER_ID) == _record()


def test_service_confirm_and_snooze_require_adult_role(state):
    state.reminders.add(_record())

    for user in (
        _user(Role.FAMILIAR, ADULT_UID),
        _user(Role.ADMINISTRADOR, ADULT_UID),
        _user(None, ADULT_UID),
    ):
        with pytest.raises(ReminderPermissionDeniedError):
            confirm_reminder(user, REMINDER_ID, state.reminders, now=T0)
        with pytest.raises(ReminderPermissionDeniedError):
            snooze_reminder(user, REMINDER_ID, 10, state.reminders)
    assert state.reminders.get(REMINDER_ID) == _record()


# --- Repositorio ---


def test_repository_returns_copies_and_handles_missing_ids():
    repository = InMemoryReminderRepository()
    added = repository.add(_record())
    added.description = "cambiado fuera"

    assert repository.get(REMINDER_ID).description == DESCRIPTION
    assert repository.get("missing") is None
    assert repository.update(_record("missing")) is None
    assert repository.update_if_status(_record("missing"), ReminderStatus.PENDIENTE) is None
    assert repository.delete("missing") is False
    assert repository.delete(REMINDER_ID) is True
    assert repository.get(REMINDER_ID) is None


def test_update_if_status_only_replaces_expected_status():
    repository = InMemoryReminderRepository()
    repository.add(_record())

    changed = repository.update_if_status(
        _record(status=ReminderStatus.CONFIRMADO), ReminderStatus.PENDIENTE
    )
    ignored = repository.update_if_status(
        _record(status=ReminderStatus.OMITIDO), ReminderStatus.PENDIENTE
    )

    assert changed is not None
    assert changed.status == ReminderStatus.CONFIRMADO
    assert ignored is None
    assert repository.get(REMINDER_ID).status == ReminderStatus.CONFIRMADO


def test_repository_list_pending_and_update_keep_insertion_order():
    repository = InMemoryReminderRepository()
    repository.add(_record("a", T0 + timedelta(hours=1)))
    repository.add(_record("b", T0 + timedelta(hours=1), adult_uid=OTHER_ADULT_UID))
    repository.add(_record("c", T0, status=ReminderStatus.CONFIRMADO))
    repository.add(_record("d", T0 + timedelta(hours=1)))
    repository.update(_record("a", T0 + timedelta(hours=1), description="nuevo"))

    assert [record.id for record in repository.list_pending()] == ["a", "b", "d"]
    assert [record.id for record in repository.list_for_adult(ADULT_UID)] == ["c", "a", "d"]
