from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.validators import blank_to_none, email_address, http_url


class ContactPermissions(BaseModel):
    calls: bool
    video_calls: bool
    messages: bool
    help_alerts: bool


class Contact(BaseModel):
    """Contacto de un adulto mayor con la forma literal del contrato (sección 3)."""

    id: str
    first_name: str
    last_name: str
    relationship: str
    phone: str | None = None
    email: str | None = None
    photo_url: str | None = None
    is_favorite: bool = False
    favorite_order: int = 0
    permissions: ContactPermissions


def _reject_explicit_null(value: object) -> object:
    """En un PATCH, null en un campo obligatorio del contacto es un dato inválido (422)."""
    if value is None:
        raise ValueError("no es válido")
    return value


class ContactCreateRequest(BaseModel):
    """Entrada de C2 (POST /api/contacts).

    adult_id solo lo envían familiar y cuidador (regla del adulto implícito).
    phone, email y photo_url admiten null; "" o solo espacios cuentan como null.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    adult_id: str | None = None
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    # relacion es varchar(100) en adulto_contacto (esquema V004, plan ronda 3, D-R3-26(d)).
    relationship: str = Field(min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=320)
    photo_url: str | None = None
    permissions: ContactPermissions

    _blank_optional = field_validator("phone", "email", "photo_url", mode="before")(
        blank_to_none
    )
    _check_photo_url = field_validator("photo_url")(http_url)
    _check_email = field_validator("email")(email_address)


class ContactUpdateRequest(BaseModel):
    """Entrada de C3 (PATCH /api/contacts/{id}): todos los campos opcionales.

    adult_id no es entrada de una ruta con {id}: no se declara y, si llega, se ignora.
    null explícito en first_name, last_name, relationship, permissions, is_favorite o
    favorite_order -> 422; null en phone, email o photo_url borra el valor.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    relationship: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=320)
    photo_url: str | None = None
    permissions: ContactPermissions | None = None
    is_favorite: bool | None = None
    # orden_favorito es integer (int4) en adulto_contacto (plan ronda 3, D-R3-7).
    favorite_order: int | None = Field(default=None, ge=0, le=2147483647)

    _not_null = field_validator(
        "first_name",
        "last_name",
        "relationship",
        "permissions",
        "is_favorite",
        "favorite_order",
        mode="before",
    )(_reject_explicit_null)
    _blank_optional = field_validator("phone", "email", "photo_url", mode="before")(
        blank_to_none
    )
    _check_photo_url = field_validator("photo_url")(http_url)
    _check_email = field_validator("email")(email_address)
