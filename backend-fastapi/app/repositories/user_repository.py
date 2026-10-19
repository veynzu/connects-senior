import threading
from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from app.db import session as db_session
from app.db.mapping import (
    T_ROL,
    T_USUARIO,
    T_USUARIO_ROL,
    T_VINCULO_USUARIO,
    ensure_user_id,
    release_email,
    utc_now,
)
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


# --- Implementación PostgreSQL (plan ronda 3, D-R3-4 a D-R3-6, D-R3-19) ---

# Fila mínima de ensure_user_id y marcadores de "sin dato" (D-R3-5b).
_EMPTY_NAME = "-"
_NO_EMAIL_DOMAIN = "@sin-correo.local"

# Catálogo rol: código del esquema y nombre visible por cada rol del contrato (D-R3-5).
_ROLE_CATALOG: dict[Role, tuple[str, str]] = {
    Role.ADULTO_MAYOR: ("ADULTO_MAYOR", "Adulto mayor"),
    Role.FAMILIAR: ("FAMILIAR", "Familiar"),
    Role.CUIDADOR: ("CUIDADOR", "Cuidador"),
    Role.ADMINISTRADOR: ("ADMINISTRADOR", "Administrador"),
}
_ROLE_BY_CODE: dict[str, Role] = {code: role for role, (code, _) in _ROLE_CATALOG.items()}
_ROLE_CODES_SQL = ", ".join(f"'{code}'" for code, _ in _ROLE_CATALOG.values())

# vinculo_usuario.tipo_vinculo (CHECK FAMILIAR, CUIDADOR) (D-R3-6).
_LINK_TYPE_BY_ROLE: dict[Role, str] = {
    Role.FAMILIAR: "FAMILIAR",
    Role.CUIDADOR: "CUIDADOR",
}
_ROLE_BY_LINK_TYPE: dict[str, Role] = {
    link_type: role for role, link_type in _LINK_TYPE_BY_ROLE.items()
}

# Campo del perfil -> columna de usuario para los cambios directos de update_profile.
_PROFILE_NAME_COLUMNS = {"first_name": "nombre", "last_name": "apellido"}
_PROFILE_OPTIONAL_TEXT_COLUMNS = {"phone": "telefono", "address": "direccion"}
_PROFILE_PLAIN_COLUMNS = {"birth_date": "fecha_nacimiento", "photo_url": "foto_url"}

_PROFILE_SELECT = (
    "SELECT u.id_usuario, u.firebase_uid, u.nombre, u.apellido, u.correo, "
    "u.correo_verificado, u.telefono, u.fecha_nacimiento, u.direccion, u.foto_url, "
    "(SELECT r.codigo "
    f"FROM {T_USUARIO_ROL} ur JOIN {T_ROL} r ON r.id_rol = ur.id_rol "
    f"WHERE ur.id_usuario = u.id_usuario AND ur.activo AND r.codigo IN ({_ROLE_CODES_SQL}) "
    "ORDER BY ur.asignado_en DESC, ur.id_rol DESC LIMIT 1) AS rol_codigo "
    f"FROM {T_USUARIO} u "
)
_SELECT_PROFILE = text(_PROFILE_SELECT + "WHERE u.firebase_uid = :uid")
_SELECT_PROFILE_BY_ID = text(_PROFILE_SELECT + "WHERE u.id_usuario = :id_usuario")
_LOCK_PROFILE = text(
    f"SELECT id_usuario, nombre, apellido FROM {T_USUARIO} "
    "WHERE firebase_uid = :uid FOR NO KEY UPDATE"
)
_UPSERT_PROFILE = text(
    f"INSERT INTO {T_USUARIO} AS u "
    "(firebase_uid, nombre, apellido, correo, correo_verificado, telefono, "
    "fecha_nacimiento, direccion, foto_url, creado_en, actualizado_en) "
    "VALUES (:uid, :nombre, :apellido, :correo, :verificado, :telefono, "
    ":fecha_nacimiento, :direccion, :foto_url, :now, :now) "
    "ON CONFLICT (firebase_uid) DO UPDATE SET "
    "nombre = EXCLUDED.nombre, "
    "apellido = EXCLUDED.apellido, "
    "correo = EXCLUDED.correo, "
    "correo_verificado = CASE "
    "WHEN lower(btrim(u.correo)) = lower(btrim(EXCLUDED.correo)) "
    "THEN u.correo_verificado OR EXCLUDED.correo_verificado "
    "ELSE EXCLUDED.correo_verificado END, "
    "telefono = EXCLUDED.telefono, "
    "fecha_nacimiento = EXCLUDED.fecha_nacimiento, "
    "direccion = EXCLUDED.direccion, "
    "foto_url = EXCLUDED.foto_url, "
    "actualizado_en = GREATEST(clock_timestamp(), u.creado_en) "
    "RETURNING id_usuario, correo_verificado"
)
_MARK_EMAIL_VERIFIED = text(
    f"UPDATE {T_USUARIO} SET correo = :correo, correo_verificado = true, "
    "actualizado_en = GREATEST(clock_timestamp(), creado_en) "
    "WHERE id_usuario = :id_usuario"
)
_FIND_UID_BY_VERIFIED_EMAIL = text(
    f"SELECT firebase_uid FROM {T_USUARIO} "
    "WHERE lower(btrim(correo)) = :correo AND correo_verificado "
    "ORDER BY id_usuario LIMIT 1"
)

_SEED_ROLE = text(
    f"INSERT INTO {T_ROL} (codigo, nombre) VALUES (:codigo, :nombre) "
    "ON CONFLICT (codigo) DO NOTHING"
)
_SELECT_ROLE_ID = text(f"SELECT id_rol FROM {T_ROL} WHERE codigo = :codigo")
_DEACTIVATE_OTHER_ROLES = text(
    f"UPDATE {T_USUARIO_ROL} SET activo = false "
    "WHERE id_usuario = :id_usuario AND activo AND id_rol <> :id_rol"
)
_DEACTIVATE_ALL_ROLES = text(
    f"UPDATE {T_USUARIO_ROL} SET activo = false WHERE id_usuario = :id_usuario AND activo"
)
_ASSIGN_ROLE = text(
    f"INSERT INTO {T_USUARIO_ROL} (id_usuario, id_rol, asignado_en, activo) "
    "VALUES (:id_usuario, :id_rol, now(), true) "
    "ON CONFLICT (id_usuario, id_rol) DO UPDATE SET activo = true, asignado_en = now()"
)

_LOCK_USER_ROW = text(
    f"SELECT id_usuario FROM {T_USUARIO} WHERE id_usuario = :id_usuario FOR NO KEY UPDATE"
)
# Mismo predicado que el índice único parcial uq_vinculo_responsable_activo_por_adulto
# (adulto_mayor_id) WHERE es_responsable AND estado = 'ACTIVO' (V004, D-R3-26(e)): sin
# filtrar por tipo_vinculo, para que ninguna fila que el índice cuente quede fuera.
_SELECT_OTHER_RESPONSIBLE_FOR_UPDATE = text(
    f"SELECT id_vinculo FROM {T_VINCULO_USUARIO} "
    "WHERE adulto_mayor_id = :adulto AND usuario_apoyo_id <> :apoyo "
    "AND estado = 'ACTIVO' AND es_responsable "
    "ORDER BY id_vinculo FOR UPDATE"
)
_UPSERT_LINK = text(
    f"INSERT INTO {T_VINCULO_USUARIO} "
    "(adulto_mayor_id, usuario_apoyo_id, tipo_vinculo, es_responsable, "
    "puede_gestionar_contactos, puede_gestionar_recordatorios, puede_recibir_alertas, "
    "estado, autorizado_por_id, fecha_fin) "
    "VALUES (:adulto, :apoyo, :tipo, :responsable, :contactos, :recordatorios, "
    ":alertas, 'ACTIVO', NULL, NULL) "
    "ON CONFLICT (adulto_mayor_id, usuario_apoyo_id) DO UPDATE SET "
    "tipo_vinculo = EXCLUDED.tipo_vinculo, "
    "es_responsable = EXCLUDED.es_responsable, "
    "puede_gestionar_contactos = EXCLUDED.puede_gestionar_contactos, "
    "puede_gestionar_recordatorios = EXCLUDED.puede_gestionar_recordatorios, "
    "puede_recibir_alertas = EXCLUDED.puede_recibir_alertas, "
    "estado = 'ACTIVO', autorizado_por_id = NULL, fecha_fin = NULL"
)

# Vínculos activos con los uid de ambos extremos (D-R3-6: las lecturas solo ven ACTIVO).
_ACTIVE_LINKS = (
    f"FROM {T_VINCULO_USUARIO} v "
    f"JOIN {T_USUARIO} a ON a.id_usuario = v.adulto_mayor_id "
    f"JOIN {T_USUARIO} s ON s.id_usuario = v.usuario_apoyo_id "
    "WHERE v.estado = 'ACTIVO' "
)
_LINK_COLUMNS = (
    "s.firebase_uid AS user_uid, a.firebase_uid AS adult_uid, v.tipo_vinculo, "
    "v.puede_gestionar_contactos, v.puede_gestionar_recordatorios, "
    "v.puede_recibir_alertas "
)
_IS_LINKED = text(
    f"SELECT EXISTS (SELECT 1 {_ACTIVE_LINKS}"
    "AND s.firebase_uid = :user_uid AND a.firebase_uid = :adult_uid)"
)
_ADULTS_OF = text(
    f"SELECT a.firebase_uid {_ACTIVE_LINKS}"
    "AND s.firebase_uid = :user_uid ORDER BY v.id_vinculo"
)
_LINK_OF = text(
    f"SELECT {_LINK_COLUMNS}{_ACTIVE_LINKS}"
    "AND s.firebase_uid = :user_uid AND a.firebase_uid = :adult_uid"
)
_CAREGIVERS_OF = text(
    f"SELECT {_LINK_COLUMNS}{_ACTIVE_LINKS}"
    "AND a.firebase_uid = :adult_uid AND v.tipo_vinculo = 'CUIDADOR' "
    "ORDER BY v.id_vinculo"
)
# Entre los FAMILIAR activos marcados como responsables, el de menor id_vinculo; si no
# hay ninguno marcado, el FAMILIAR activo de menor id_vinculo (D-R3-6).
_RESPONSIBLE_FAMILY_OF = text(
    f"SELECT s.firebase_uid {_ACTIVE_LINKS}"
    "AND a.firebase_uid = :adult_uid AND v.tipo_vinculo = 'FAMILIAR' "
    "ORDER BY v.es_responsable DESC, v.id_vinculo LIMIT 1"
)


def _placeholder_email(uid: str) -> str:
    """Correo de reserva cuando no hay uno real (CHECK de no vacío, D-R3-5b)."""
    return f"{uid}{_NO_EMAIL_DOMAIN}"


def _name_for_db(value: object) -> str:
    """Nombre o apellido para usuario: vacío, solo espacios o no texto -> "-"."""
    if isinstance(value, str) and value.strip():
        return value
    return _EMPTY_NAME


def _optional_text_for_db(value: object) -> object:
    """telefono y direccion: NULL o no vacíos (CHECK); "" o solo espacios -> NULL."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def _as_role(value: object) -> Role | None:
    """Rol del perfil; None o un valor fuera del enum se tratan como sin rol (D-02.4)."""
    if value is None:
        return None
    try:
        return Role(value)
    except ValueError:
        return None


def _is_minimal_row(nombre: str | None, apellido: str | None) -> bool:
    """Fila mínima de ensure_user_id: nombre y apellido "-" (D-R3-5b)."""
    return (nombre or "").strip() == _EMPTY_NAME and (apellido or "").strip() == _EMPTY_NAME


def _name_from_db(value: str | None) -> str:
    cleaned = value or ""
    return "" if cleaned.strip() == _EMPTY_NAME else cleaned


def _email_from_db(value: str | None, uid: str) -> str:
    cleaned = value or ""
    if cleaned.strip().lower() == _placeholder_email(uid).lower():
        return ""
    return cleaned


def _profile_from_row(row: Row) -> UserProfile:
    uid = row.firebase_uid
    return UserProfile(
        uid=uid,
        first_name=_name_from_db(row.nombre),
        last_name=_name_from_db(row.apellido),
        email=_email_from_db(row.correo, uid),
        role=_ROLE_BY_CODE.get(row.rol_codigo) if row.rol_codigo else None,
        phone=row.telefono,
        birth_date=row.fecha_nacimiento,
        address=row.direccion,
        photo_url=row.foto_url,
        email_verified=bool(row.correo_verificado),
    )


def _link_from_row(row: Row) -> AdultLink:
    role = _ROLE_BY_LINK_TYPE[row.tipo_vinculo]
    permissions: CaregiverPermissions | None = None
    if role == Role.CUIDADOR:
        permissions = CaregiverPermissions(
            manage_contacts=bool(row.puede_gestionar_contactos),
            manage_reminders=bool(row.puede_gestionar_recordatorios),
            receive_alerts=bool(row.puede_recibir_alertas),
        )
    return AdultLink(
        user_uid=row.user_uid,
        adult_uid=row.adult_uid,
        role=role,
        permissions=permissions,
    )


def _write_role(conn: Connection, user_id: int, role: Role | None) -> None:
    """Refleja el rol del claim en usuario_rol con una sola fila activa (D-R3-5).

    Siembra en rol solo el código usado (ON CONFLICT DO NOTHING, en la misma transacción),
    desactiva las demás filas activas del usuario y activa la del rol. Sin rol, desactiva
    todas: get_profile devolverá role None.
    """
    if role is None:
        conn.execute(_DEACTIVATE_ALL_ROLES, {"id_usuario": user_id})
        return
    code, name = _ROLE_CATALOG[role]
    conn.execute(_SEED_ROLE, {"codigo": code, "nombre": name})
    role_id = int(conn.execute(_SELECT_ROLE_ID, {"codigo": code}).scalar_one())
    conn.execute(_DEACTIVATE_OTHER_ROLES, {"id_usuario": user_id, "id_rol": role_id})
    conn.execute(_ASSIGN_ROLE, {"id_usuario": user_id, "id_rol": role_id})


class SqlUserRepository:
    """Perfiles, correos verificados y vínculos en PostgreSQL (tablas usuario, rol,
    usuario_rol y vinculo_usuario del esquema connects_senior).

    Cada método público abre una sola transacción con ``Database.transaction()`` y no
    llama a otros métodos públicos. Devuelve objetos nuevos en cada llamada. La fila
    mínima que crea ensure_user_id (nombre y apellido "-") no es un perfil para
    get_profile ni update_profile, pero sí cuenta para correos verificados y vínculos.
    """

    def __init__(self, db: db_session.Database) -> None:
        self._db = db

    # --- Perfiles ---

    def add_profile(self, profile: UserProfile) -> UserProfile:
        """Crea o sobrescribe el perfil del uid (upsert por firebase_uid).

        correo_verificado se conserva solo si el correo no cambia (D-R3-5a); otro perfil
        con el mismo correo pierde ese correo antes de escribir (D-R3-5b). Devuelve el
        perfil recibido con el correo normalizado y email_verified tal como quedó guardado.
        """
        normalized = normalize_email(profile.email or "")
        role = _as_role(profile.role)
        with self._db.transaction() as conn:
            if normalized:
                release_email(conn, normalized, profile.uid)
            row = conn.execute(
                _UPSERT_PROFILE,
                {
                    "uid": profile.uid,
                    "nombre": _name_for_db(profile.first_name),
                    "apellido": _name_for_db(profile.last_name),
                    "correo": normalized or _placeholder_email(profile.uid),
                    "verificado": bool(profile.email_verified),
                    "telefono": _optional_text_for_db(profile.phone),
                    "fecha_nacimiento": profile.birth_date,
                    "direccion": _optional_text_for_db(profile.address),
                    "foto_url": profile.photo_url,
                    "now": utc_now(),
                },
            ).one()
            _write_role(conn, int(row.id_usuario), role)
            stored_verified = bool(row.correo_verificado)
        return replace(profile, email=normalized, email_verified=stored_verified)

    def get_profile(self, uid: str) -> UserProfile | None:
        if not uid:
            return None
        with self._db.transaction() as conn:
            row = conn.execute(_SELECT_PROFILE, {"uid": uid}).one_or_none()
        if row is None or _is_minimal_row(row.nombre, row.apellido):
            return None
        return _profile_from_row(row)

    def update_profile(
        self, uid: str, changes: Mapping[str, object]
    ) -> UserProfile | None:
        """Aplica solo las claves del perfil (ignora el resto); None si no hay perfil.

        email: normalizado, vacío -> correo de reserva; un correo distinto pierde la marca
        de verificado salvo que llegue email_verified explícito (D-R3-5a). role: None o un
        valor fuera del enum dejan el perfil sin rol. Devuelve el perfil releído.
        """
        columns: dict[str, object] = {}
        new_email: str | None = None
        verified: bool | None = None
        role_changed = False
        new_role: Role | None = None
        for key, value in changes.items():
            if key not in _UPDATABLE_PROFILE_FIELDS:
                continue
            if key in _PROFILE_NAME_COLUMNS:
                columns[_PROFILE_NAME_COLUMNS[key]] = _name_for_db(value)
            elif key in _PROFILE_OPTIONAL_TEXT_COLUMNS:
                columns[_PROFILE_OPTIONAL_TEXT_COLUMNS[key]] = _optional_text_for_db(value)
            elif key in _PROFILE_PLAIN_COLUMNS:
                columns[_PROFILE_PLAIN_COLUMNS[key]] = value
            elif key == "email":
                if isinstance(value, str):
                    new_email = normalize_email(value)
            elif key == "email_verified":
                if value is not None:
                    verified = bool(value)
            elif key == "role":
                role_changed = True
                new_role = _as_role(value)

        if not uid:
            return None
        with self._db.transaction() as conn:
            current = conn.execute(_LOCK_PROFILE, {"uid": uid}).one_or_none()
            if current is None or _is_minimal_row(current.nombre, current.apellido):
                return None
            user_id = int(current.id_usuario)

            # Los nombres de columna salen de los mapas fijos de arriba, nunca de changes.
            assignments = [f"{column} = :{column}" for column in columns]
            params: dict[str, object] = dict(columns)
            if new_email is not None:
                if new_email:
                    release_email(conn, new_email, uid)
                params["correo"] = new_email or _placeholder_email(uid)
                assignments.append("correo = :correo")
                if verified is None:
                    # Parámetro aparte: reutilizar :correo deduce tipos distintos
                    # (varchar en la asignación, text en btrim) y Postgres lo rechaza.
                    params["correo_comparado"] = params["correo"]
                    assignments.append(
                        "correo_verificado = CASE "
                        "WHEN lower(btrim(correo)) = lower(btrim(:correo_comparado)) "
                        "THEN correo_verificado ELSE false END"
                    )
            if verified is not None:
                params["correo_verificado"] = verified
                assignments.append("correo_verificado = :correo_verificado")
            if assignments:
                assignments.append("actualizado_en = GREATEST(clock_timestamp(), creado_en)")
                params["id_usuario"] = user_id
                conn.execute(
                    text(
                        f"UPDATE {T_USUARIO} SET {', '.join(assignments)} "
                        "WHERE id_usuario = :id_usuario"
                    ),
                    params,
                )
            if role_changed:
                _write_role(conn, user_id, new_role)
            row = conn.execute(_SELECT_PROFILE_BY_ID, {"id_usuario": user_id}).one()
        return _profile_from_row(row)

    # --- Correos verificados (vínculo contacto -> usuario, P9) ---

    def mark_email_verified(self, uid: str, email: str) -> None:
        """Marca el correo como verificado del uid; sin perfil crea la fila mínima.

        Gana la marca más reciente: otro uid con el mismo correo lo pierde (D-R3-5b).
        """
        normalized = normalize_email(email or "")
        if not normalized:
            return
        with self._db.transaction() as conn:
            release_email(conn, normalized, uid)
            user_id = ensure_user_id(conn, uid, normalized)
            conn.execute(
                _MARK_EMAIL_VERIFIED, {"correo": normalized, "id_usuario": user_id}
            )

    def find_uid_by_verified_email(self, email: str) -> str | None:
        normalized = normalize_email(email or "")
        if not normalized:
            return None
        with self._db.transaction() as conn:
            return conn.execute(
                _FIND_UID_BY_VERIFIED_EMAIL, {"correo": normalized}
            ).scalar_one_or_none()

    # --- Vínculos con adultos mayores ---

    def link_adult(
        self,
        user_uid: str,
        adult_uid: str,
        role: Role,
        permissions: CaregiverPermissions | None = None,
    ) -> None:
        """Crea o reemplaza el vínculo (upsert por adulto y usuario de apoyo, D-R3-6).

        Solo FAMILIAR o CUIDADOR (otro rol -> ValueError). El cuidador guarda sus permisos
        (None -> los tres en false); el familiar nunca. El vínculo queda ACTIVO y sin
        fecha_fin.

        Responsable (índice único parcial de V004, D-R3-26(e)): con la fila del adulto
        bloqueada, un FAMILIAR queda como responsable solo si el adulto no tiene otro
        vínculo ACTIVO marcado como responsable; si lo tiene, queda sin la marca aunque la
        tuviera antes (por ejemplo, un vínculo revocado que vuelve después de que otro
        familiar asumió). Re-vincular al responsable actual la conserva; pasar a CUIDADOR
        la quita.
        """
        link_role = Role(role)
        link_type = _LINK_TYPE_BY_ROLE.get(link_role)
        if link_type is None:
            raise ValueError("Solo un familiar o un cuidador se vincula con un adulto mayor.")
        if user_uid == adult_uid:
            raise ValueError("Un usuario no puede vincularse consigo mismo.")
        if link_role == Role.CUIDADOR:
            granted = permissions if permissions is not None else NO_CAREGIVER_PERMISSIONS
        else:
            granted = NO_CAREGIVER_PERMISSIONS

        with self._db.transaction() as conn:
            adult_id = ensure_user_id(conn, adult_uid)
            user_id = ensure_user_id(conn, user_uid)
            # Serializa las altas de vínculos de un mismo adulto (marca de responsable):
            # con este bloqueo y la consulta idéntica al predicado del índice, dos
            # link_adult concurrentes no pueden dejar dos responsables activos.
            conn.execute(_LOCK_USER_ROW, {"id_usuario": adult_id})
            ids = {"adulto": adult_id, "apoyo": user_id}
            if link_role == Role.FAMILIAR:
                other_responsible = conn.execute(
                    _SELECT_OTHER_RESPONSIBLE_FOR_UPDATE, ids
                ).first()
                responsible = other_responsible is None
            else:
                responsible = False
            conn.execute(
                _UPSERT_LINK,
                {
                    **ids,
                    "tipo": link_type,
                    "responsable": responsible,
                    "contactos": bool(granted.manage_contacts),
                    "recordatorios": bool(granted.manage_reminders),
                    "alertas": bool(granted.receive_alerts),
                },
            )

    def is_linked(self, user_uid: str, adult_uid: str) -> bool:
        with self._db.transaction() as conn:
            return bool(
                conn.execute(
                    _IS_LINKED, {"user_uid": user_uid, "adult_uid": adult_uid}
                ).scalar_one()
            )

    def adults_of(self, user_uid: str) -> list[str]:
        with self._db.transaction() as conn:
            return list(conn.execute(_ADULTS_OF, {"user_uid": user_uid}).scalars())

    def link_of(self, user_uid: str, adult_uid: str) -> AdultLink | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                _LINK_OF, {"user_uid": user_uid, "adult_uid": adult_uid}
            ).one_or_none()
        return None if row is None else _link_from_row(row)

    def caregivers_of(self, adult_uid: str) -> list[AdultLink]:
        with self._db.transaction() as conn:
            rows = conn.execute(_CAREGIVERS_OF, {"adult_uid": adult_uid}).all()
        return [_link_from_row(row) for row in rows]

    def responsible_family_of(self, adult_uid: str) -> str | None:
        with self._db.transaction() as conn:
            return conn.execute(
                _RESPONSIBLE_FAMILY_OF, {"adult_uid": adult_uid}
            ).scalar_one_or_none()


_default_user_repository = InMemoryUserRepository()
_sql_user_repository = db_session.lazy_sql_repository(SqlUserRepository)


def get_user_repository() -> UserRepository:
    if db_session.sql_enabled():
        return _sql_user_repository()
    return _default_user_repository
