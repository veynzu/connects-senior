import threading
from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import bindparam, text

from app.db import session as db_session
from app.db.mapping import T_DISPOSITIVO_FCM, T_USUARIO, ensure_user_id, utc_now


class DeviceRepository(Protocol):
    """Tokens FCM de los navegadores de cada usuario (A10, ADR-07, D-R2-4)."""

    def register(self, uid: str, fcm_token: str) -> None: ...

    def tokens_of(self, uids: Iterable[str]) -> list[str]: ...

    def remove_tokens(self, tokens: Iterable[str]) -> None: ...

    def has_device(self, uid: str) -> bool: ...


class InMemoryDeviceRepository:
    """Implementación en memoria: los datos se pierden al reiniciar.

    Un token pertenece a un solo uid: registrarlo con otro uid lo mueve. Todos los métodos
    toman el mismo lock porque el planificador lee desde el threadpool.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # fcm_token -> uid; el dict conserva el orden de inserción.
        self._owners: dict[str, str] = {}

    def register(self, uid: str, fcm_token: str) -> None:
        """Idempotente para el mismo uid; si el token era de otro uid, pasa a este."""
        with self._lock:
            current_owner = self._owners.get(fcm_token)
            if current_owner == uid:
                return
            if current_owner is not None:
                del self._owners[fcm_token]
            self._owners[fcm_token] = uid

    def tokens_of(self, uids: Iterable[str]) -> list[str]:
        """Tokens de esos uids en orden de inserción, sin duplicados; uids desconocidos se ignoran."""
        wanted = set(uids)
        with self._lock:
            owners = list(self._owners.items())
        return [token for token, owner in owners if owner in wanted]

    def remove_tokens(self, tokens: Iterable[str]) -> None:
        # El iterable se materializa fuera del lock por si lo produce este mismo repositorio.
        token_list = list(tokens)
        with self._lock:
            for token in token_list:
                self._owners.pop(token, None)

    def has_device(self, uid: str) -> bool:
        with self._lock:
            return any(owner == uid for owner in list(self._owners.values()))


# --- PostgreSQL (plan ronda 3, D-R3-8) ---

_UPSERT_DEVICE = text(
    f"INSERT INTO {T_DISPOSITIVO_FCM} AS d "
    "(usuario_id, token_fcm, activo, registrado_en, actualizado_en) "
    "VALUES (:usuario_id, :token, true, :now, :now) "
    "ON CONFLICT (token_fcm) DO UPDATE SET "
    "usuario_id = EXCLUDED.usuario_id, activo = true, "
    "actualizado_en = GREATEST(clock_timestamp(), d.registrado_en)"
)
_SELECT_ACTIVE_TOKENS = text(
    f"SELECT d.token_fcm FROM {T_DISPOSITIVO_FCM} d "
    f"JOIN {T_USUARIO} u ON u.id_usuario = d.usuario_id "
    "WHERE d.activo AND u.firebase_uid IN :uids "
    "ORDER BY d.id_dispositivo"
).bindparams(bindparam("uids", expanding=True))
_DEACTIVATE_TOKENS = text(
    f"UPDATE {T_DISPOSITIVO_FCM} SET activo = false, "
    "actualizado_en = GREATEST(clock_timestamp(), registrado_en) "
    "WHERE activo AND token_fcm IN :tokens"
).bindparams(bindparam("tokens", expanding=True))
_HAS_ACTIVE_DEVICE = text(
    "SELECT EXISTS ("
    f"SELECT 1 FROM {T_DISPOSITIVO_FCM} d "
    f"JOIN {T_USUARIO} u ON u.id_usuario = d.usuario_id "
    "WHERE d.activo AND u.firebase_uid = :uid"
    ")"
)


class SqlDeviceRepository:
    """Tokens FCM en connects_senior.dispositivo_fcm (D-R3-8).

    token_fcm es UNIQUE: registrar un token que ya existe lo pasa al uid nuevo y lo
    reactiva. remove_tokens no borra filas: las desactiva (activo = false). Las lecturas
    solo ven tokens activos, en orden de id_dispositivo. Los tokens son datos sensibles:
    nunca se registran.
    """

    def __init__(self, db: db_session.Database) -> None:
        self._db = db

    def register(self, uid: str, fcm_token: str) -> None:
        """Upsert por token; crea la fila mínima de usuario si el uid no tiene perfil."""
        with self._db.transaction() as conn:
            user_id = ensure_user_id(conn, uid)
            conn.execute(
                _UPSERT_DEVICE,
                {"usuario_id": user_id, "token": fcm_token, "now": utc_now()},
            )

    def tokens_of(self, uids: Iterable[str]) -> list[str]:
        """Tokens activos de esos uids por id_dispositivo, sin duplicados."""
        wanted = [uid for uid in dict.fromkeys(uids) if uid]
        if not wanted:
            return []
        with self._db.transaction() as conn:
            tokens = conn.execute(_SELECT_ACTIVE_TOKENS, {"uids": wanted}).scalars().all()
        return list(dict.fromkeys(tokens))

    def remove_tokens(self, tokens: Iterable[str]) -> None:
        # Se materializa ANTES de abrir la transacción: el iterable puede venir de este
        # mismo repositorio (abriría otra transacción dentro de esta).
        token_list = [token for token in dict.fromkeys(tokens) if token]
        if not token_list:
            return
        with self._db.transaction() as conn:
            conn.execute(_DEACTIVATE_TOKENS, {"tokens": token_list})

    def has_device(self, uid: str) -> bool:
        if not uid:
            return False
        with self._db.transaction() as conn:
            return bool(conn.execute(_HAS_ACTIVE_DEVICE, {"uid": uid}).scalar_one())


_default_device_repository = InMemoryDeviceRepository()
_sql_device_repository = db_session.lazy_sql_repository(SqlDeviceRepository)


def get_device_repository() -> DeviceRepository:
    if db_session.sql_enabled():
        return _sql_device_repository()
    return _default_device_repository
