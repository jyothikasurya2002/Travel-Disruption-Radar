"""Single place for outbound HTTP so every source gets the same timeout and
User-Agent (api.weather.gov rejects requests without one)."""
import os
from typing import Any, Dict, Optional

import httpx

TIMEOUT = httpx.Timeout(12.0, connect=5.0)


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": os.getenv(
            "NWS_USER_AGENT", "travel-disruption-radar (contact@example.com)"
        ),
        "Accept": "application/geo+json, application/json;q=0.9, */*;q=0.8",
    }


async def get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def get_text(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=_headers()) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.text
