"""Tests de los validadores compartidos (models/validators.py), los enums nuevos y los
campos nuevos de Settings de la ronda 2 (D-R2-18)."""

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.core.config import Settings
from app.models.enums import (
    AlertCategory,
    AlertStatus,
    CallStatus,
    ReminderKind,
    ReminderRepeat,
    ReminderStatus,
    Role,
)
from app.models.validators import (
    EMAIL_PATTERN,
    blank_to_none,
    email_address,
    http_url,
    strip_text,
)


# --- email_address ---


@pytest.mark.parametrize(
    "value",
    [
        "ana@example.com",
        "Ana.Perez+familia@Example.CO",
        "rosa_gomez@correo.uqvirtual.edu.co",
        "a@b.co",
    ],
)
def test_email_address_accepts_valid(value):
    assert email_address(value) == value
    assert EMAIL_PATTERN.fullmatch(value) is not None


def test_email_address_strips_without_lowercasing():
    assert email_address("  Ana@Example.COM \t") == "Ana@Example.COM"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "sin-arroba",
        "ana@",
        "@example.com",
        "ana@example",
        "ana@@example.com",
        "ana perez@example.com",
        "ana@exa mple.com",
        "ana@.com",
        "ana@example.com\nbcc@example.com",
    ],
)
def test_email_address_rejects_invalid(value):
    with pytest.raises(ValueError, match="no es un correo válido"):
        email_address(value)


def test_email_address_keeps_none_for_optional_fields():
    assert email_address(None) is None


@pytest.mark.parametrize("value", [123, ["ana@example.com"], {"email": "a@b.co"}])
def test_email_address_rejects_non_text(value):
    with pytest.raises(ValueError):
        email_address(value)


# --- http_url ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/foto.png", "https://example.com/foto.png"),
        ("http://example.com/foto.png", "http://example.com/foto.png"),
        ("  https://example.com/a.jpg  ", "https://example.com/a.jpg"),
        (
            "https://firebasestorage.googleapis.com/v0/b/x/o/y?alt=media",
            "https://firebasestorage.googleapis.com/v0/b/x/o/y?alt=media",
        ),
    ],
)
def test_http_url_accepts_http_and_https(value, expected):
    assert http_url(value) == expected


def test_http_url_keeps_none():
    assert http_url(None) is None


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:image/png;base64,iVBORw0KGgo=",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/foto.png",
        "file:///etc/passwd",
        "//example.com/foto.png",
        "www.example.com/foto.png",
        "",
        "   ",
    ],
)
def test_http_url_rejects_other_schemes(value):
    with pytest.raises(ValueError):
        http_url(value)


def test_http_url_rejects_non_text():
    with pytest.raises(ValueError):
        http_url(42)


# --- blank_to_none y strip_text ---


@pytest.mark.parametrize("value", ["", " ", "   \t\n "])
def test_blank_to_none_converts_blank_text(value):
    assert blank_to_none(value) is None


@pytest.mark.parametrize(
    "value",
    [None, " +57 300 ", "texto", 0, False, ["", " "], {"a": ""}],
)
def test_blank_to_none_keeps_other_values(value):
    assert blank_to_none(value) == value


def test_strip_text_strips_only_text():
    assert strip_text("  Ana  ") == "Ana"
    assert strip_text("   ") == ""
    assert strip_text(None) is None
    assert strip_text(7) == 7
    payload = {"first_name": " Ana "}
    assert strip_text(payload) is payload


# --- Uso dentro de modelos Pydantic (patrón de las pistas T2 y T5) ---


class _ContactLikeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    phone: str | None = None
    email: str | None = None
    photo_url: str | None = None

    _blank_optional = field_validator("phone", "email", "photo_url", mode="before")(
        blank_to_none
    )
    _check_photo_url = field_validator("photo_url")(http_url)
    _check_email = field_validator("email")(email_address)


class _RegisterLikeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    first_name: str
    email: str
    password: str
    address: str | None = None

    _strip_names = field_validator("first_name", "address", mode="before")(strip_text)
    _blank_address = field_validator("address", mode="before")(blank_to_none)
    _check_email = field_validator("email")(email_address)


def test_validators_in_model_convert_blank_to_none():
    model = _ContactLikeRequest.model_validate(
        {"phone": "   ", "email": "", "photo_url": " "}
    )

    assert model.phone is None
    assert model.email is None
    assert model.photo_url is None


def test_validators_in_model_accept_valid_values_and_null():
    model = _ContactLikeRequest.model_validate(
        {
            "phone": " +57 300 123 ",
            "email": " Ana@Example.com ",
            "photo_url": " https://example.com/a.png ",
        }
    )

    assert model.phone == "+57 300 123"
    assert model.email == "Ana@Example.com"
    assert model.photo_url == "https://example.com/a.png"

    cleared = _ContactLikeRequest.model_validate(
        {"phone": None, "email": None, "photo_url": None}
    )
    assert cleared.model_dump(exclude_unset=True) == {
        "phone": None,
        "email": None,
        "photo_url": None,
    }


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"photo_url": "javascript:alert(1)"}, "photo_url"),
        ({"photo_url": "data:image/png;base64,AAAA"}, "photo_url"),
        ({"email": "no-es-correo"}, "email"),
    ],
)
def test_validators_in_model_raise_value_error(payload, field):
    with pytest.raises(ValidationError) as exc_info:
        _ContactLikeRequest.model_validate(payload)

    errors = exc_info.value.errors()
    assert [(error["type"], error["loc"]) for error in errors] == [
        ("value_error", (field,))
    ]


def test_strip_text_never_touches_password():
    model = _RegisterLikeRequest.model_validate(
        {
            "first_name": "  Ana ",
            "email": " ana@example.com ",
            "password": "  clave con espacios  ",
            "address": "   ",
        }
    )

    assert model.first_name == "Ana"
    assert model.email == "ana@example.com"
    assert model.password == "  clave con espacios  "
    assert model.address is None


# --- Integración con el handler 422 de main.py (D-03) ---


class _PhotoRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    email: str
    photo_url: str | None = None

    _check_photo_url = field_validator("photo_url")(http_url)
    _check_email = field_validator("email")(email_address)


@pytest.fixture
def validation_client() -> TestClient:
    from app.main import validation_exception_handler

    test_app = FastAPI()
    test_app.add_exception_handler(RequestValidationError, validation_exception_handler)

    @test_app.post("/photo")
    def photo_route(payload: _PhotoRequest) -> dict:
        return payload.model_dump()

    return TestClient(test_app)


@pytest.mark.parametrize(
    "photo_url",
    ["javascript:alert(document.cookie)", "data:text/html,<b>x</b>"],
)
def test_handler_reports_invalid_photo_url(validation_client, photo_url):
    response = validation_client.post(
        "/photo", json={"email": "ana@example.com", "photo_url": photo_url}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: photo_url: no es válido"}


def test_handler_reports_invalid_email_without_leaking_value(validation_client):
    response = validation_client.post("/photo", json={"email": "secreto-sin-arroba"})

    assert response.status_code == 422
    assert response.json() == {"detail": "Datos inválidos: email: no es válido"}
    assert "secreto-sin-arroba" not in response.text
    assert "correo" not in response.text


def test_handler_accepts_valid_payload(validation_client):
    response = validation_client.post(
        "/photo",
        json={"email": " ana@example.com ", "photo_url": "https://example.com/a.png"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "email": "ana@example.com",
        "photo_url": "https://example.com/a.png",
    }


# --- Enums del contrato (valores literales, sección 6) ---


def test_new_enums_have_contract_values():
    assert [member.value for member in AlertStatus] == [
        "pendiente",
        "en_atencion",
        "atendida",
    ]
    assert [member.value for member in AlertCategory] == [
        "ayuda_en_casa",
        "compania",
        "problema",
        "otro",
    ]
    assert [member.value for member in ReminderKind] == ["cita_medica", "medicamento"]
    assert [member.value for member in ReminderRepeat] == [
        "ninguna",
        "diaria",
        "semanal",
    ]
    assert [member.value for member in ReminderStatus] == [
        "pendiente",
        "confirmado",
        "omitido",
    ]
    assert AlertStatus.EN_ATENCION == "en_atencion"
    assert AlertCategory.COMPANIA == "compania"
    assert ReminderKind.CITA_MEDICA == "cita_medica"


def test_existing_enums_unchanged():
    assert [member.value for member in Role] == [
        "adulto_mayor",
        "familiar",
        "cuidador",
        "administrador",
    ]
    assert [member.value for member in CallStatus] == [
        "completada",
        "no_contestada",
        "cancelada",
    ]


# --- Settings nuevos (D-R2-18): default seguro para arrancar sin ellos ---


_NEW_SETTINGS_ENV = (
    "ALERT_ESCALATION_SECONDS",
    "REMINDER_MISSED_MINUTES",
    "VOICE_MESSAGES_DIR",
    "VOICE_MESSAGE_MAX_BYTES",
    "VOICE_MESSAGE_SIGNING_KEY",
)


def test_settings_new_fields_have_safe_defaults(monkeypatch):
    for name in _NEW_SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.alert_escalation_seconds == 120
    assert settings.reminder_missed_minutes == 15
    assert settings.voice_messages_dir == ""
    assert settings.voice_message_max_bytes == 5_242_880
    assert settings.voice_message_signing_key == ""


def test_settings_new_fields_read_environment(monkeypatch):
    monkeypatch.setenv("ALERT_ESCALATION_SECONDS", "60")
    monkeypatch.setenv("REMINDER_MISSED_MINUTES", "5")
    monkeypatch.setenv("VOICE_MESSAGES_DIR", "C:/tmp/voz")
    monkeypatch.setenv("VOICE_MESSAGE_MAX_BYTES", "1024")
    monkeypatch.setenv("VOICE_MESSAGE_SIGNING_KEY", "clave-de-prueba")

    settings = Settings(_env_file=None)

    assert settings.alert_escalation_seconds == 60
    assert settings.reminder_missed_minutes == 5
    assert settings.voice_messages_dir == "C:/tmp/voz"
    assert settings.voice_message_max_bytes == 1024
    assert settings.voice_message_signing_key == "clave-de-prueba"
