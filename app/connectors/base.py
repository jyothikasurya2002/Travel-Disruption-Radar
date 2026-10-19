"""Run connectors concurrently and isolate their failures."""
import asyncio
import logging
import time
from typing import Awaitable, Callable, List, Tuple

from app.models import Signal, SourceStatus, Trip

log = logging.getLogger(__name__)

Connector = Callable[[Trip], Awaitable[List[Signal]]]

# Whole-request budget per source; a slow API degrades the answer, never blocks it.
SOURCE_TIMEOUT_S = 20


async def run_source(name: str, fn: Connector, trip: Trip) -> Tuple[List[Signal], SourceStatus]:
    started = time.perf_counter()
    try:
        signals = await asyncio.wait_for(fn(trip), timeout=SOURCE_TIMEOUT_S)
        ok, error = True, None
    except Exception as e:  # noqa: BLE001 - any failure becomes a reported status
        log.exception("source %s failed", name)
        signals, ok = [], False
        error = f"{type(e).__name__}: {e}"[:200] or type(e).__name__
    latency = int((time.perf_counter() - started) * 1000)
    return signals, SourceStatus(source=name, ok=ok, error=error, latency_ms=latency)
