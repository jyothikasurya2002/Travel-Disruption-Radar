"""Connector registry. Adding a source = one module + one line here."""
import asyncio
from typing import List, Tuple

from app.connectors import faa, nws, openmeteo, taf
from app.connectors.base import run_source
from app.models import Mode, Signal, SourceStatus, Trip

CONNECTORS = {
    Mode.flight: [
        (faa.SOURCE, faa.fetch),
        (taf.SOURCE, taf.fetch),
        (nws.SOURCE, nws.fetch),
        (openmeteo.SOURCE, openmeteo.fetch),
    ],
    Mode.drive: [
        (nws.SOURCE, nws.fetch),
        (openmeteo.SOURCE, openmeteo.fetch),
    ],
}


async def gather_signals(trip: Trip) -> Tuple[List[Signal], List[SourceStatus]]:
    results = await asyncio.gather(*(run_source(name, fn, trip) for name, fn in CONNECTORS[trip.mode]))
    signals = [s for sigs, _ in results for s in sigs]
    statuses = [st for _, st in results]
    return signals, statuses
