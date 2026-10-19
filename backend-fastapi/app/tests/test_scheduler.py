import asyncio
import logging
import threading

import pytest

from app.core import scheduler


SCHEDULER_LOGGER = "app.core.scheduler"
FAST_INTERVAL = 0.01
WAIT_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _empty_tick_registry():
    """Guarda el registro real (ticks de los routers), lo vacía y lo restaura."""
    saved = scheduler.registered_ticks()
    for name in saved:
        scheduler.unregister_tick(name)
    try:
        yield
    finally:
        for name in scheduler.registered_ticks():
            scheduler.unregister_tick(name)
        for name, fn in saved.items():
            scheduler.register_tick(name, fn)


class CountingTick:
    def __init__(self) -> None:
        self.count = 0
        self.thread_ids: list[int] = []
        self._lock = threading.Lock()

    def __call__(self) -> None:
        with self._lock:
            self.count += 1
            self.thread_ids.append(threading.get_ident())


async def _wait_until(condition, timeout: float = WAIT_TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            pytest.fail("La condición no se cumplió a tiempo.")
        await asyncio.sleep(FAST_INTERVAL)


def test_default_interval_is_30_seconds():
    assert scheduler.SCHEDULER_INTERVAL_SECONDS == 30.0


def test_register_tick_adds_and_overwrites_same_name():
    first = CountingTick()
    second = CountingTick()

    scheduler.register_tick("demo", first)
    assert scheduler.registered_ticks() == {"demo": first}

    scheduler.register_tick("demo", second)
    assert scheduler.registered_ticks() == {"demo": second}


def test_unregister_tick_removes_and_ignores_unknown():
    tick = CountingTick()
    scheduler.register_tick("demo", tick)

    scheduler.unregister_tick("demo")
    scheduler.unregister_tick("desconocido")

    assert scheduler.registered_ticks() == {}


def test_registered_ticks_returns_copy():
    tick = CountingTick()
    scheduler.register_tick("demo", tick)

    snapshot = scheduler.registered_ticks()
    snapshot.clear()
    snapshot["intruso"] = CountingTick()

    assert scheduler.registered_ticks() == {"demo": tick}


def test_fixture_starts_with_empty_registry():
    assert scheduler.registered_ticks() == {}


@pytest.mark.asyncio
async def test_run_ticks_once_calls_all_ticks():
    first = CountingTick()
    second = CountingTick()
    scheduler.register_tick("first", first)
    scheduler.register_tick("second", second)

    await scheduler.run_ticks_once()

    assert first.count == 1
    assert second.count == 1


@pytest.mark.asyncio
async def test_run_ticks_once_with_empty_registry_does_nothing():
    await scheduler.run_ticks_once()

    assert scheduler.registered_ticks() == {}


@pytest.mark.asyncio
async def test_run_ticks_once_runs_ticks_in_threadpool():
    tick = CountingTick()
    scheduler.register_tick("demo", tick)
    loop_thread_id = threading.get_ident()

    await scheduler.run_ticks_once()

    assert tick.count == 1
    assert tick.thread_ids[0] != loop_thread_id


@pytest.mark.asyncio
async def test_run_ticks_once_isolates_failing_tick(caplog):
    caplog.set_level(logging.ERROR, logger=SCHEDULER_LOGGER)
    after = CountingTick()

    def failing_tick() -> None:
        raise RuntimeError("fallo del tick")

    scheduler.register_tick("boom", failing_tick)
    scheduler.register_tick("after", after)

    await scheduler.run_ticks_once()

    assert after.count == 1
    assert "El tick boom falló" in caplog.text
    records = [r for r in caplog.records if r.name == SCHEDULER_LOGGER]
    assert records and records[0].exc_info is not None


@pytest.mark.asyncio
async def test_scheduler_loop_runs_ticks_and_stops():
    tick = CountingTick()
    scheduler.register_tick("demo", tick)
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        scheduler.scheduler_loop(interval_seconds=FAST_INTERVAL, stop_event=stop_event)
    )
    await _wait_until(lambda: tick.count >= 1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=WAIT_TIMEOUT)

    assert task.done()
    assert task.exception() is None
    assert tick.count >= 1


@pytest.mark.asyncio
async def test_scheduler_loop_with_stop_event_set_runs_no_ticks():
    tick = CountingTick()
    scheduler.register_tick("demo", tick)
    stop_event = asyncio.Event()
    stop_event.set()

    await asyncio.wait_for(
        scheduler.scheduler_loop(interval_seconds=FAST_INTERVAL, stop_event=stop_event),
        timeout=WAIT_TIMEOUT,
    )

    assert tick.count == 0


@pytest.mark.asyncio
async def test_scheduler_loop_waits_interval_before_first_tick():
    tick = CountingTick()
    scheduler.register_tick("demo", tick)
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        scheduler.scheduler_loop(interval_seconds=60.0, stop_event=stop_event)
    )
    await asyncio.sleep(0.05)
    assert tick.count == 0

    stop_event.set()
    await asyncio.wait_for(task, timeout=WAIT_TIMEOUT)

    assert tick.count == 0


@pytest.mark.asyncio
async def test_scheduler_loop_keeps_running_after_failing_tick(caplog):
    caplog.set_level(logging.ERROR, logger=SCHEDULER_LOGGER)
    tick = CountingTick()

    def failing_tick() -> None:
        raise ValueError("fallo del tick")

    scheduler.register_tick("boom", failing_tick)
    scheduler.register_tick("demo", tick)
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        scheduler.scheduler_loop(interval_seconds=FAST_INTERVAL, stop_event=stop_event)
    )
    await _wait_until(lambda: tick.count >= 2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=WAIT_TIMEOUT)

    assert task.exception() is None
    assert "El tick boom falló" in caplog.text


@pytest.mark.asyncio
async def test_scheduler_loop_propagates_cancellation():
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        scheduler.scheduler_loop(interval_seconds=60.0, stop_event=stop_event)
    )
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_scheduler_loop_without_stop_event_runs_until_cancelled():
    tick = CountingTick()
    scheduler.register_tick("demo", tick)

    task = asyncio.create_task(scheduler.scheduler_loop(interval_seconds=FAST_INTERVAL))
    await _wait_until(lambda: tick.count >= 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert tick.count >= 1
