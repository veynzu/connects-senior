import threading
from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Protocol

from app.models.enums import Role
from app.models.users import (
    NO_CAREGIVER_PERMISSIONS,
    AdultLink,
    CaregiverPermissions,
    UserProfile,
    normalize_email,
)


class UserRepository(Protocol):
    """Perfiles de usuario, correos verificados y vínculos con los adultos mayores (ADR-05)."""

    def add_profile(self, profile: UserProfile) -> UserProfile: ...

    def get_profile(self, uid: str) -> UserProfile | None: ...

    def update_profile(
        self, uid: str, changes: Mapping[str, object]
    ) -> UserProfile | None: ...

    def mark_email_verified(self, uid: str, email: str) -> None: ...

    def find_uid_by_verified_email(self, email: str) -> str | None: ...

    def link_adult(
        self,
        user_uid: str,
        adult_uid: str,
        role: Role,
        permissions: CaregiverPermissions | None = None,
    ) -> None: ...

    def is_linked(self, user_uid: str, adult_uid: str) -> bool: ...

    def adults_of(self, user_uid: str) -> list[str]: ...

    def link_of(self, user_uid: str, adult_uid: str) -> AdultLink | None: ...

    def caregivers_of(self, adult_uid: str) -> list[AdultLink]: ...

    def responsible_family_of(self, adult_uid: str) -> str | None: ...


# uid identifica el perfil: no se cambia con update_profile.
_UPDATABLE_PROFILE_FIELDS = frozenset(
    field.name for field in fields(UserProfile) if field.name != "uid"
)


def _copy_permissions(permissions: CaregiverPermissions) -> CaregiverPermissions:
    """Copia independiente, siempre de la clase CaregiverPermissions."""
    return CaregiverPermissions(
        manage_contacts=permissions.manage_contacts,
        manage_reminders=permissions.manage_reminders,
        receive_alerts=permissions.receive_alerts,
    )


def _copy_link(link: AdultLink) -> AdultLink:
    if link.permissions is None:
        return replace(link)
    return replace(link, permissions=_copy_permissions(link.permissions))


class InMemoryUserRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias. Todos los métodos toman el mismo lock porque el planificador
    lee desde el threadpool mientras las rutas escriben; las iteraciones recorren copias.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profiles: dict[str, UserProfile] = {}
        # uid -> correo verificado normalizado (D-R2-3).
        self._verified_emails: dict[str, str] = {}
        # (user_uid, adult_uid) -> vínculo; el dict conserva el orden de inserción.
        self._links: dict[tuple[str, str], AdultLink] = {}

    # --- Perfiles ---

    def add_profile(self, profile: UserProfile) -> UserProfile:
        stored = replace(profile, email=normalize_email(profile.email))
        with self._lock:
            self._profiles[stored.uid] = stored
        return replace(stored)

    def get_profile(self, uid: str) -> UserProfile | None:
        with self._lock:
            profile = self._profiles.get(uid)
            return None if profile is None else replace(profile)

    def update_profile(
        self, uid: str, changes: Mapping[str, object]
    ) -> UserProfile | None:
        valid_changes: dict[str, object] = {}
        for key, value in changes.items():
            if key not in _UPDATABLE_PROFILE_FIELDS:
                continue
            if key == "email":
                if not isinstance(value, str):
                    continue
                value = normalize_email(value)
            valid_changes[key] = value

        with self._lock:
            current = self._profiles.get(uid)
            if current is None:
                return None
            updated = replace(current, **valid_changes)
            self._profiles[uid] = updated
            return replace(updated)

    # --- Correos verificados (vínculo contacto -> usuario, P9) ---

    def mark_email_verified(self, uid: str, email: str) -> None:
        normalized = normalize_email(email)
        if not normalized:
            return
        with self._lock:
            # Un correo verificado pertenece a un solo uid: gana la marca más reciente
            # (por ejemplo, una cuenta borrada y creada de nuevo con el mismo correo).
            for other_uid, other_email in list(self._verified_emails.items()):
                if other_uid != uid and other_email == normalized:
                    del self._verified_emails[other_uid]
            self._verified_emails[uid] = normalized
            profile = self._profiles.get(uid)
            if profile is not None:
                self._profiles[uid] = replace(
                    profile, email=normalized, email_verified=True
                )

    def find_uid_by_verified_email(self, email: str) -> str | None:
        normalized = normalize_email(email)
        if not normalized:
            return None
        with self._lock:
            for uid, verified_email in list(self._verified_emails.items()):
                if verified_email == normalized:
                    return uid
        return None

    # --- Vínculos con adultos mayores ---

    def link_adult(
        self,
        user_uid: str,
        adult_uid: str,
        role: Role,
        permissions: CaregiverPermissions | None = None,
    ) -> None:
        link_role = Role(role)
        stored_permissions: CaregiverPermissions | None = None
        if link_role == Role.CUIDADOR:
            # D-R2-8: un cuidador sin permisos explícitos tiene los tres en False.
            stored_permissions = _copy_permissions(
                permissions if permissions is not None else NO_CAREGIVER_PERMISSIONS
            )
        link = AdultLink(
            user_uid=user_uid,
            adult_uid=adult_uid,
            role=link_role,
            permissions=stored_permissions,
        )
        with self._lock:
            self._links[(user_uid, adult_uid)] = link

    def is_linked(self, user_uid: str, adult_uid: str) -> bool:
        with self._lock:
            return (user_uid, adult_uid) in self._links

    def adults_of(self, user_uid: str) -> list[str]:
        with self._lock:
            links = list(self._links.values())
        return [link.adult_uid for link in links if link.user_uid == user_uid]

    def link_of(self, user_uid: str, adult_uid: str) -> AdultLink | None:
        with self._lock:
            link = self._links.get((user_uid, adult_uid))
            return None if link is None else _copy_link(link)

    def caregivers_of(self, adult_uid: str) -> list[AdultLink]:
        with self._lock:
            links = list(self._links.values())
        return [
            _copy_link(link)
            for link in links
            if link.adult_uid == adult_uid and link.role == Role.CUIDADOR
        ]

    def responsible_family_of(self, adult_uid: str) -> str | None:
        with self._lock:
            links = list(self._links.values())
        for link in links:
            if link.adult_uid == adult_uid and link.role == Role.FAMILIAR:
                return link.user_uid
        return None


_default_user_repository = InMemoryUserRepository()


def get_user_repository() -> UserRepository:
    return _default_user_repository
