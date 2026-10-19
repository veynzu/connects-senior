"""Planificador en proceso (P10, D-R2-6): ticks periódicos sin dependencias nuevas.

Cada módulo que necesita trabajo periódico (escalamiento de alertas RF-24,
recordatorios RF-28) registra una función `def` sin argumentos con register_tick.
El lifespan de app.main arranca scheduler_loop, que cada
SCHEDULER_INTERVAL_SECONDS ejecuta todos los ticks en el threadpool (FCM y el
Admin SDK son síncronos y hacen red) y aísla sus excepciones: un tick que falla
se registra en el log y nunca detiene el bucle ni a los demás ticks.
"""
import asyncio
import logging
import threading
from collections.abc import Callable

from starlette.concurrency import run_in_threadpool


SCHEDULER_INTERVAL_SECONDS = 30.0

TickFn = Callable[[], None]

logger = logging.getLogger(__name__)

_ticks: dict[str, TickFn] = {}
_ticks_lock = threading.Lock()


def register_tick(name: str, fn: TickFn) -> None:
    """Registra un tick; si ya existía uno con el mismo nombre, lo sobrescribe."""
    with _ticks_lock:
        _ticks[name] = fn


def unregister_tick(name: str) -> None:
    """Quita un tick; un nombre desconocido se ignora."""
    with _ticks_lock:
        _ticks.pop(name, None)


def registered_ticks() -> dict[str, TickFn]:
    """Copia del registro actual (modificarla no altera el registro)."""
    with _ticks_lock:
        return dict(_ticks)


async def run_ticks_once() -> None:
    """Ejecuta una vez cada tick registrado; nunca propaga sus excepciones."""
    for name, fn in registered_ticks().items():
        try:
            await run_in_threadpool(fn)
        except Exception:
            logger.exception("El tick %s falló", name)


async def scheduler_loop(
    interval_seconds: float = SCHEDULER_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Espera interval_seconds (o hasta stop_event) antes de cada ronda de ticks.

    Termina cuando stop_event está activado; asyncio.CancelledError se propaga.
    """
    if stop_event is None:
        stop_event = asyncio.Event()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            await run_ticks_once()
