"""Validadores compartidos por los modelos Pydantic de entrada.

Son funciones puras para usar con field_validator, por ejemplo
`_photo_url = field_validator("photo_url")(http_url)` o
`_blank = field_validator("phone", mode="before")(blank_to_none)`.
Un ValueError se traduce en el handler 422 de main.py a
"Datos inválidos: <campo>: no es válido"; el mensaje del ValueError nunca llega al cliente.
"""

import re


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HTTP_URL_PREFIXES = ("http://", "https://")


def email_address(value: str | None) -> str | None:
    """Comprueba el formato del correo y lo devuelve con strip (sin pasarlo a minúsculas).

    La normalización (strip + lower) la hacen los repositorios con normalize_email.
    None se devuelve tal cual para que un campo opcional pueda borrarse con null; un campo
    obligatorio ya rechaza None por su tipo antes de llegar aquí.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("no es un correo válido")
    candidate = value.strip()
    if EMAIL_PATTERN.fullmatch(candidate) is None:
        raise ValueError("no es un correo válido")
    return candidate


def http_url(value: str | None) -> str | None:
    """Exige el prefijo http:// o https:// (rechaza javascript:, data: y cualquier otro)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("no es una URL http o https")
    candidate = value.strip()
    if not candidate.startswith(_HTTP_URL_PREFIXES):
        raise ValueError("no es una URL http o https")
    return candidate


def blank_to_none(value: object) -> object:
    """Convierte un texto vacío o con solo espacios en None; lo demás queda igual.

    Pensado para field_validator(mode="before") en campos opcionales que un formulario
    puede enviar como "".
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def strip_text(value: object) -> object:
    """Aplica strip a los textos; lo demás queda igual.

    Para los Requests que no usan str_strip_whitespace porque llevan una contraseña,
    que nunca se recorta.
    """
    if isinstance(value, str):
        return value.strip()
    return value
