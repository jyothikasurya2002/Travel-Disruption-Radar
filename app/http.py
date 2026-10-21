"""Single place for outbound HTTP so every source gets the same timeout,
User-Agent (api.weather.gov rejects requests without one) and retry policy."""
import asyncio
import os
from typing import Any, Dict, Optional

import httpx

TIMEOUT = httpx.Timeout(12.0, connect=5.0)
# Free public APIs have brief blips (dropped connections, 502/503s). One quick
# retry absorbs most of them; anything longer is reported as a failed source.
RETRIES = 1
RETRY_DELAY_S = 0.5


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": os.getenv(
            "NWS_USER_AGENT", "travel-disruption-radar (contact@example.com)"
        ),
        "Accept": "application/geo+json, application/json;q=0.9, */*;q=0.8",
    }


async def _get(url: str, params: Optional[Dict[str, Any]]) -> httpx.Response:
    for attempt in range(RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as client:
                resp = await client.get(url, params=params)
            if resp.status_code < 500 or attempt == RETRIES:
                resp.raise_for_status()
                return resp
        except httpx.TransportError:
            if attempt == RETRIES:
                raise
        await asyncio.sleep(RETRY_DELAY_S)
    raise RuntimeError("unreachable")


async def get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    return (await _get(url, params)).json()


async def get_text(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    return (await _get(url, params)).text
