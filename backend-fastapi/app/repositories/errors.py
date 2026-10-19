"""Excepciones de dominio que lanzan los repositorios SQL (plan ronda 3).

Los servicios las traducen a sus propias excepciones y los routers a HTTP; nunca llevan
datos personales ni mensajes crudos de la base.
"""


class RepositoryError(Exception):
    """Base de los errores de persistencia que el servicio sabe manejar."""


class RecipientNotLinkedError(RepositoryError):
    """El contacto no corresponde a un usuario de la plataforma distinto del adulto.

    mensaje_texto y comunicacion exigen emisor y destinatario usuarios; sin usuario del
    contacto (o si es el propio adulto) no se puede guardar el mensaje (D-R3-12, D-R3-13).
    """


class ContactOrderConflictError(RepositoryError):
    """Otro contacto favorito activo del mismo adulto ya usa ese favorite_order.

    Corresponde al índice único parcial uq_adulto_contacto_orden_favorito (D-R3-7).
    """
