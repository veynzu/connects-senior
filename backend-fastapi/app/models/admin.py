"""Esquemas de administración de usuarios (B1, B2; contrato 6.7, RF-06)."""
from pydantic import BaseModel, ConfigDict

from app.models.enums import Role


class AdminUserResponse(BaseModel):
    """Usuario tal como lo devuelven B1 y B2: { uid, email, display_name, role, disabled }.

    role es None si el claim falta o no es uno de los cuatro valores del contrato (D-02.4).
    """

    uid: str
    email: str | None
    display_name: str | None
    role: Role | None
    disabled: bool


class AdminUserUpdateRequest(BaseModel):
    """Cuerpo de B2: disabled (verdadero o falso)."""

    model_config = ConfigDict(extra="ignore")

    disabled: bool
