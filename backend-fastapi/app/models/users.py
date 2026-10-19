"""Perfiles de usuario y vínculos con los adultos mayores (D-R2-2, D-R2-8).

El rol del perfil es espejo del custom claim de Firebase (D-02.1): la fuente de verdad
sigue siendo el token verificado.
"""

from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel

from app.models.enums import Role


def normalize_email(email: str) -> str:
    """Forma canónica de un correo para compararlo: strip y minúsculas."""
    return email.strip().lower()


class CaregiverPermissions(BaseModel):
    """Permisos del cuidador sobre un adulto (A9), con los booleanos exactos del contrato."""

    manage_contacts: bool
    manage_reminders: bool
    receive_alerts: bool


NO_CAREGIVER_PERMISSIONS = CaregiverPermissions(
    manage_contacts=False,
    manage_reminders=False,
    receive_alerts=False,
)


@dataclass
class UserProfile:
    """Perfil del usuario; role es None si no tiene rol reflejado (D-R3-5)."""

    uid: str
    first_name: str
    last_name: str
    email: str
    role: Role | None
    phone: str | None = None
    birth_date: date | None = None
    address: str | None = None
    photo_url: str | None = None
    email_verified: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class AdultLink:
    """Vínculo de un familiar o cuidador con un adulto mayor.

    Para un cuidador permissions nunca es None; para un familiar siempre es None.
    """

    user_uid: str
    adult_uid: str
    role: Role
    permissions: CaregiverPermissions | None = None
