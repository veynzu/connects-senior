from datetime import date
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import Role
from app.models.users import CaregiverPermissions
from app.models.validators import blank_to_none, email_address, http_url, strip_text


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=6, max_length=4096)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    refresh_token: str = Field(min_length=1, max_length=4096)


class AuthenticatedUser(BaseModel):
    uid: str
    email: str | None = None
    email_verified: bool | None = None
    display_name: str | None = None
    photo_url: str | None = None
    role: Role | None = None


class LoginResponse(BaseModel):
    id_token: str
    refresh_token: str
    expires_in: int
    user: AuthenticatedUser


# --- Tareas 5 y 8: registro, adultos, cuidadores, contraseña, perfil y dispositivos ---


def _blank_or_stripped(value: object) -> object:
    """Texto opcional: "" o solo espacios -> None; si no, strip (mode="before")."""
    return strip_text(blank_to_none(value))


def _http_url_with_host(value: str | None) -> str | None:
    """http_url más un host no vacío: Firebase rechaza una photo_url sin netloc."""
    candidate = http_url(value)
    if candidate is not None and not urlparse(candidate).netloc:
        raise ValueError("la URL no tiene host")
    return candidate


class RegisterRequest(BaseModel):
    """A4. Sin str_strip_whitespace: la contraseña nunca se recorta (D-R2-14)."""

    model_config = ConfigDict(extra="ignore")

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=6, max_length=4096)

    _strip_names = field_validator("first_name", "last_name", mode="before")(strip_text)
    _check_email = field_validator("email")(email_address)


class AdultCreateRequest(BaseModel):
    """A7. Igual que RegisterRequest más los datos opcionales del adulto mayor."""

    model_config = ConfigDict(extra="ignore")

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=6, max_length=4096)
    phone: str | None = Field(default=None, max_length=50)
    birth_date: date | None = None
    address: str | None = Field(default=None, max_length=300)
    photo_url: str | None = None

    _strip_names = field_validator("first_name", "last_name", mode="before")(strip_text)
    _check_email = field_validator("email")(email_address)
    _blank_optional = field_validator("phone", "address", mode="before")(
        _blank_or_stripped
    )
    _blank_photo_url = field_validator("photo_url", mode="before")(blank_to_none)
    _check_photo_url = field_validator("photo_url")(http_url)


class AdultResponse(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str
    phone: str | None = None
    birth_date: date | None = None
    address: str | None = None
    photo_url: str | None = None


class CaregiverCreateRequest(BaseModel):
    """A9."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    permissions: CaregiverPermissions

    _check_email = field_validator("email")(email_address)


class CaregiverResponse(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str
    permissions: CaregiverPermissions


class PasswordResetRequest(BaseModel):
    """A5."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)

    _check_email = field_validator("email")(email_address)


class ProfileUpdateRequest(BaseModel):
    """A6. Todos opcionales; el servicio usa model_dump(exclude_unset=True)."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=50)
    photo_url: str | None = None

    _blank_optional = field_validator("phone", "photo_url", mode="before")(blank_to_none)
    _check_photo_url = field_validator("photo_url")(_http_url_with_host)


class DeviceRegisterRequest(BaseModel):
    """A10."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    fcm_token: str = Field(min_length=1, max_length=4096)
