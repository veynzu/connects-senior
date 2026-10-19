from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.models.enums import CallStatus


class CallCreateRequest(BaseModel):
    """Entrada de L1 (POST /api/calls)."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    contact_id: str = Field(min_length=1)
    audio_only: bool = False


class CallUpdateRequest(BaseModel):
    """Entrada de L2 (PATCH /api/calls/{call_id})."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    status: CallStatus


class CallStartResponse(BaseModel):
    """Salida de L1: datos para unirse a la sala de Daily.co."""

    call_id: str
    room_url: str
    token: str


class CallResponse(BaseModel):
    """Elemento del historial de L3 y salida de L2."""

    id: str
    contact_id: str
    started_at: AwareDatetime
    status: CallStatus


@dataclass
class CallRecord:
    """Registro interno de una llamada; nunca se devuelve tal cual en la API.

    room_name y room_expires_at permiten crear más adelante el meeting token del
    contacto para la misma sala (tarea 3).
    """

    id: str
    adult_uid: str
    contact_id: str
    started_at: datetime
    status: CallStatus
    audio_only: bool
    room_name: str
    room_url: str
    room_expires_at: datetime
