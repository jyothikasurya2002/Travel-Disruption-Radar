"""Plain-English summary of an assessment, written by Claude.

The model only gets the already computed result (score, level, confidence,
action) and the evidence signals, each with an id like S3. It can't change
any of it, it just explains it. The reply is JSON {summary, cited_signals},
and grounding_problems() rejects it if it cites evidence that doesn't
exist, names an airport not on the trip, or quotes a different score.

On any failure (no key, timeout, refusal, bad JSON, failed check) we use the
rule-based summary instead and record why.
"""
import asyncio
import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

import anthropic
from pydantic import BaseModel, ValidationError

from app.geo import load_airports
from app.models import Action, Driver, Mode, Severity, Signal, Trip

log = logging.getLogger(__name__)

MODEL = os.getenv("SUMMARY_MODEL", "claude-opus-5-5")
# Summarising given facts is a simple task: low effort keeps it fast and cheap.
EFFORT = "low"
TIMEOUT_S = 30
MAX_EVIDENCE = 25
MAX_SUMMARY_CHARS = 900
# Opus 5.5 safety classifiers can decline; "default" re-runs a declined
# request on Anthropic's recommended fallback model, server-side.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You write short travel-disruption briefings for a corporate travel operations team.

You receive a risk assessment that a rules engine has ALREADY computed, and the evidence signals behind it. Each signal has an id such as S3.

Rules:
- Use only facts present in the input. Do not add hazards, places, times, airlines or numbers that are not in the evidence.
- Do not change or second-guess the score, level, confidence or recommended action. Explain them.
- Cite the ids of every signal your summary relies on in cited_signals.
- If confidence is not "high", say briefly why in plain words.
- Translate jargon: say "low cloud and poor visibility" rather than IFR or LIFR, "airport forecast" rather than TAF, "official airport notice" rather than NOTAM.
- 2 to 4 sentences, plain English, no markdown, no greeting."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "cited_signals": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "cited_signals"],
    "additionalProperties": False,
}


class LLMSummary(BaseModel):
    summary: str
    cited_signals: List[str]


def llm_enabled() -> bool:
    if os.getenv("SUMMARY_MODE", "").lower() == "rules":
        return False
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


_client: Optional[anthropic.AsyncAnthropic] = None


def _default_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        # One retry: a briefing that arrives 40 s late is worse than the fallback.
        _client = anthropic.AsyncAnthropic(timeout=float(TIMEOUT_S), max_retries=1)
    return _client


async def summarize(
    trip: Trip, score: int, level: str, confidence: str, confidence_reasons: List[str],
    drivers: List[Driver], action: Action, signals: List[Signal], rules_summary: str,
    client: Optional[anthropic.AsyncAnthropic] = None,
) -> Tuple[str, str, Optional[str]]:
    """Returns (summary, source "llm" | "rules", note explaining a fallback)."""
    if client is None and not llm_enabled():
        return rules_summary, "rules", "AI summary off: no ANTHROPIC_API_KEY configured"

    payload, ids = build_payload(trip, score, level, confidence, confidence_reasons, drivers, action, signals)
    try:
        resp = await asyncio.wait_for(_call(client or _default_client(), payload), timeout=TIMEOUT_S + 5)
    except anthropic.AuthenticationError:
        return rules_summary, "rules", "AI summary unavailable: API key rejected"
    except anthropic.RateLimitError:
        return rules_summary, "rules", "AI summary unavailable: rate limited"
    except anthropic.APIStatusError as e:
        log.warning("summary API error %s: %s", e.status_code, e.message)
        return rules_summary, "rules", f"AI summary unavailable: API error {e.status_code}"
    except (anthropic.APIConnectionError, asyncio.TimeoutError):
        return rules_summary, "rules", "AI summary unavailable: timed out or unreachable"

    if resp.stop_reason == "refusal":
        return rules_summary, "rules", "AI summary declined by the model"
    if resp.stop_reason == "max_tokens":
        return rules_summary, "rules", "AI summary was cut off"

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = LLMSummary.model_validate_json(text)
    except ValidationError:
        return rules_summary, "rules", "AI summary returned malformed output"

    problems = grounding_problems(out, ids, trip, score)
    if problems:
        log.warning("summary rejected: %s | %s", problems, out.summary)
        return rules_summary, "rules", f"AI summary rejected by grounding check: {problems[0]}"
    return out.summary.strip(), "llm", None


async def _call(client: anthropic.AsyncAnthropic, payload: Dict):
    return await client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        messages=[{"role": "user", "content": json.dumps(payload, indent=1, default=str)}],
    )


def build_payload(trip: Trip, score: int, level: str, confidence: str, confidence_reasons: List[str],
                  drivers: List[Driver], action: Action, signals: List[Signal]) -> Tuple[Dict, List[str]]:
    """Only what the briefing needs, with stable ids the model must cite."""
    findings = [s for s in signals if s.severity > Severity.none][:MAX_EVIDENCE]
    clear = [s for s in signals if s.severity == Severity.none][: max(0, MAX_EVIDENCE - len(findings))]
    evidence, ids = [], []
    for i, s in enumerate(findings + clear, start=1):
        sid = f"S{i}"
        ids.append(sid)
        evidence.append({
            "id": sid, "source": s.source, "where": s.location, "part_of_trip": s.area,
            "severity": Severity(s.severity).name, "finding": s.headline,
            "applies_to_travel_date": s.valid_for_travel_date,
        })
    payload = {
        "trip": {
            "from": trip.origin.name, "to": trip.destination.name,
            "date": trip.travel_date.isoformat(),
            "mode": "flight" if trip.mode == Mode.flight else "drive",
            "airports_checked": [a.iata for p in (trip.origin, trip.destination) for a in p.airports],
        },
        "assessment": {
            "score_out_of_100": score, "level": level,
            "score_breakdown": [f"+{d.points}: {d.reason}" for d in drivers],
            "confidence": confidence, "confidence_reasons": confidence_reasons,
            "recommended_action": action.headline, "action_steps": action.steps,
        },
        "evidence": evidence,
    }
    return payload, ids


def grounding_problems(out: LLMSummary, ids: List[str], trip: Trip, score: int) -> List[str]:
    """Cheap, deterministic checks that catch the common ways an LLM drifts."""
    problems = []
    text = out.summary

    unknown = [c for c in out.cited_signals if c not in ids]
    if unknown:
        problems.append(f"cites evidence that doesn't exist ({', '.join(unknown)})")
    if score > 0 and not out.cited_signals:
        problems.append("cites no evidence for a non-zero risk")

    trip_codes = {a.iata for p in (trip.origin, trip.destination) for a in p.airports}
    all_codes = {a.iata for a in load_airports()}
    foreign = sorted({w for w in re.findall(r"\b[A-Z]{3}\b", text) if w in all_codes and w not in trip_codes})
    if foreign:
        problems.append(f"mentions airports not on this trip ({', '.join(foreign)})")

    for quoted in re.findall(r"(\d{1,3})\s*/\s*100", text):
        if int(quoted) != score:
            problems.append(f"quotes score {quoted}/100 but the engine computed {score}/100")

    if not text.strip():
        problems.append("empty summary")
    elif len(text) > MAX_SUMMARY_CHARS:
        problems.append("summary too long")
    return problems
