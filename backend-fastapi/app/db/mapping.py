"""Traducciones entre el contrato y el esquema connects_senior (plan ronda 3).

Los nombres públicos de este módulo son exactamente los de ``__all__`` (sección 5.1 del
plan); las pistas que necesiten algo más lo definen privado en su propio módulo. Los
helpers que reciben ``conn`` (una Connection abierta por ``Database.transaction()``) usan
``sqlalchemy.text()`` con parámetros nombrados, tablas calificadas con el esquema y
respetan los CHECK del esquema real (.claude/plans/2026-10-18-esquema-neon.md).
Nunca se registran correos, uid ni otros datos personales.
"""

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.models.enums import CallStatus
from app.models.users import normalize_email


__all__ = [
    "T_USUARIO",
    "T_ROL",
    "T_PERMISO",
    "T_USUARIO_ROL",
    "T_ROL_PERMISO",
    "T_VINCULO_USUARIO",
    "T_CONTACTO",
    "T_ADULTO_CONTACTO",
    "T_IDEMPOTENCIA_OPERACION",
    "T_COMUNICACION",
    "T_MENSAJE_VOZ",
    "T_COMUNICACION_HISTORIAL_ESTADO",
    "T_SOLICITUD_AYUDA",
    "T_SOLICITUD_AYUDA_NOTIFICACION",
    "T_SOLICITUD_AYUDA_HISTORIAL_ESTADO",
    "T_MENSAJE_TEXTO",
    "T_RECORDATORIO",
    "T_RECORDATORIO_HISTORIAL_ESTADO",
    "T_DISPOSITIVO_FCM",
    "OP_CREAR_COMUNICACION",
    "OP_CREAR_SOLICITUD_AYUDA",
    "TIPO_LLAMADA_AUDIO",
    "TIPO_VIDEOLLAMADA",
    "TIPO_MENSAJE_VOZ",
    "CALL_STATUS_TO_DB",
    "call_status_from_db",
    "tipo_for_audio_only",
    "audio_only_from_tipo",
    "location_state",
    "offset_text",
    "tzinfo_from_text",
    "as_utc",
    "utc_now",
    "parse_id",
    "id_str",
    "daily_room_url",
    "user_id_for_uid",
    "uid_for_user_id",
    "ensure_user_id",
    "user_full_name",
    "release_email",
    "new_idempotency",
    "contact_user_id",
]


logger = logging.getLogger(__name__)

_SCHEMA = "connects_senior"

# --- Tablas (siempre calificadas: el search_path del usuario de Neon no incluye el esquema) ---
T_USUARIO = f"{_SCHEMA}.usuario"
T_ROL = f"{_SCHEMA}.rol"
T_PERMISO = f"{_SCHEMA}.permiso"
T_USUARIO_ROL = f"{_SCHEMA}.usuario_rol"
T_ROL_PERMISO = f"{_SCHEMA}.rol_permiso"
T_VINCULO_USUARIO = f"{_SCHEMA}.vinculo_usuario"
T_CONTACTO = f"{_SCHEMA}.contacto"
T_ADULTO_CONTACTO = f"{_SCHEMA}.adulto_contacto"
T_IDEMPOTENCIA_OPERACION = f"{_SCHEMA}.idempotencia_operacion"
T_COMUNICACION = f"{_SCHEMA}.comunicacion"
T_MENSAJE_VOZ = f"{_SCHEMA}.mensaje_voz"
T_COMUNICACION_HISTORIAL_ESTADO = f"{_SCHEMA}.comunicacion_historial_estado"
T_SOLICITUD_AYUDA = f"{_SCHEMA}.solicitud_ayuda"
T_SOLICITUD_AYUDA_NOTIFICACION = f"{_SCHEMA}.solicitud_ayuda_notificacion"
T_SOLICITUD_AYUDA_HISTORIAL_ESTADO = f"{_SCHEMA}.solicitud_ayuda_historial_estado"
T_MENSAJE_TEXTO = f"{_SCHEMA}.mensaje_texto"
T_RECORDATORIO = f"{_SCHEMA}.recordatorio"
T_RECORDATORIO_HISTORIAL_ESTADO = f"{_SCHEMA}.recordatorio_historial_estado"
T_DISPOSITIVO_FCM = f"{_SCHEMA}.dispositivo_fcm"

# --- Literales de los CHECK del esquema ---
OP_CREAR_COMUNICACION = "CREAR_COMUNICACION"
OP_CREAR_SOLICITUD_AYUDA = "CREAR_SOLICITUD_AYUDA"

TIPO_LLAMADA_AUDIO = "LLAMADA_AUDIO"
TIPO_VIDEOLLAMADA = "VIDEOLLAMADA"
TIPO_MENSAJE_VOZ = "MENSAJE_VOZ"

# Escritura de L2 (D-R3-9): completada -> FINALIZADA, no_contestada -> NO_ATENDIDA,
# cancelada -> CANCELADA.
CALL_STATUS_TO_DB: dict[CallStatus, str] = {
    CallStatus.COMPLETADA: "FINALIZADA",
    CallStatus.NO_CONTESTADA: "NO_ATENDIDA",
    CallStatus.CANCELADA: "CANCELADA",
}
# Lectura: FINALIZADA -> completada, CANCELADA -> cancelada; INICIADA, ESTABLECIDA,
# NO_ATENDIDA y FALLIDA -> no_contestada.
_CALL_STATUS_FROM_DB: dict[str, CallStatus] = {
    "FINALIZADA": CallStatus.COMPLETADA,
    "CANCELADA": CallStatus.CANCELADA,
    "INICIADA": CallStatus.NO_CONTESTADA,
    "ESTABLECIDA": CallStatus.NO_CONTESTADA,
    "NO_ATENDIDA": CallStatus.NO_CONTESTADA,
    "FALLIDA": CallStatus.NO_CONTESTADA,
}

_LOCATION_AVAILABLE = "DISPONIBLE"
_LOCATION_UNAVAILABLE = "NO_DISPONIBLE"

_OFFSET_TEXT = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_MAX_BIGINT = 2**63 - 1
_NO_EMAIL_DOMAIN = "@sin-correo.local"
_EMPTY_NAME = "-"
_IDEMPOTENCY_TTL = timedelta(hours=24)
_IDEMPOTENCY_OPERATIONS = frozenset({OP_CREAR_COMUNICACION, OP_CREAR_SOLICITUD_AYUDA})


# --- Conversiones puras ---


def call_status_from_db(value: str) -> CallStatus:
    """Estado de comunicacion -> CallStatus del contrato (desconocido -> no_contestada)."""
    return _CALL_STATUS_FROM_DB.get(value, CallStatus.NO_CONTESTADA)


def tipo_for_audio_only(audio_only: bool) -> str:
    """audio_only true -> LLAMADA_AUDIO; false -> VIDEOLLAMADA."""
    return TIPO_LLAMADA_AUDIO if audio_only else TIPO_VIDEOLLAMADA


def audio_only_from_tipo(tipo: str) -> bool:
    """LLAMADA_AUDIO -> True; cualquier otro tipo -> False."""
    return tipo == TIPO_LLAMADA_AUDIO


def location_state(latitude: float | None, longitude: float | None) -> str:
    """estado_ubicacion: DISPONIBLE solo si llegan latitud y longitud (0.0 es válido)."""
    if latitude is not None and longitude is not None:
        return _LOCATION_AVAILABLE
    return _LOCATION_UNAVAILABLE


def offset_text(dt: datetime) -> str:
    """Offset de una fecha con zona como texto "+HH:MM" o "-HH:MM" (zona_horaria, D-R3-11)."""
    offset = dt.utcoffset()
    if offset is None:
        raise ValueError("La fecha no tiene zona horaria.")
    total_seconds = int(offset.total_seconds())
    sign = "-" if total_seconds < 0 else "+"
    hours, remainder = divmod(abs(total_seconds), 3600)
    return f"{sign}{hours:02d}:{remainder // 60:02d}"


def tzinfo_from_text(value: str | None) -> tzinfo:
    """Zona a partir de zona_horaria (D-R3-11).

    "+HH:MM" o "-HH:MM" -> offset fijo; nombre IANA (por ejemplo el default
    'America/Bogota') -> ZoneInfo; vacío o inválido -> UTC.
    """
    if not value:
        return timezone.utc
    candidate = value.strip()
    match = _OFFSET_TEXT.match(candidate)
    if match is not None:
        sign, hours, minutes = match.groups()
        if int(hours) > 23 or int(minutes) > 59:
            return timezone.utc
        delta = timedelta(hours=int(hours), minutes=int(minutes))
        return timezone(-delta if sign == "-" else delta)
    try:
        return ZoneInfo(candidate)
    except Exception:
        return timezone.utc


def as_utc(value: datetime | None) -> datetime | None:
    """Fecha leída de la base en UTC; sin zona se interpreta como UTC; None -> None."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    """Ahora en UTC, sin microsegundos."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_id(value: object) -> int | None:
    """Id de la API (texto) -> bigint; None si no es un entero ASCII no negativo de 64 bits.

    "²", "", "-1", "1e3", " 12" o un valor mayor que 2**63-1 se tratan como inexistentes
    sin consultar la base.
    """
    if not isinstance(value, str) or not (value.isascii() and value.isdigit()):
        return None
    number = int(value)
    if number > _MAX_BIGINT:
        return None
    return number


def id_str(value: int) -> str:
    """bigint de la base -> id de la API en texto."""
    return str(int(value))


def daily_room_url(domain: str, room_name: str | None) -> str:
    """URL de la sala de Daily a partir del dominio configurado y daily_sala_id (D-R3-9).

    Normaliza el dominio igual que daily_client: quita http(s):// y la barra final y
    añade ".daily.co" si no tiene punto. Sin nombre de sala devuelve "".
    """
    if not room_name:
        return ""
    host = (domain or "").strip()
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
            break
    host = host.rstrip("/")
    if "." not in host:
        host = f"{host}.daily.co"
    return f"https://{host}/{room_name}"


# --- Helpers con conexión ---

_SELECT_USER_ID = text(f"SELECT id_usuario FROM {T_USUARIO} WHERE firebase_uid = :uid")
_SELECT_UID = text(f"SELECT firebase_uid FROM {T_USUARIO} WHERE id_usuario = :user_id")
_SELECT_FULL_NAME = text(
    f"SELECT nombre, apellido FROM {T_USUARIO} WHERE id_usuario = :user_id"
)
_INSERT_MINIMAL_USER = text(
    f"INSERT INTO {T_USUARIO} "
    "(firebase_uid, nombre, apellido, correo, creado_en, actualizado_en) "
    f"VALUES (:uid, '{_EMPTY_NAME}', '{_EMPTY_NAME}', :correo, :now, :now) "
    "ON CONFLICT (firebase_uid) DO NOTHING"
)
_RELEASE_EMAIL = text(
    f"UPDATE {T_USUARIO} "
    f"SET correo = firebase_uid || '{_NO_EMAIL_DOMAIN}', "
    "correo_verificado = false, "
    "actualizado_en = GREATEST(clock_timestamp(), creado_en) "
    "WHERE lower(btrim(correo)) = lower(btrim(:correo)) AND firebase_uid <> :keep_uid"
)
_INSERT_IDEMPOTENCY = text(
    f"INSERT INTO {T_IDEMPOTENCIA_OPERACION} "
    "(usuario_id, operacion, clave_hash, hash_solicitud, estado, "
    "creado_en, expira_en, completado_en) "
    "VALUES (:usuario_id, :operacion, :clave_hash, :hash_solicitud, 'COMPLETADA', "
    ":now, :expira_en, :now) "
    "RETURNING id_idempotencia"
)
_SELECT_CONTACT_USER = text(
    "SELECT u.id_usuario "
    f"FROM {T_ADULTO_CONTACTO} ac "
    f"JOIN {T_USUARIO} u ON lower(btrim(u.correo)) = lower(btrim(ac.correo)) "
    "WHERE ac.id_adulto_contacto = :adulto_contacto_id "
    "ORDER BY u.id_usuario "
    "LIMIT 1"
)


def user_id_for_uid(conn: Connection, uid: str) -> int | None:
    """usuario.id_usuario del firebase_uid dado; None si no hay fila (lee usuario)."""
    if not uid:
        return None
    value = conn.execute(_SELECT_USER_ID, {"uid": uid}).scalar_one_or_none()
    return None if value is None else int(value)


def uid_for_user_id(conn: Connection, user_id: int | None) -> str | None:
    """usuario.firebase_uid del id_usuario dado; None si no hay fila (lee usuario)."""
    if user_id is None:
        return None
    return conn.execute(_SELECT_UID, {"user_id": int(user_id)}).scalar_one_or_none()


def release_email(conn: Connection, email: str, keep_uid: str) -> int:
    """Libera el correo de perfiles obsoletos antes de escribirlo en keep_uid (D-R3-5b).

    Tabla usuario: toda fila con otro firebase_uid cuyo lower(btrim(correo)) coincide pasa
    a correo = firebase_uid || '@sin-correo.local', correo_verificado = false y
    actualizado_en = GREATEST(clock_timestamp(), creado_en). Así el correo no choca con
    uq_usuario_correo_ci y gana la marca más reciente. Devuelve cuántas filas cambió y, si
    cambió alguna, registra un warning sin el correo. Correo vacío -> no hace nada.
    """
    normalized = normalize_email(email or "")
    if not normalized:
        return 0
    result = conn.execute(_RELEASE_EMAIL, {"correo": normalized, "keep_uid": keep_uid})
    released = max(result.rowcount or 0, 0)
    if released:
        logger.warning(
            "Se liberó un correo repetido en %d perfil(es) obsoleto(s).", released
        )
    return released


def ensure_user_id(conn: Connection, uid: str, email: str | None = None) -> int:
    """id_usuario del uid; si no existe crea la fila mínima en usuario (D-R3-4).

    Fila mínima: firebase_uid = uid, nombre = apellido = "-" (CHECK de no vacío),
    correo = email normalizado o f"{uid}@sin-correo.local", creado_en = actualizado_en =
    ahora (UTC, desde Python); correo_verificado, estado y el resto quedan con su default.
    Antes de escribir un correo real aplica release_email. El INSERT usa
    ON CONFLICT (firebase_uid) DO NOTHING y relee, así dos peticiones simultáneas del mismo
    uid no fallan. Una fila ya existente no se modifica.
    """
    if not uid:
        raise ValueError("Falta el uid del usuario.")
    existing = user_id_for_uid(conn, uid)
    if existing is not None:
        return existing

    correo = normalize_email(email or "")
    if correo:
        release_email(conn, correo, uid)
    else:
        correo = f"{uid}{_NO_EMAIL_DOMAIN}"
    conn.execute(_INSERT_MINIMAL_USER, {"uid": uid, "correo": correo, "now": utc_now()})

    created = user_id_for_uid(conn, uid)
    if created is None:
        raise RuntimeError("No se pudo crear el perfil mínimo del usuario.")
    return created


def user_full_name(conn: Connection, user_id: int | None) -> str:
    """Nombre y apellido de usuario separados por espacio (lee usuario).

    Omite el "-" de la fila mínima; "" si el usuario no existe.
    """
    if user_id is None:
        return ""
    row = conn.execute(_SELECT_FULL_NAME, {"user_id": int(user_id)}).one_or_none()
    if row is None:
        return ""
    parts = []
    for part in (row[0], row[1]):
        cleaned = (part or "").strip()
        if cleaned and cleaned != _EMPTY_NAME:
            parts.append(cleaned)
    return " ".join(parts)


def new_idempotency(
    conn: Connection, user_id: int, operacion: str, hash_source: str
) -> int:
    """Inserta una fila COMPLETADA en idempotencia_operacion; devuelve id_idempotencia.

    D-R3-14. Columnas: usuario_id, operacion (CREAR_COMUNICACION o CREAR_SOLICITUD_AYUDA;
    otra -> ValueError), clave_hash = uuid4().hex (32 caracteres), hash_solicitud = sha256
    hex de hash_source (64), estado 'COMPLETADA', creado_en = completado_en = ahora (UTC,
    Python) y expira_en = creado_en + 24 h. Sin deduplicación por clave.
    """
    if operacion not in _IDEMPOTENCY_OPERATIONS:
        raise ValueError("Operación de idempotencia no válida.")
    now = utc_now()
    value = conn.execute(
        _INSERT_IDEMPOTENCY,
        {
            "usuario_id": int(user_id),
            "operacion": operacion,
            "clave_hash": uuid.uuid4().hex,
            "hash_solicitud": hashlib.sha256(hash_source.encode("utf-8")).hexdigest(),
            "now": now,
            "expira_en": now + _IDEMPOTENCY_TTL,
        },
    ).scalar_one()
    return int(value)


def contact_user_id(conn: Connection, adulto_contacto_id: int | str | None) -> int | None:
    """id_usuario cuyo correo coincide con adulto_contacto.correo (D-R3-12).

    Lee adulto_contacto y usuario comparando lower(btrim(correo)), verificado o no. Es un
    mapeo de ALMACENAMIENTO para las FK de mensaje_texto y comunicacion; nunca sirve para
    autorizar ni para decidir push. None si el contacto no existe, no tiene correo o ningún
    usuario lo tiene. Acepta el id como int o como texto de la API.
    """
    if isinstance(adulto_contacto_id, str):
        adulto_contacto_id = parse_id(adulto_contacto_id)
    if adulto_contacto_id is None:
        return None
    value = conn.execute(
        _SELECT_CONTACT_USER, {"adulto_contacto_id": int(adulto_contacto_id)}
    ).scalar_one_or_none()
    return None if value is None else int(value)
