"""
ConnectsSenior Backend - FastAPI
Arranque de la aplicación y registro de routers.
"""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.api.auth_router import router as auth_router
from app.core import scheduler
from app.core.config import get_settings
from app.core.firebase import ensure_emulator_not_leaking
from app.db import session as db_session


logger = logging.getLogger(__name__)

DATABASE_UNAVAILABLE_DETAIL = "El servicio no está disponible. Intenta más tarde."
DATABASE_DATA_ERROR_DETAIL = "Datos inválidos: cuerpo: no es válido"
DATABASE_INTERNAL_ERROR_DETAIL = "Error interno. Intenta más tarde."


async def _open_database() -> db_session.Database | None:
    """Comprueba la base al arrancar si DATABASE_URL es PostgreSQL (D-R3-16).

    La URL se lee en tiempo de ejecución con get_settings(), nunca de la variable de módulo
    `settings` (evaluada al importar). Si la base no responde, check_database lanza un
    RuntimeError sin la URL ni la causa y el arranque falla.
    """
    if not db_session.is_postgres_url(get_settings().database_url):
        logger.info("Persistencia: memoria")
        return None
    database = db_session.get_database()
    try:
        await run_in_threadpool(db_session.check_database, database)
    except BaseException:
        await _close_database(database)
        raise
    logger.info("Persistencia: PostgreSQL")
    return database


async def _close_database(database: db_session.Database | None) -> None:
    engine = database.engine if database is not None else None
    if engine is not None:
        await run_in_threadpool(engine.dispose)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    database = await _open_database()
    # Planificador en proceso (P10, D-R2-6). Se llama por el módulo para que los
    # tests puedan sustituir scheduler.scheduler_loop.
    stop_event = asyncio.Event()
    scheduler_task = asyncio.create_task(
        scheduler.scheduler_loop(stop_event=stop_event)
    )
    try:
        yield
    finally:
        stop_event.set()
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await _close_database(database)


app = FastAPI(
    title="ConnectsSenior API",
    version="0.0.1",
    description="Backend para la plataforma ConnectsSenior",
    lifespan=lifespan,
)

# httpx registra en INFO la URL completa, que incluye la clave web de Firebase en ?key=.
logging.getLogger("httpx").setLevel(logging.WARNING)

settings = get_settings()
ensure_emulator_not_leaking(settings)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Registro de routers ---
from app.api.contacts_router import router as contacts_router
from app.api.reminders_router import router as reminders_router
from app.api.calls_router import router as calls_router
from app.api.alerts_router import router as alerts_router
from app.api.messages_router import router as messages_router
from app.api.admin_router import router as admin_router

app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(contacts_router, prefix="/api/contacts", tags=["Contacts"])
app.include_router(reminders_router, prefix="/api/reminders", tags=["Reminders"])
app.include_router(calls_router, prefix="/api/calls", tags=["Calls"])
app.include_router(alerts_router, prefix="/api/alerts", tags=["Alerts"])
app.include_router(messages_router, prefix="/api/messages", tags=["Messages"])
app.include_router(admin_router, prefix="/api/admin", tags=["Admin"])


# --- Errores de validación (D-03): 422 con detail en texto ---
_VALIDATION_MESSAGES = {
    "missing": "es obligatorio",
    "string_too_short": "es demasiado corto",
    "string_too_long": "es demasiado largo",
    "json_invalid": "no tiene un formato válido",
}
_DEFAULT_VALIDATION_MESSAGE = "no es válido"
_LOCATION_PREFIXES = ("body", "query", "path", "header", "cookie")
_BODY_FIELD_NAME = "cuerpo"


def _validation_field(error: dict) -> str:
    if error["type"] == "json_invalid":
        return _BODY_FIELD_NAME
    field = ".".join(
        str(part) for part in error["loc"] if part not in _LOCATION_PREFIXES
    )
    return field or _BODY_FIELD_NAME


def _validation_message(error: dict) -> str:
    return _VALIDATION_MESSAGES.get(error["type"], _DEFAULT_VALIDATION_MESSAGE)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Nunca se usan error["input"] ni error["ctx"]: pueden traer la contraseña.
    details = "; ".join(
        f"{_validation_field(error)}: {_validation_message(error)}"
        for error in exc.errors()
    )
    return JSONResponse(status_code=422, content={"detail": f"Datos inválidos: {details}"})


# --- Errores de la base de datos (D-R3-20) ---
# Handlers globales documentados junto al de D-03: 503 para DATABASE_UNAVAILABLE_ERRORS,
# 422 para DataError y 500 para el resto de SQLAlchemyError (sin este último, un
# IntegrityError imprevisto dejaría a uvicorn registrar la traza con el DETAIL de Postgres,
# que puede traer correos). Starlette elige el handler de la clase más cercana en el MRO de
# la excepción, así que el de SQLAlchemyError solo recibe lo que no cubren los otros dos.
# Solo se registra el tipo de la excepción y, si Postgres lo informa, el nombre de la
# restricción: el texto de la excepción trae la sentencia SQL y la causa del driver.


def _database_error_summary(exc: Exception) -> str:
    """Tipo de la excepción y, si existe, la restricción violada; nunca su texto ni su causa."""
    name = type(exc).__name__
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    if isinstance(constraint, str) and constraint:
        return f"{name}, restricción {constraint}"
    return name


async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("La base de datos no respondió (%s)", _database_error_summary(exc))
    return JSONResponse(status_code=503, content={"detail": DATABASE_UNAVAILABLE_DETAIL})


async def database_data_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("La base de datos rechazó un dato (%s)", _database_error_summary(exc))
    return JSONResponse(status_code=422, content={"detail": DATABASE_DATA_ERROR_DETAIL})


async def database_internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "La base de datos falló de forma inesperada (%s)", _database_error_summary(exc)
    )
    return JSONResponse(status_code=500, content={"detail": DATABASE_INTERNAL_ERROR_DETAIL})


for _database_error in db_session.DATABASE_UNAVAILABLE_ERRORS:
    app.add_exception_handler(_database_error, database_unavailable_handler)
app.add_exception_handler(DataError, database_data_error_handler)
app.add_exception_handler(SQLAlchemyError, database_internal_error_handler)


@app.get("/")
async def root():
    return {"message": "ConnectsSenior API is running"}
