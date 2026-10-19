"""Repositorio de recordatorios (ADR-05, D-R2-11, D-R2-23)."""
import threading
from dataclasses import replace
from typing import Protocol

from app.models.enums import ReminderStatus
from app.models.reminders import ReminderRecord


class ReminderRepository(Protocol):
    """Acceso a los recordatorios de todos los adultos mayores."""

    def add(self, record: ReminderRecord) -> ReminderRecord: ...

    def get(self, reminder_id: str) -> ReminderRecord | None: ...

    def update(self, record: ReminderRecord) -> ReminderRecord | None: ...

    def update_if_status(
        self, record: ReminderRecord, expected_status: ReminderStatus
    ) -> ReminderRecord | None: ...

    def delete(self, reminder_id: str) -> bool: ...

    def list_for_adult(self, adult_uid: str) -> list[ReminderRecord]: ...

    def list_pending(self) -> list[ReminderRecord]: ...


class InMemoryReminderRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias. Todos los métodos toman el mismo lock porque el
    planificador (threadpool) lee y escribe mientras las rutas `def` escriben; las
    iteraciones recorren copias. Un contador de inserción desempata los listados.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # id -> (orden de inserción, registro)
        self._records: dict[str, tuple[int, ReminderRecord]] = {}
        self._next_sequence = 0

    def add(self, record: ReminderRecord) -> ReminderRecord:
        stored = replace(record)
        with self._lock:
            current = self._records.get(stored.id)
            if current is None:
                sequence = self._next_sequence
                self._next_sequence += 1
            else:
                sequence = current[0]
            self._records[stored.id] = (sequence, stored)
        return replace(stored)

    def get(self, reminder_id: str) -> ReminderRecord | None:
        with self._lock:
            current = self._records.get(reminder_id)
            return None if current is None else replace(current[1])

    def update(self, record: ReminderRecord) -> ReminderRecord | None:
        """Reemplaza el registro con el mismo id; None si no existe."""
        stored = replace(record)
        with self._lock:
            current = self._records.get(stored.id)
            if current is None:
                return None
            self._records[stored.id] = (current[0], stored)
        return replace(stored)

    def update_if_status(
        self, record: ReminderRecord, expected_status: ReminderStatus
    ) -> ReminderRecord | None:
        """Reemplaza solo si el registro guardado tiene expected_status (atómico).

        Si no existe o su estado cambió, devuelve None sin tocar nada.
        """
        stored = replace(record)
        with self._lock:
            current = self._records.get(stored.id)
            if current is None or current[1].status != expected_status:
                return None
            self._records[stored.id] = (current[0], stored)
        return replace(stored)

    def delete(self, reminder_id: str) -> bool:
        with self._lock:
            return self._records.pop(reminder_id, None) is not None

    def list_for_adult(self, adult_uid: str) -> list[ReminderRecord]:
        """scheduled_at ascendente; en empate, orden de inserción."""
        with self._lock:
            entries = list(self._records.values())
        selected = [entry for entry in entries if entry[1].adult_uid == adult_uid]
        selected.sort(key=lambda entry: (entry[1].scheduled_at, entry[0]))
        return [replace(record) for _, record in selected]

    def list_pending(self) -> list[ReminderRecord]:
        """Recordatorios pendientes de todos los adultos, scheduled_at ascendente."""
        with self._lock:
            entries = list(self._records.values())
        selected = [
            entry for entry in entries if entry[1].status == ReminderStatus.PENDIENTE
        ]
        selected.sort(key=lambda entry: (entry[1].scheduled_at, entry[0]))
        return [replace(record) for _, record in selected]


_default_reminder_repository = InMemoryReminderRepository()


def get_reminder_repository() -> ReminderRepository:
    return _default_reminder_repository
