"""Repositorio de solicitudes de ayuda (H1-H5, ADR-05, D-R2-10, D-R2-23)."""
import itertools
import threading
from dataclasses import replace
from typing import Protocol

from app.models.alerts import AlertRecord
from app.models.enums import AlertStatus


class AlertRepository(Protocol):
    """Acceso a las solicitudes de ayuda de todos los adultos mayores."""

    def add(self, record: AlertRecord) -> AlertRecord: ...

    def get(self, alert_id: str) -> AlertRecord | None: ...

    def update(self, record: AlertRecord) -> AlertRecord | None: ...

    def update_if_status(
        self, record: AlertRecord, expected_status: AlertStatus
    ) -> AlertRecord | None: ...

    def list_for_adult(self, adult_uid: str) -> list[AlertRecord]: ...

    def list_pending(self) -> list[AlertRecord]: ...


class InMemoryAlertRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias. Todos los métodos toman el mismo lock porque el tick
    de escalamiento escribe desde el threadpool mientras las rutas leen y escriben;
    las iteraciones recorren copias. Un contador de inserción desempata las
    solicitudes creadas en el mismo segundo.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._alerts: dict[str, AlertRecord] = {}
        self._insertion_order: dict[str, int] = {}
        self._counter = itertools.count()

    def add(self, record: AlertRecord) -> AlertRecord:
        stored = replace(record)
        with self._lock:
            self._alerts[stored.id] = stored
            if stored.id not in self._insertion_order:
                self._insertion_order[stored.id] = next(self._counter)
        return replace(stored)

    def get(self, alert_id: str) -> AlertRecord | None:
        """Busca por id en todos los adultos; el servicio comprueba el alcance."""
        with self._lock:
            record = self._alerts.get(alert_id)
            return None if record is None else replace(record)

    def update(self, record: AlertRecord) -> AlertRecord | None:
        """Reemplaza el registro con el mismo id; None si no existe."""
        stored = replace(record)
        with self._lock:
            if stored.id not in self._alerts:
                return None
            self._alerts[stored.id] = stored
        return replace(stored)

    def update_if_status(
        self, record: AlertRecord, expected_status: AlertStatus
    ) -> AlertRecord | None:
        """Reemplaza el registro solo si el guardado tiene expected_status (atómico).

        Si no existe o su estado cambió (por ejemplo, alguien lo atendió), devuelve
        None sin tocar nada.
        """
        stored = replace(record)
        with self._lock:
            current = self._alerts.get(stored.id)
            if current is None or current.status != expected_status:
                return None
            self._alerts[stored.id] = stored
        return replace(stored)

    def list_for_adult(self, adult_uid: str) -> list[AlertRecord]:
        """Del más reciente al más antiguo.

        Se ordena ascendente y estable por created_at (empate: orden de inserción) y
        después se invierte: en un empate va primero la última insertada.
        """
        with self._lock:
            records = [
                (self._insertion_order[record.id], record)
                for record in list(self._alerts.values())
                if record.adult_uid == adult_uid
            ]
        records.sort(key=lambda item: (item[1].created_at, item[0]))
        return [replace(record) for _, record in reversed(records)]

    def list_pending(self) -> list[AlertRecord]:
        """Solicitudes en estado pendiente de todos los adultos, en orden de inserción."""
        with self._lock:
            records = list(self._alerts.values())
        return [
            replace(record)
            for record in records
            if record.status == AlertStatus.PENDIENTE
        ]


_default_alert_repository = InMemoryAlertRepository()


def get_alert_repository() -> AlertRepository:
    return _default_alert_repository
