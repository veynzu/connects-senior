import threading
from typing import Protocol

from app.models.contacts import Contact


class ContactRepository(Protocol):
    """Acceso a los contactos de cada adulto mayor (ADR-05)."""

    def get(self, adult_uid: str, contact_id: str) -> Contact | None: ...

    def list_for_adult(self, adult_uid: str) -> list[Contact]: ...

    def add(self, adult_uid: str, contact: Contact) -> Contact: ...

    def update(self, adult_uid: str, contact: Contact) -> Contact | None: ...

    def delete(self, adult_uid: str, contact_id: str) -> bool: ...

    def find(self, contact_id: str) -> tuple[str, Contact] | None: ...


class InMemoryContactRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias para que modificar un Contact recibido no altere lo guardado.
    Todos los métodos toman el mismo lock porque el planificador lee desde el threadpool;
    las iteraciones recorren copias.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contacts: dict[str, dict[str, Contact]] = {}

    def get(self, adult_uid: str, contact_id: str) -> Contact | None:
        with self._lock:
            contact = self._contacts.get(adult_uid, {}).get(contact_id)
            if contact is None:
                return None
            return contact.model_copy(deep=True)

    def list_for_adult(self, adult_uid: str) -> list[Contact]:
        with self._lock:
            return [
                contact.model_copy(deep=True)
                for contact in list(self._contacts.get(adult_uid, {}).values())
            ]

    def add(self, adult_uid: str, contact: Contact) -> Contact:
        stored = contact.model_copy(deep=True)
        with self._lock:
            self._contacts.setdefault(adult_uid, {})[stored.id] = stored
            return stored.model_copy(deep=True)

    def update(self, adult_uid: str, contact: Contact) -> Contact | None:
        """Reemplaza completo el contacto con el mismo id; None si no existe para ese adulto."""
        stored = contact.model_copy(deep=True)
        with self._lock:
            adult_contacts = self._contacts.get(adult_uid)
            if adult_contacts is None or stored.id not in adult_contacts:
                return None
            adult_contacts[stored.id] = stored
            return stored.model_copy(deep=True)

    def delete(self, adult_uid: str, contact_id: str) -> bool:
        with self._lock:
            adult_contacts = self._contacts.get(adult_uid)
            if adult_contacts is None or contact_id not in adult_contacts:
                return False
            del adult_contacts[contact_id]
            return True

    def find(self, contact_id: str) -> tuple[str, Contact] | None:
        """Busca el contacto en todos los adultos y devuelve (adult_uid, contacto)."""
        with self._lock:
            for adult_uid, adult_contacts in list(self._contacts.items()):
                contact = adult_contacts.get(contact_id)
                if contact is not None:
                    return adult_uid, contact.model_copy(deep=True)
        return None


_default_contact_repository = InMemoryContactRepository()


def get_contact_repository() -> ContactRepository:
    return _default_contact_repository
