"""AI summary path, tested against a fake Claude endpoint (no key, no cost).

Proves the request we send is right, and that every way the LLM can fail or
drift (refusal, API error, bad JSON, invented airport, wrong score, made-up
citation) falls back to the rule-based summary with a reason.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from app import summary
from app.models import Action, Driver, Severity, Signal
from app.timewin import local_today
from tests.helpers import ny_to_sf

TRIP = ny_to_sf(local_today("America/New_York") + timedelta(days=1))
SIGNALS = [
    Signal(source="AviationWeather TAF", location="JFK", area="origin", category="weather",
           severity=Severity.high, headline="JFK forecast: thunderstorms (TSRA) from Tue 3:00 PM EDT",
           observed_at=datetime.now(timezone.utc)),
    Signal(source="NWS Alerts", location="SFO", area="destination", category="alert",
           severity=Severity.none, headline="No travel-relevant NWS alerts for 1 checked location(s)",
           observed_at=datetime.now(timezone.utc)),
]
DRIVERS = [Driver(points=50, reason="Worst finding: JFK thunderstorms", source="AviationWeather TAF", location="JFK")]
ACTION = Action(level="mitigate", headline="Take action to reduce disruption", steps=["Move to an earlier flight."])
RULES = "rules fallback text"


def fake_client(captured, text=None, stop_reason="end_turn", status=200):
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text or ""}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 40},
        })
    return AsyncAnthropic(api_key="test-key", max_retries=0,
                          http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


def run(client):
    return asyncio.run(summary.summarize(
        TRIP, 50, "High", "medium", ["Trip is 1 day out"], DRIVERS, ACTION, SIGNALS, RULES, client=client))


def reply(text, cited=("S1",)):
    return json.dumps({"summary": text, "cited_signals": list(cited)})


GOOD = reply("Thunderstorms are forecast at JFK from 3 PM on the travel day, so delays are likely "
             "(High risk, 50/100). Move to an earlier flight.")


def test_grounded_summary_is_used_and_request_is_correct():
    captured = {}
    text, source, note = run(fake_client(captured, GOOD))
    assert (source, note) == ("llm", None) and "Thunderstorms" in text

    body = captured["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["headers"]["anthropic-beta"]
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body  # Opus 5.5: always adaptive; effort is the control
    sent = json.loads(body["messages"][0]["content"])
    assert [e["id"] for e in sent["evidence"]] == ["S1", "S2"]
    assert sent["assessment"]["score_out_of_100"] == 50


def test_invented_airport_is_rejected():
    _, source, note = run(fake_client({}, reply("Storms at ORD will ripple to JFK.")))
    assert source == "rules" and "ORD" in note


def test_wrong_score_is_rejected():
    _, source, note = run(fake_client({}, reply("Risk is 80/100 due to JFK storms.")))
    assert source == "rules" and "80/100" in note


def test_made_up_citation_is_rejected():
    _, source, note = run(fake_client({}, reply("JFK storms.", cited=("S1", "S99"))))
    assert source == "rules" and "S99" in note


def test_uncited_risk_is_rejected():
    _, source, note = run(fake_client({}, reply("Storms at JFK.", cited=())))
    assert source == "rules" and "no evidence" in note


def test_refusal_falls_back():
    _, source, note = run(fake_client({}, "", stop_reason="refusal"))
    assert source == "rules" and "declined" in note


def test_api_error_falls_back():
    text, source, note = run(fake_client({}, status=500))
    assert (text, source) == (RULES, "rules") and "500" in note


def test_malformed_output_falls_back():
    _, source, note = run(fake_client({}, "not json"))
    assert source == "rules" and "malformed" in note


def test_no_key_means_rules_without_calling_the_api(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    text, source, note = asyncio.run(summary.summarize(
        TRIP, 50, "High", "medium", [], DRIVERS, ACTION, SIGNALS, RULES))
    assert (text, source) == (RULES, "rules") and "ANTHROPIC_API_KEY" in note
