"""Modelos de recordatorios (R1-R6, RF-27 a RF-30).

Los Requests y la Response usan los campos literales del contrato (CLAUDE.md 6.6).
ReminderRecord es el registro interno del repositorio; nunca se devuelve tal cual.
"""
from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.models.enums import ReminderKind, ReminderRepeat, ReminderStatus


DESCRIPTION_MAX_LENGTH = 500
SNOOZE_MIN_MINUTES = 1
SNOOZE_MAX_MINUTES = 1440


class ReminderCreateRequest(BaseModel):
    """Entrada de R2 (POST /api/reminders).

    adult_id solo lo envían familiar y cuidador (regla del adulto implícito, sección 5).
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    adult_id: str | None = None
    kind: ReminderKind
    description: str = Field(min_length=1, max_length=DESCRIPTION_MAX_LENGTH)
    scheduled_at: AwareDatetime
    repeat: ReminderRepeat


class ReminderUpdateRequest(BaseModel):
    """Entrada de R3 (PATCH /api/reminders/{id}): cualquier campo de R2, todos opcionales.

    adult_id no se declara: en rutas con {id} el adulto se resuelve desde el recurso
    (sección 5) y, si llega, se ignora (extra="ignore"). Un null explícito en un campo
    es un dato inválido (422, D-R2-11); un campo ausente no cambia.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    kind: ReminderKind | None = None
    description: str | None = Field(
        default=None, min_length=1, max_length=DESCRIPTION_MAX_LENGTH
    )
    scheduled_at: AwareDatetime | None = None
    repeat: ReminderRepeat | None = None

    @field_validator("kind", "description", "scheduled_at", "repeat", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("no es válido")
        return value


class ReminderSnoozeRequest(BaseModel):
    """Entrada de R6 (POST /api/reminders/{id}/snooze); el frontend envía 10."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    minutes: int = Field(ge=SNOOZE_MIN_MINUTES, le=SNOOZE_MAX_MINUTES)


class ReminderResponse(BaseModel):
    """Salida de R1 (cada elemento), R2, R3, R5 y R6."""

    id: str
    kind: ReminderKind
    description: str
    scheduled_at: AwareDatetime
    repeat: ReminderRepeat
    status: ReminderStatus


@dataclass
class ReminderRecord:
    """Registro interno de un recordatorio.

    notified_at: momento en que el planificador envió la push (RF-28); None mientras no
    se haya notificado la hora actual de scheduled_at. next_created: ya se creó la
    siguiente ocurrencia de un recordatorio repetido (D-R2-11), para no duplicarla.
    """

    id: str
    adult_uid: str
    kind: ReminderKind
    description: str
    scheduled_at: datetime
    repeat: ReminderRepeat
    status: ReminderStatus
    created_at: datetime
    notified_at: datetime | None = None
    next_created: bool = False
