import threading
from dataclasses import replace
from typing import Protocol

from app.models.messages import MessageRecord


class MessageRepository(Protocol):
    """Acceso a los mensajes de texto entre cada adulto mayor y sus contactos (ADR-05)."""

    def add(self, record: MessageRecord) -> MessageRecord: ...

    def list_conversation(
        self, adult_uid: str, contact_id: str
    ) -> list[MessageRecord]: ...


class InMemoryMessageRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias para que modificar un MessageRecord recibido no altere lo
    guardado. Todos los métodos toman el mismo lock porque las rutas def corren en el
    threadpool; las lecturas recorren copias.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (adult_uid, contact_id) -> mensajes en orden de inserción.
        self._conversations: dict[tuple[str, str], list[MessageRecord]] = {}

    def add(self, record: MessageRecord) -> MessageRecord:
        stored = replace(record)
        with self._lock:
            self._conversations.setdefault(
                (stored.adult_uid, stored.contact_id), []
            ).append(stored)
        return replace(stored)

    def list_conversation(
        self, adult_uid: str, contact_id: str
    ) -> list[MessageRecord]:
        """Mensajes de la conversación por created_at ascendente.

        sorted es estable: con el mismo created_at se conserva el orden de inserción.
        """
        with self._lock:
            records = list(self._conversations.get((adult_uid, contact_id), []))
        ordered = sorted(records, key=lambda record: record.created_at)
        return [replace(record) for record in ordered]


_default_message_repository = InMemoryMessageRepository()


def get_message_repository() -> MessageRepository:
    return _default_message_repository
