"""
ConnectsSenior Backend - FastAPI
Arranque de la aplicación y registro de routers.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth_router import router as auth_router
from app.core.config import get_settings

app = FastAPI(
    title="ConnectsSenior API",
    version="0.0.1",
    description="Backend para la plataforma ConnectsSenior"
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Registro de routers ---
# from app.api.contacts_router import router as contacts_router
# from app.api.reminders_router import router as reminders_router
# from app.api.calls_router import router as calls_router
# from app.api.alerts_router import router as alerts_router

app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
# app.include_router(contacts_router, prefix="/api/contacts", tags=["Contacts"])
# app.include_router(reminders_router, prefix="/api/reminders", tags=["Reminders"])
# app.include_router(calls_router, prefix="/api/calls", tags=["Calls"])
# app.include_router(alerts_router, prefix="/api/alerts", tags=["Alerts"])


@app.get("/")
async def root():
    return {"message": "ConnectsSenior API is running"}
