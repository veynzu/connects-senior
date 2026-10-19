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

from app.api.auth_router import router as auth_router
from app.core import scheduler
from app.core.config import get_settings
from app.core.firebase import ensure_emulator_not_leaking


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
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


@app.get("/")
async def root():
    return {"message": "ConnectsSenior API is running"}
