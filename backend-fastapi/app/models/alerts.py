"""Modelos de Pedir ayuda (H1-H5, tarea 3; CLAUDE.md 6.5, ADR-10, D-R2-10).

Los Request y Response tienen la forma literal del contrato. AlertRecord es el
registro interno del repositorio y nunca se devuelve tal cual en la API.
"""
from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.models.enums import AlertCategory, AlertStatus


class AlertCreateRequest(BaseModel):
    """Entrada de H1 (POST /api/alerts). Todo es opcional; el adulto sale del token.

    Sin model_validator: si llega solo una coordenada lo detecta el servicio
    (AlertLocationIncompleteError) para responder el texto literal de D-R2-10.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    category: AlertCategory | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class AlertUpdateRequest(BaseModel):
    """Entrada de H5 (PATCH /api/alerts/{id})."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    status: AlertStatus


class AlertCreateResponse(BaseModel):
    """Salida de H1."""

    id: str
    status: AlertStatus
    created_at: AwareDatetime
    location_available: bool
    has_trusted_contacts: bool


class AlertSummary(BaseModel):
    """Elemento de la lista de H2."""

    id: str
    adult_name: str
    category: AlertCategory | None
    status: AlertStatus
    created_at: AwareDatetime
    location_available: bool


class AlertDetail(AlertSummary):
    """Salida de H3, H4 y H5: la solicitud con latitude y longitude si se compartieron."""

    latitude: float | None
    longitude: float | None


@dataclass
class AlertRecord:
    """Registro interno de una solicitud de ayuda.

    last_notified_at y notification_round controlan el reenvío y escalamiento
    (RF-24): la creación cuenta como el envío inicial (ronda 0).
    acknowledged_by es el uid de quien la atendió con H4.
    """

    id: str
    adult_uid: str
    adult_name: str
    category: AlertCategory | None
    status: AlertStatus
    created_at: datetime
    latitude: float | None
    longitude: float | None
    has_trusted_contacts: bool
    last_notified_at: datetime
    acknowledged_by: str | None = None
    notification_round: int = 0
