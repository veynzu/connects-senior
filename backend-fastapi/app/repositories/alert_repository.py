"""Repositorio de solicitudes de ayuda (H1-H5, ADR-05, D-R2-10, D-R2-23).

Dos implementaciones del mismo Protocol: InMemoryAlertRepository (tests y DATABASE_URL de
sqlite) y SqlAlertRepository (PostgreSQL, plan ronda 3, D-R3-10; esquema V004, D-R3-26b).
El proveedor elige en cada llamada según DATABASE_URL (D-R3-2).
"""
import itertools
import json
import threading
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from app.db import mapping
from app.db import session as db_session
from app.db.mapping import (
    OP_CREAR_SOLICITUD_AYUDA,
    T_ADULTO_CONTACTO,
    T_SOLICITUD_AYUDA,
    T_SOLICITUD_AYUDA_HISTORIAL_ESTADO,
    T_USUARIO,
)
from app.models.alerts import AlertRecord
from app.models.enums import AlertCategory, AlertStatus


class AlertRepository(Protocol):
    """Acceso a las solicitudes de ayuda de todos los adultos mayores."""

    def add(self, record: AlertRecord) -> AlertRecord: ...

    def get(self, alert_id: str) -> AlertRecord | None: ...

    def update(self, record: AlertRecord) -> AlertRecord | None: ...

    def update_if_status(
        self, record: AlertRecord, expected_status: AlertStatus
    ) -> AlertRecord | None: ...

    def list_for_adult(self, adult_uid: str) -> list[AlertRecord]: ...

    def list_pending(self) -> list[AlertRecord]: ...


class InMemoryAlertRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Guarda y devuelve copias. Todos los métodos toman el mismo lock porque el tick
    de escalamiento escribe desde el threadpool mientras las rutas leen y escriben;
    las iteraciones recorren copias. Un contador de inserción desempata las
    solicitudes creadas en el mismo segundo.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._alerts: dict[str, AlertRecord] = {}
        self._insertion_order: dict[str, int] = {}
        self._counter = itertools.count()

    def add(self, record: AlertRecord) -> AlertRecord:
        stored = replace(record)
        with self._lock:
            self._alerts[stored.id] = stored
            if stored.id not in self._insertion_order:
                self._insertion_order[stored.id] = next(self._counter)
        return replace(stored)

    def get(self, alert_id: str) -> AlertRecord | None:
        """Busca por id en todos los adultos; el servicio comprueba el alcance."""
        with self._lock:
            record = self._alerts.get(alert_id)
            return None if record is None else replace(record)

    def update(self, record: AlertRecord) -> AlertRecord | None:
        """Reemplaza el registro con el mismo id; None si no existe."""
        stored = replace(record)
        with self._lock:
            if stored.id not in self._alerts:
                return None
            self._alerts[stored.id] = stored
        return replace(stored)

    def update_if_status(
        self, record: AlertRecord, expected_status: AlertStatus
    ) -> AlertRecord | None:
        """Reemplaza el registro solo si el guardado tiene expected_status (atómico).

        Si no existe o su estado cambió (por ejemplo, alguien lo atendió), devuelve
        None sin tocar nada.
        """
        stored = replace(record)
        with self._lock:
            current = self._alerts.get(stored.id)
            if current is None or current.status != expected_status:
                return None
            self._alerts[stored.id] = stored
        return replace(stored)

    def list_for_adult(self, adult_uid: str) -> list[AlertRecord]:
        """Del más reciente al más antiguo.

        Se ordena ascendente y estable por created_at (empate: orden de inserción) y
        después se invierte: en un empate va primero la última insertada.
        """
        with self._lock:
            records = [
                (self._insertion_order[record.id], record)
                for record in list(self._alerts.values())
                if record.adult_uid == adult_uid
            ]
        records.sort(key=lambda item: (item[1].created_at, item[0]))
        return [replace(record) for _, record in reversed(records)]

    def list_pending(self) -> list[AlertRecord]:
        """Solicitudes en estado pendiente de todos los adultos, en orden de inserción."""
        with self._lock:
            records = list(self._alerts.values())
        return [
            replace(record)
            for record in records
            if record.status == AlertStatus.PENDIENTE
        ]


# --- PostgreSQL (plan ronda 3, D-R3-10; esquema V004, D-R3-26b) ---

# 'cerrada' existe en el CHECK del esquema pero el backend no la escribe: se lee como
# atendida (D-R3-10).
_DB_CLOSED = "cerrada"
_EMPTY_NAME = "-"

# Una fila por solicitud con todo lo derivado en la misma consulta (sin N+1):
# - rondas / ultimo_envio: columnas ronda_notificacion y ultimo_envio_en; NULL en
#   ultimo_envio_en = solo el envío inicial, se lee creada_en (D-R3-26b).
# - confianza: existe un contacto ACTIVO del adulto con es_confianza (has_trusted_contacts).
# - atendida_por: uid de cambiado_por_id de la última fila 'en_atencion' del historial
#   (NULL si esa fila no tiene usuario).
_SELECT_ALERTS = (
    "SELECT s.id_solicitud_ayuda AS id_solicitud, u.firebase_uid AS adulto_uid, "
    "u.nombre AS nombre, u.apellido AS apellido, s.categoria AS categoria, "
    "s.estado AS estado, s.creada_en AS creada_en, s.latitud AS latitud, "
    "s.longitud AS longitud, s.ronda_notificacion AS rondas, "
    "COALESCE(s.ultimo_envio_en, s.creada_en) AS ultimo_envio, "
    "EXISTS ("
    f"SELECT 1 FROM {T_ADULTO_CONTACTO} ac "
    "WHERE ac.adulto_mayor_id = s.adulto_mayor_id "
    "AND ac.estado = 'ACTIVO' AND ac.es_confianza"
    ") AS confianza, "
    "("
    f"SELECT ua.firebase_uid FROM {T_SOLICITUD_AYUDA_HISTORIAL_ESTADO} ha "
    f"LEFT JOIN {T_USUARIO} ua ON ua.id_usuario = ha.cambiado_por_id "
    "WHERE ha.solicitud_ayuda_id = s.id_solicitud_ayuda "
    "AND ha.estado_nuevo = 'en_atencion' "
    "ORDER BY ha.id_historial_estado DESC LIMIT 1"
    ") AS atendida_por "
    f"FROM {T_SOLICITUD_AYUDA} s "
    f"JOIN {T_USUARIO} u ON u.id_usuario = s.adulto_mayor_id "
)
_SELECT_ALERT = text(_SELECT_ALERTS + "WHERE s.id_solicitud_ayuda = :id")
_LIST_FOR_ADULT = text(
    _SELECT_ALERTS
    + "WHERE u.firebase_uid = :uid "
    "ORDER BY s.creada_en DESC, s.id_solicitud_ayuda DESC"
)
_LIST_PENDING = text(
    _SELECT_ALERTS + "WHERE s.estado = 'pendiente' ORDER BY s.id_solicitud_ayuda"
)
_INSERT_ALERT = text(
    f"INSERT INTO {T_SOLICITUD_AYUDA} "
    "(adulto_mayor_id, id_idempotencia, categoria, estado, estado_ubicacion, "
    "latitud, longitud, precision_metros, ronda_notificacion, ultimo_envio_en, "
    "creada_en, actualizada_en, cerrada_en) "
    "VALUES (:adulto, :idempotencia, :categoria, :estado, :estado_ubicacion, "
    ":latitud, :longitud, NULL, :ronda, :ultimo_envio, "
    ":creada_en, :creada_en, :cerrada_en) "
    "RETURNING id_solicitud_ayuda"
)
_INSERT_CREATION_HISTORY = text(
    f"INSERT INTO {T_SOLICITUD_AYUDA_HISTORIAL_ESTADO} "
    "(solicitud_ayuda_id, cambiado_por_id, estado_nuevo, cambiado_en) "
    "VALUES (:solicitud, :cambiado_por, :estado, :cambiado_en)"
)
_LOCK_ALERT = text(
    "SELECT estado, creada_en, ronda_notificacion, "
    "COALESCE(ultimo_envio_en, creada_en) AS ultimo_envio "
    f"FROM {T_SOLICITUD_AYUDA} "
    "WHERE id_solicitud_ayuda = :id FOR UPDATE"
)
# Reemplaza las columnas que corresponden a campos del registro (categoria, estado,
# ubicación, ronda y último envío). cerrada_en se fija al pasar a atendida y se vacía al
# salir de atendida. ultimo_envio_en solo se escribe si last_notified_at cambió respecto a
# lo guardado, con GREATEST para respetar el CHECK >= creada_en (D-R3-19, D-R3-26b).
# precision_metros nunca la escribe el backend: se conserva mientras haya ubicación.
_UPDATE_ALERT_SQL = (
    f"UPDATE {T_SOLICITUD_AYUDA} SET "
    "categoria = :categoria, "
    "estado = :estado, "
    "ronda_notificacion = :ronda, "
    "ultimo_envio_en = CASE WHEN :cambiar_envio "
    "THEN GREATEST(CAST(:ultimo_envio AS timestamptz), creada_en) "
    "ELSE ultimo_envio_en END, "
    "estado_ubicacion = :estado_ubicacion, "
    "latitud = :latitud, "
    "longitud = :longitud, "
    "precision_metros = CASE WHEN :con_ubicacion THEN precision_metros ELSE NULL END, "
    "cerrada_en = CASE WHEN :cerrar THEN GREATEST(clock_timestamp(), creada_en) "
    "WHEN :reabrir THEN NULL ELSE cerrada_en END, "
    "actualizada_en = GREATEST(clock_timestamp(), creada_en) "
    "WHERE id_solicitud_ayuda = :id "
)
_UPDATE_ALERT = text(_UPDATE_ALERT_SQL + "RETURNING id_solicitud_ayuda")
_UPDATE_ALERT_IF_STATUS = text(
    _UPDATE_ALERT_SQL
    + "AND estado IN (:esperado, :esperado_alt) RETURNING id_solicitud_ayuda"
)
_INSERT_STATUS_HISTORY = text(
    f"INSERT INTO {T_SOLICITUD_AYUDA_HISTORIAL_ESTADO} "
    "(solicitud_ayuda_id, cambiado_por_id, estado_nuevo, cambiado_en) "
    "VALUES (:solicitud, :cambiado_por, :estado, "
    "GREATEST(clock_timestamp(), :creada_en))"
)


def _status_from_db(value: str) -> AlertStatus:
    """estado de solicitud_ayuda -> AlertStatus ('cerrada' se lee como atendida)."""
    if value == _DB_CLOSED:
        return AlertStatus.ATENDIDA
    return AlertStatus(value)


def _db_statuses(status: AlertStatus) -> tuple[str, str]:
    """Valores de estado en la base que equivalen a un AlertStatus (atendida incluye cerrada)."""
    if status == AlertStatus.ATENDIDA:
        return (AlertStatus.ATENDIDA.value, _DB_CLOSED)
    return (status.value, status.value)


def _category_to_db(category: AlertCategory | None) -> str | None:
    """categoria admite NULL desde V004: sin categoría se guarda NULL (D-R3-26b)."""
    return None if category is None else category.value


def _category_from_db(value: str | None) -> AlertCategory | None:
    """categoria NULL -> None (D-R3-26b)."""
    return None if value is None else AlertCategory(value)


def _first_sent_at(record: AlertRecord) -> datetime | None:
    """ultimo_envio_en para add: NULL si last_notified_at no es posterior a created_at.

    NULL significa "solo el envío inicial" y se lee como creada_en; un valor anterior a
    created_at violaría el CHECK ultimo_envio_en >= creada_en (D-R3-19).
    """
    if record.last_notified_at > record.created_at:
        return record.last_notified_at
    return None


def _coordinate(value: Decimal | None) -> float | None:
    """numeric(9,6) -> float (6 decimales, AV-09f)."""
    return None if value is None else float(value)


def _full_name(first_name: str | None, last_name: str | None) -> str:
    """Igual que mapping.user_full_name, sobre columnas ya leídas (sin consulta extra)."""
    parts = []
    for part in (first_name, last_name):
        cleaned = (part or "").strip()
        if cleaned and cleaned != _EMPTY_NAME:
            parts.append(cleaned)
    return " ".join(parts)


def _hash_source(record: AlertRecord) -> str:
    """Texto del que sale hash_solicitud de idempotencia_operacion (D-R3-14)."""
    return json.dumps(
        {
            "adult_uid": record.adult_uid,
            "category": record.category.value if record.category is not None else None,
            "status": record.status.value,
            "created_at": record.created_at.isoformat(),
            "latitude": record.latitude,
            "longitude": record.longitude,
        },
        sort_keys=True,
    )


def _to_record(row: Row) -> AlertRecord:
    return AlertRecord(
        id=mapping.id_str(row.id_solicitud),
        adult_uid=row.adulto_uid,
        adult_name=_full_name(row.nombre, row.apellido),
        category=_category_from_db(row.categoria),
        status=_status_from_db(row.estado),
        created_at=mapping.as_utc(row.creada_en),
        latitude=_coordinate(row.latitud),
        longitude=_coordinate(row.longitud),
        has_trusted_contacts=bool(row.confianza),
        last_notified_at=mapping.as_utc(row.ultimo_envio),
        acknowledged_by=row.atendida_por,
        notification_round=int(row.rondas),
    )


def _read_alert(conn: Connection, alert_id: int) -> AlertRecord | None:
    row = conn.execute(_SELECT_ALERT, {"id": alert_id}).one_or_none()
    return None if row is None else _to_record(row)


def _write_alert(
    conn: Connection,
    alert_id: int,
    record: AlertRecord,
    expected_status: AlertStatus | None,
) -> AlertRecord | None:
    """update / update_if_status dentro de la transacción del método público.

    D-R3-10 y D-R3-26b. Bloquea la fila con FOR UPDATE y reemplaza las columnas del
    registro; con expected_status, solo si el estado guardado sigue siendo ese (UPDATE
    condicionado: 0 filas -> None sin tocar nada). notification_round va a
    ronda_notificacion y last_notified_at a ultimo_envio_en (este solo si cambió respecto
    a lo guardado). Si el estado cambia inserta una fila de historial (cambiado_por_id =
    quien la atiende al pasar a en_atencion; NULL en el resto), salvo en una transición a
    pendiente: el registro no dice quién la reabrió y D-R3-26b retira las filas
    'pendiente' sin usuario. adult_uid, adult_name, created_at y has_trusted_contacts no
    se escriben (son fijos o derivados). Devuelve el registro releído.
    """
    current = conn.execute(_LOCK_ALERT, {"id": alert_id}).one_or_none()
    if current is None:
        return None

    old_status = _status_from_db(current.estado)
    status_changed = record.status != old_status
    has_location = record.latitude is not None and record.longitude is not None
    params = {
        "id": alert_id,
        "categoria": _category_to_db(record.category),
        # Sin cambio de estado se conserva el valor guardado (por ejemplo 'cerrada').
        "estado": record.status.value if status_changed else current.estado,
        # Con la fila bloqueada, una ronda sin cambio reescribe el mismo valor.
        "ronda": record.notification_round,
        "cambiar_envio": record.last_notified_at != mapping.as_utc(current.ultimo_envio),
        "ultimo_envio": record.last_notified_at,
        "estado_ubicacion": mapping.location_state(record.latitude, record.longitude),
        "latitud": record.latitude if has_location else None,
        "longitud": record.longitude if has_location else None,
        "con_ubicacion": has_location,
        "cerrar": status_changed and record.status == AlertStatus.ATENDIDA,
        "reabrir": status_changed and old_status == AlertStatus.ATENDIDA,
    }
    if expected_status is None:
        updated = conn.execute(_UPDATE_ALERT, params).scalar_one_or_none()
    else:
        expected, expected_alt = _db_statuses(expected_status)
        updated = conn.execute(
            _UPDATE_ALERT_IF_STATUS,
            {**params, "esperado": expected, "esperado_alt": expected_alt},
        ).scalar_one_or_none()
    if updated is None:
        return None

    if status_changed and record.status != AlertStatus.PENDIENTE:
        changed_by = None
        if record.status == AlertStatus.EN_ATENCION and record.acknowledged_by:
            changed_by = mapping.ensure_user_id(conn, record.acknowledged_by)
        conn.execute(
            _INSERT_STATUS_HISTORY,
            {
                "solicitud": alert_id,
                "cambiado_por": changed_by,
                "estado": record.status.value,
                "creada_en": current.creada_en,
            },
        )

    return _read_alert(conn, alert_id)


class SqlAlertRepository:
    """Solicitudes de ayuda en PostgreSQL (D-R3-10).

    Tablas: solicitud_ayuda (una fila por solicitud, con categoria NULL si no hay
    categoría y la ronda y el último envío en ronda_notificacion / ultimo_envio_en,
    D-R3-26b), solicitud_ayuda_historial_estado (creación y cambios de estado) e
    idempotencia_operacion (una fila por alta; id_idempotencia admite NULL desde V004 pero
    se sigue escribiendo). Lo que el registro no guarda se deriva al leer: adult_name del
    perfil actual, has_trusted_contacts de los contactos activos con es_confianza,
    acknowledged_by de la última fila 'en_atencion' y last_notified_at = creada_en cuando
    ultimo_envio_en es NULL. solicitud_ayuda_notificacion no se usa (P29b).

    Cada método público hace todo en una sola transacción. Un id que no es un entero se
    trata como inexistente sin consultar la base.
    """

    def __init__(self, db: db_session.Database) -> None:
        self._db = db

    def add(self, record: AlertRecord) -> AlertRecord:
        """Inserta idempotencia + solicitud + historial de creación.

        La ronda va a ronda_notificacion (el servicio pasa 0) y ultimo_envio_en queda NULL
        mientras last_notified_at sea created_at. Devuelve el registro recibido con el id
        nuevo y todo lo demás tal como vino (adult_name y has_trusted_contacts del
        servicio, D-R3-4).
        """
        has_location = record.latitude is not None and record.longitude is not None
        with self._db.transaction() as conn:
            adult_id = mapping.ensure_user_id(conn, record.adult_uid)
            idempotency_id = mapping.new_idempotency(
                conn, adult_id, OP_CREAR_SOLICITUD_AYUDA, _hash_source(record)
            )
            new_id = conn.execute(
                _INSERT_ALERT,
                {
                    "adulto": adult_id,
                    "idempotencia": idempotency_id,
                    "categoria": _category_to_db(record.category),
                    "estado": record.status.value,
                    "estado_ubicacion": mapping.location_state(
                        record.latitude, record.longitude
                    ),
                    "latitud": record.latitude if has_location else None,
                    "longitud": record.longitude if has_location else None,
                    "ronda": record.notification_round,
                    "ultimo_envio": _first_sent_at(record),
                    "creada_en": record.created_at,
                    "cerrada_en": (
                        record.created_at
                        if record.status == AlertStatus.ATENDIDA
                        else None
                    ),
                },
            ).scalar_one()
            # Fila de creación con el adulto como autor: nunca cuenta como ronda.
            conn.execute(
                _INSERT_CREATION_HISTORY,
                {
                    "solicitud": new_id,
                    "cambiado_por": adult_id,
                    "estado": record.status.value,
                    "cambiado_en": record.created_at,
                },
            )
        return replace(record, id=mapping.id_str(new_id))

    def get(self, alert_id: str) -> AlertRecord | None:
        """Busca por id en todos los adultos; el servicio comprueba el alcance."""
        number = mapping.parse_id(alert_id)
        if number is None:
            return None
        with self._db.transaction() as conn:
            return _read_alert(conn, number)

    def update(self, record: AlertRecord) -> AlertRecord | None:
        """Reemplaza la solicitud con el mismo id; None si no existe."""
        number = mapping.parse_id(record.id)
        if number is None:
            return None
        with self._db.transaction() as conn:
            return _write_alert(conn, number, record, None)

    def update_if_status(
        self, record: AlertRecord, expected_status: AlertStatus
    ) -> AlertRecord | None:
        """Reemplaza solo si el estado guardado es expected_status (atómico).

        Si no existe o su estado cambió, devuelve None sin tocar nada.
        """
        number = mapping.parse_id(record.id)
        if number is None:
            return None
        with self._db.transaction() as conn:
            return _write_alert(conn, number, record, expected_status)

    def list_for_adult(self, adult_uid: str) -> list[AlertRecord]:
        """Del más reciente al más antiguo; en un empate, primero el id mayor."""
        if not adult_uid:
            return []
        with self._db.transaction() as conn:
            rows = conn.execute(_LIST_FOR_ADULT, {"uid": adult_uid}).all()
        return [_to_record(row) for row in rows]

    def list_pending(self) -> list[AlertRecord]:
        """Solicitudes pendientes de todos los adultos, en orden de creación (id)."""
        with self._db.transaction() as conn:
            rows = conn.execute(_LIST_PENDING).all()
        return [_to_record(row) for row in rows]


_default_alert_repository = InMemoryAlertRepository()
_sql_alert_repository = db_session.lazy_sql_repository(SqlAlertRepository)


def get_alert_repository() -> AlertRepository:
    if db_session.sql_enabled():
        return _sql_alert_repository()
    return _default_alert_repository
