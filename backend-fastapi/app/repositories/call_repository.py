from dataclasses import replace
from typing import Protocol

from app.models.calls import CallRecord
from app.models.enums import CallStatus


class CallRepository(Protocol):
    """Acceso a las llamadas de cada adulto mayor (ADR-05)."""

    def add(self, record: CallRecord) -> CallRecord: ...

    def get(self, adult_uid: str, call_id: str) -> CallRecord | None: ...

    def update_status(
        self, adult_uid: str, call_id: str, status: CallStatus
    ) -> CallRecord | None: ...

    def list_for_adult(self, adult_uid: str) -> list[CallRecord]: ...


class InMemoryCallRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias para que modificar un CallRecord recibido no altere lo guardado.
    """

    def __init__(self) -> None:
        self._calls: dict[str, dict[str, CallRecord]] = {}

    def add(self, record: CallRecord) -> CallRecord:
        stored = replace(record)
        self._calls.setdefault(stored.adult_uid, {})[stored.id] = stored
        return replace(stored)

    def get(self, adult_uid: str, call_id: str) -> CallRecord | None:
        record = self._calls.get(adult_uid, {}).get(call_id)
        if record is None:
            return None
        return replace(record)

    def update_status(
        self, adult_uid: str, call_id: str, status: CallStatus
    ) -> CallRecord | None:
        adult_calls = self._calls.get(adult_uid, {})
        record = adult_calls.get(call_id)
        if record is None:
            return None
        updated = replace(record, status=status)
        adult_calls[call_id] = updated
        return replace(updated)

    def list_for_adult(self, adult_uid: str) -> list[CallRecord]:
        # sorted es estable: con el mismo started_at se conserva el orden de inserción.
        records = sorted(
            self._calls.get(adult_uid, {}).values(),
            key=lambda record: record.started_at,
            reverse=True,
        )
        return [replace(record) for record in records]


_default_call_repository = InMemoryCallRepository()


def get_call_repository() -> CallRepository:
    return _default_call_repository
