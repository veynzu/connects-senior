import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from app.models.voice_messages import VoiceMessageRecord


class VoiceMessageRepository(Protocol):
    """Metadatos de los mensajes de voz (ADR-05, ADR-08): el audio vive en disco."""

    def add(self, record: VoiceMessageRecord) -> VoiceMessageRecord: ...

    def get(self, voice_message_id: str) -> VoiceMessageRecord | None: ...

    def list_received_by_adult(self, adult_uid: str) -> list[VoiceMessageRecord]: ...

    def list_sent_to_contact(
        self, adult_uid: str, contact_id: str
    ) -> list[VoiceMessageRecord]: ...


class InMemoryVoiceMessageRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias. Las listas van del más reciente al más antiguo; con el
    mismo created_at va primero el último insertado (ascendente estable por created_at
    y después invertido), para lo que se guarda un contador de inserción.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        # voice_message_id -> (orden de inserción, registro)
        self._records: dict[str, tuple[int, VoiceMessageRecord]] = {}

    def add(self, record: VoiceMessageRecord) -> VoiceMessageRecord:
        stored = replace(record)
        with self._lock:
            self._sequence += 1
            self._records[stored.id] = (self._sequence, stored)
        return replace(stored)

    def get(self, voice_message_id: str) -> VoiceMessageRecord | None:
        with self._lock:
            entry = self._records.get(voice_message_id)
        return None if entry is None else replace(entry[1])

    def list_received_by_adult(self, adult_uid: str) -> list[VoiceMessageRecord]:
        """Mensajes que los contactos enviaron a ese adulto (from_adult False)."""
        return self._newest_first(
            lambda record: record.adult_uid == adult_uid and not record.from_adult
        )

    def list_sent_to_contact(
        self, adult_uid: str, contact_id: str
    ) -> list[VoiceMessageRecord]:
        """Mensajes que ese adulto envió a ese contacto (from_adult True)."""
        return self._newest_first(
            lambda record: record.adult_uid == adult_uid
            and record.contact_id == contact_id
            and record.from_adult
        )

    def _newest_first(
        self, predicate: Callable[[VoiceMessageRecord], bool]
    ) -> list[VoiceMessageRecord]:
        with self._lock:
            entries = list(self._records.values())
        selected = [entry for entry in entries if predicate(entry[1])]
        selected.sort(key=lambda entry: (entry[1].created_at, entry[0]), reverse=True)
        return [replace(record) for _, record in selected]


_default_voice_message_repository = InMemoryVoiceMessageRepository()


def get_voice_message_repository() -> VoiceMessageRepository:
    return _default_voice_message_repository
