"""Servicio de mensajes de texto (M1, M2; CLAUDE.md 6.4, P19, D-R2-12).

La conversación es siempre entre un adulto mayor y uno de sus contactos. El adulto
escribe y lee con sus propios contactos; un familiar o cuidador vinculado escribe y
lee como el contacto cuyo email coincide con el email verificado de su token
(resolución compartida en contact_scope). El mensaje se guarda como adulto -> contacto
(from_adult True) o contacto -> adulto (from_adult False), así from_me sale correcto
para ambas partes.

No conoce FastAPI: lanza excepciones de dominio que el router traduce. La push al
destinatario nunca hace fallar el envío: push_service.notify_users no lanza y la
resolución del destinatario y del nombre del emisor, que se hace después de guardar,
queda protegida (D-R3-23). Nunca se registra el texto del mensaje.
"""
import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.models.auth import AuthenticatedUser
from app.models.contacts import Contact
from app.models.messages import (
    MessageCreateRequest,
    MessageCreateResponse,
    MessageRecord,
    MessageResponse,
)
from app.repositories.contact_repository import ContactRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.errors import RecipientNotLinkedError
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services import push_service
from app.services.contact_scope import (
    ContactNotLinkedError,
    ContactScope,
    ContactScopeForbiddenError,
    ContactScopeNotFoundError,
    resolve_contact_scope,
)


MESSAGE_PUSH_TITLE = "Nuevo mensaje"
MESSAGE_PUSH_BODY_MAX_LENGTH = 120
MESSAGE_PUSH_TYPE = "message"
MESSAGE_RECIPIENT_UNAVAILABLE_DETAIL = "El contacto no tiene una cuenta en Connects Senior."

logger = logging.getLogger(__name__)


class MessageContactNotFoundError(Exception):
    """El contacto no existe o pertenece a un adulto fuera del alcance del llamante."""


class MessagePermissionDeniedError(Exception):
    """Sin permiso: rol sin acceso, contacto no vinculado al llamante o messages en false."""


class MessageRecipientUnavailableError(Exception):
    """El contacto no tiene una cuenta de la plataforma con la que guardar el mensaje.

    Solo con la base PostgreSQL y, desde V004, solo cuando escribe el contacto y su
    usuario emisor no se resuelve (D-R3-26a; en la práctica no ocurre, y el adulto sí
    puede escribir a un contacto sin cuenta). El router responde 422 con
    MESSAGE_RECIPIENT_UNAVAILABLE_DETAIL.
    """


def _resolve_scope(
    user: AuthenticatedUser,
    contact_id: str,
    contacts: ContactRepository,
    users: UserRepository,
) -> ContactScope:
    try:
        return resolve_contact_scope(user, contact_id, contacts, users)
    except ContactScopeNotFoundError as error:
        raise MessageContactNotFoundError(str(error)) from error
    except ContactScopeForbiddenError as error:
        # Incluye ContactNotLinkedError: en M1 un contacto no vinculado es 403 (P19).
        raise MessagePermissionDeniedError(str(error)) from error


def _contact_full_name(contact: Contact) -> str:
    return f"{contact.first_name} {contact.last_name}".strip()


def _sender_name(
    user: AuthenticatedUser, scope: ContactScope, users: UserRepository
) -> str:
    """Nombre del emisor para la push (D-R2-12).

    Adulto: nombre del perfil, display_name del token o uid. Contacto (familiar o
    cuidador): display_name del token o "first_name last_name" del contacto.
    """
    if scope.from_adult:
        profile = users.get_profile(user.uid)
        if profile is not None and profile.full_name:
            return profile.full_name
        return user.display_name or user.uid
    return user.display_name or _contact_full_name(scope.contact) or user.uid


def _recipient_uid(scope: ContactScope, users: UserRepository) -> str | None:
    """Destinatario de la push (D-R2-12).

    Si escribe el adulto: el uid verificado del contacto, solo si además está vinculado
    al adulto (quien no puede leer la conversación con M2 no recibe la push). Si escribe
    el contacto: el adulto.
    """
    if not scope.from_adult:
        return scope.adult_uid
    linked_uid = push_service.linked_uid_for_contact(scope.contact, users)
    if linked_uid is None or not users.is_linked(linked_uid, scope.adult_uid):
        return None
    return linked_uid


def send_message(
    user: AuthenticatedUser,
    payload: MessageCreateRequest,
    contacts: ContactRepository,
    messages: MessageRepository,
    users: UserRepository,
    devices: DeviceRepository,
    now: datetime | None = None,
) -> MessageCreateResponse:
    """M1: guarda el mensaje y avisa por push al destinatario si es alcanzable.

    Lanza MessageContactNotFoundError (contacto inexistente o fuera del alcance),
    MessagePermissionDeniedError (rol sin acceso, contacto no vinculado al familiar o
    cuidador, o permissions.messages en false) o MessageRecipientUnavailableError (el
    repositorio no puede guardar el mensaje porque el contacto que escribe no tiene
    cuenta, D-R3-26a).
    Síncrona: la push hace red. Una vez guardado el mensaje, ningún fallo al resolver el
    destinatario o al enviar la push cambia la respuesta (D-R3-23).
    """
    scope = _resolve_scope(user, payload.contact_id, contacts, users)
    if not scope.contact.permissions.messages:
        raise MessagePermissionDeniedError("El contacto no admite mensajes.")

    created_at = (now if now is not None else datetime.now(timezone.utc)).replace(
        microsecond=0
    )
    try:
        record = messages.add(
            MessageRecord(
                id=str(uuid4()),
                adult_uid=scope.adult_uid,
                contact_id=scope.contact.id,
                text=payload.text,
                created_at=created_at,
                from_adult=scope.from_adult,
            )
        )
    except RecipientNotLinkedError as error:
        raise MessageRecipientUnavailableError(
            MESSAGE_RECIPIENT_UNAVAILABLE_DETAIL
        ) from error

    # D-R3-23: el mensaje ya está guardado; con la base SQL resolver el destinatario y el
    # nombre del emisor hace red, y un fallo ahí no puede convertir M1 en un error (el
    # front reintentaría y duplicaría el mensaje). Solo se registra el tipo del error.
    try:
        recipient_uid = _recipient_uid(scope, users)
        if recipient_uid is not None:
            push_service.notify_users(
                [recipient_uid],
                MESSAGE_PUSH_TITLE,
                record.text[:MESSAGE_PUSH_BODY_MAX_LENGTH],
                {
                    "type": MESSAGE_PUSH_TYPE,
                    "message_id": record.id,
                    "contact_id": record.contact_id,
                    "adult_id": record.adult_uid,
                    "from_name": _sender_name(user, scope, users),
                },
                devices,
            )
    except Exception as error:
        logger.error(
            "No se pudo avisar por push del mensaje guardado (%s)", type(error).__name__
        )

    return MessageCreateResponse(id=record.id, created_at=record.created_at)


def list_conversation(
    user: AuthenticatedUser,
    contact_id: str,
    contacts: ContactRepository,
    messages: MessageRepository,
    users: UserRepository,
) -> list[MessageResponse]:
    """M2: conversación con el contacto, por created_at ascendente.

    Adulto: from_me = from_adult. Familiar o cuidador: from_me = not from_adult, y lista
    vacía si el contacto no está vinculado a su email verificado (P19). Lanza
    MessageContactNotFoundError (contacto inexistente o fuera del alcance) o
    MessagePermissionDeniedError (administrador o sin rol; defensa en profundidad).
    """
    try:
        scope = resolve_contact_scope(user, contact_id, contacts, users)
    except ContactNotLinkedError:
        return []
    except ContactScopeNotFoundError as error:
        raise MessageContactNotFoundError(str(error)) from error
    except ContactScopeForbiddenError as error:
        raise MessagePermissionDeniedError(str(error)) from error

    return [
        MessageResponse(
            id=record.id,
            from_me=record.from_adult == scope.from_adult,
            text=record.text,
            created_at=record.created_at,
        )
        for record in messages.list_conversation(scope.adult_uid, scope.contact.id)
    ]
