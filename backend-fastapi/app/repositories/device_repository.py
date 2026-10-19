import threading
from collections.abc import Iterable
from typing import Protocol


class DeviceRepository(Protocol):
    """Tokens FCM de los navegadores de cada usuario (A10, ADR-07, D-R2-4)."""

    def register(self, uid: str, fcm_token: str) -> None: ...

    def tokens_of(self, uids: Iterable[str]) -> list[str]: ...

    def remove_tokens(self, tokens: Iterable[str]) -> None: ...

    def has_device(self, uid: str) -> bool: ...


class InMemoryDeviceRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Un token pertenece a un solo uid: registrarlo con otro uid lo mueve. Todos los métodos
    toman el mismo lock porque el planificador lee desde el threadpool.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # fcm_token -> uid; el dict conserva el orden de inserción.
        self._owners: dict[str, str] = {}

    def register(self, uid: str, fcm_token: str) -> None:
        """Idempotente para el mismo uid; si el token era de otro uid, pasa a este."""
        with self._lock:
            current_owner = self._owners.get(fcm_token)
            if current_owner == uid:
                return
            if current_owner is not None:
                del self._owners[fcm_token]
            self._owners[fcm_token] = uid

    def tokens_of(self, uids: Iterable[str]) -> list[str]:
        """Tokens de esos uids en orden de inserción, sin duplicados; uids desconocidos se ignoran."""
        wanted = set(uids)
        with self._lock:
            owners = list(self._owners.items())
        return [token for token, owner in owners if owner in wanted]

    def remove_tokens(self, tokens: Iterable[str]) -> None:
        # El iterable se materializa fuera del lock por si lo produce este mismo repositorio.
        token_list = list(tokens)
        with self._lock:
            for token in token_list:
                self._owners.pop(token, None)

    def has_device(self, uid: str) -> bool:
        with self._lock:
            return any(owner == uid for owner in list(self._owners.values()))


_default_device_repository = InMemoryDeviceRepository()


def get_device_repository() -> DeviceRepository:
    return _default_device_repository
