"""Modelos de los mensajes de voz (V1, V2; RF-21, ADR-08).

Los Response tienen la forma literal del contrato (sección 6.3). VoiceMessageRecord es
el registro interno: solo guarda la referencia al archivo y sus metadatos, nunca el
audio (ADR-08).
"""
from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, BaseModel


class VoiceMessageCreateResponse(BaseModel):
    """Salida de V1 (POST /api/calls/voice-messages, 201)."""

    id: str
    created_at: AwareDatetime


class VoiceMessageResponse(BaseModel):
    """Elemento de V2 (GET /api/calls/voice-messages): un mensaje recibido."""

    id: str
    from_name: str
    created_at: AwareDatetime
    audio_url: str


@dataclass
class VoiceMessageRecord:
    """Registro interno de un mensaje de voz; nunca se devuelve tal cual en la API.

    from_adult es True si lo envió el adulto mayor al contacto y False si lo envió el
    contacto (familiar o cuidador vinculado por email verificado, P19) al adulto.
    file_name es el nombre generado por el backend (uuid4) dentro del directorio de
    mensajes de voz; content_type es el tipo de medio ya normalizado ("audio/webm").
    """

    id: str
    adult_uid: str
    contact_id: str
    from_adult: bool
    from_name: str
    file_name: str
    content_type: str
    size_bytes: int
    created_at: datetime
