"""Modelos de los mensajes de texto (M1, M2) bajo /api/messages (CLAUDE.md 6.4, D-R2-12)."""
from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


MESSAGE_TEXT_MAX_LENGTH = 2000


class MessageCreateRequest(BaseModel):
    """Entrada de M1 (POST /api/messages).

    text se recorta antes de validar: vacío o solo espacios -> 422.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    contact_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=MESSAGE_TEXT_MAX_LENGTH)


class MessageCreateResponse(BaseModel):
    """Salida de M1: 201 { id, created_at }."""

    id: str
    created_at: AwareDatetime


class MessageResponse(BaseModel):
    """Elemento de la conversación de M2: { id, from_me, text, created_at }."""

    id: str
    from_me: bool
    text: str
    created_at: AwareDatetime


@dataclass
class MessageRecord:
    """Registro interno de un mensaje; nunca se devuelve tal cual en la API.

    La conversación es siempre entre un adulto mayor y uno de sus contactos:
    from_adult es True si lo envió el adulto y False si lo envió el contacto.
    """

    id: str
    adult_uid: str
    contact_id: str
    text: str
    created_at: datetime
    from_adult: bool
