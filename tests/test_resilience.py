"""Flaky upstream APIs: retry once, then fail clearly, never crash."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import http, main


def _patch_transport(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(http, "RETRY_DELAY_S", 0)
    monkeypatch.setattr(http.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_one_blip_is_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json={"ok": True})

    _patch_transport(monkeypatch, handler)
    assert asyncio.run(http.get_json("https://example.test/x")) == {"ok": True}
    assert len(calls) == 2


def test_server_error_is_retried_then_raised(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(http.get_json("https://example.test/x"))
    assert len(calls) == 2


def test_client_error_is_not_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(http.get_json("https://example.test/x"))
    assert len(calls) == 1


def test_geocoder_outage_returns_clear_503_not_a_crash(monkeypatch):
    async def down(*args, **kwargs):
        raise httpx.ConnectError("connection reset")

    monkeypatch.setattr(main, "resolve_trip", down)
    resp = TestClient(main.app).post("/api/assess", json={"origin": "Atlanta", "destination": "Toledo"})
    assert resp.status_code == 503
    assert "try again" in resp.json()["detail"]
