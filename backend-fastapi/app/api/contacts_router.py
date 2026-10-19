"""Router de contactos (C1-C4) del adulto mayor."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.core.security import FORBIDDEN_DETAIL, require_roles, resolve_adult_scope
from app.models.auth import AuthenticatedUser
from app.models.contacts import Contact, ContactCreateRequest, ContactUpdateRequest
from app.models.enums import Role
from app.repositories.contact_repository import (
    ContactRepository,
    get_contact_repository,
)
from app.repositories.user_repository import UserRepository, get_user_repository
from app.services.contact_service import (
    ContactNotFoundError,
    ContactOrderInUseError,
    ContactPermissionDeniedError,
    create_contact,
    delete_contact,
    list_contacts,
    update_contact,
)


CONTACT_NOT_FOUND_DETAIL = "El contacto no existe."
# Índice único de favoritos en PostgreSQL (plan ronda 3, D-R3-7; AV-09b).
CONTACT_ORDER_IN_USE_DETAIL = (
    "Datos inválidos: favorite_order: ya está en uso por otro contacto"
)

router = APIRouter()

# Administrador y usuario sin rol reciben 403 antes de buscar el recurso (D-R2-7, paso 0).
_require_contact_roles = require_roles(Role.ADULTO_MAYOR, Role.FAMILIAR, Role.CUIDADOR)


@router.get("", response_model=list[Contact])
def list_adult_contacts(
    adult_id: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(_require_contact_roles),
    users: UserRepository = Depends(get_user_repository),
    contacts: ContactRepository = Depends(get_contact_repository),
) -> list[Contact]:
    adult_uid = resolve_adult_scope(user, adult_id, users)
    return list_contacts(adult_uid, contacts)


@router.post("", response_model=Contact)
def create_adult_contact(
    payload: ContactCreateRequest,
    user: AuthenticatedUser = Depends(_require_contact_roles),
    users: UserRepository = Depends(get_user_repository),
    contacts: ContactRepository = Depends(get_contact_repository),
) -> Contact:
    adult_uid = resolve_adult_scope(user, payload.adult_id, users)
    try:
        return create_contact(user, adult_uid, payload, contacts, users)
    except ContactPermissionDeniedError as error:
        raise _forbidden() from error
    except ContactOrderInUseError as error:
        raise _order_in_use() from error


@router.patch("/{contact_id}", response_model=Contact)
def update_adult_contact(
    contact_id: str,
    payload: ContactUpdateRequest,
    user: AuthenticatedUser = Depends(_require_contact_roles),
    users: UserRepository = Depends(get_user_repository),
    contacts: ContactRepository = Depends(get_contact_repository),
) -> Contact:
    try:
        return update_contact(user, contact_id, payload, contacts, users)
    except ContactNotFoundError as error:
        raise _not_found() from error
    except ContactPermissionDeniedError as error:
        raise _forbidden() from error
    except ContactOrderInUseError as error:
        raise _order_in_use() from error


@router.delete(
    "/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
def delete_adult_contact(
    contact_id: str,
    user: AuthenticatedUser = Depends(_require_contact_roles),
    users: UserRepository = Depends(get_user_repository),
    contacts: ContactRepository = Depends(get_contact_repository),
) -> Response:
    try:
        delete_contact(user, contact_id, contacts, users)
    except ContactNotFoundError as error:
        raise _not_found() from error
    except ContactPermissionDeniedError as error:
        raise _forbidden() from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=CONTACT_NOT_FOUND_DETAIL,
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=FORBIDDEN_DETAIL,
    )


def _order_in_use() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=CONTACT_ORDER_IN_USE_DETAIL,
    )
