# Travel Disruption Radar

Enter a U.S. trip (e.g. **New York → San Francisco, tomorrow**, by air or road)
and get an assessment of potential disruptions, the evidence behind it, and a
recommended action.

## Run it

Requires Python 3.10+ built against a modern OpenSSL (macOS's bundled
Python 3.9 uses LibreSSL 2.8, which fails the TLS handshake with OSRM).

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optionally add ANTHROPIC_API_KEY
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

## Data sources (all free, no paid access)

| Source | Used for | Mode |
|---|---|---|
| FAA NAS Status (`nasstatus.faa.gov`) | Live ground stops, ground delay programs, closures | Flight |
| NWS `api.weather.gov` | Official weather alerts + forecasts | Both |
| AviationWeather.gov TAF/METAR | Airport forecasts: ceilings, visibility, wind | Flight |
| Open-Meteo forecast | Precip / snow / gusts up to 16 days out | Both |
| Open-Meteo geocoding | City name → coordinates | Both |
| OurAirports dataset | City → nearby commercial airports | Flight |
| OSRM | Driving route geometry for sampling points along the road | Drive |

## Architecture

```
TripRequest → resolve locations → connectors (parallel) → Signals
            → risk engine (deterministic score + reasons) → action
            → summary (LLM, grounded in Signals; rule-based fallback)
```

## Decision log

**Common `Signal` contract.** Every source returns the same shape (source,
location, severity, headline, raw evidence, link, timestamp). The risk engine
and UI never special-case a source, so adding one is a single new file.

**Source health is reported, not hidden.** If the FAA feed times out, the
assessment says so. "No evidence of risk" and "we couldn't check" must never
look the same to an ops user.

**Input resolution (step 2)**
- *Airports:* bundled a filtered copy of OurAirports (502 U.S. airports with
  scheduled service, rebuilt via `scripts/build_airports.py`). No runtime
  dependency, instant lookups. A city maps to hubs within 100 km (e.g. NYC →
  LGA/EWR/JFK), falling back to medium airports only when no hub exists, so
  Chicago doesn't list Gary. Typing an airport code (`SFO`) pins that airport.
- *Ambiguous cities:* exact name match first, then largest population.
  "Portland" → Portland, OR; "Portland, ME" overrides. Resolved names are
  echoed back so the user can catch a wrong guess.
- *Dates:* small dependency-free parser for what ops staff actually type
  ("tomorrow", "friday", "next friday", "10/24", ISO). "Next friday" = the
  Friday after the coming one. Dates beyond 16 days are rejected: forecasts
  don't reach that far and a confident-looking score would be misleading.
- *Driving:* OSRM route, sampled every ~150 km (max 12 points) with an
  estimated arrival time at each, so weather is checked for *when you'll be
  there*, not just at the endpoints. Assumes constant speed, non-stop.

**Connectors (step 3)**
- *Concurrent and isolated.* All sources run in parallel (~1-2 s total) with
  a 20 s budget each. A failing source becomes a `SourceStatus(ok=False)`,
  never a crash and never a silent "all clear".
- *"Checked and clear" is evidence too.* Connectors emit severity-0 signals
  ("JFK: no ground stops…") so the user sees what was actually verified.
- *Exposure windows.* A hazard only counts if it overlaps when the traveller
  is there: 05:00-24:00 local at each airport, or ±2 h around the ETA at each
  road sample (departure assumed 08:00 local).
- *FAA:* NOTAM closures have exact UTC windows, so they're predictive.
  GA-only restrictions (e.g. LAX "CLSD TO NON SKED TRANSIENT GA", active for
  months) and overnight closures (LGA nightly) are filtered out. Live ground
  stops/delays usually clear within hours, so for future dates they're shown
  as context, capped at low severity. Parsing keys off element tags from the
  FAA's published `AirportStatus.dtd`, not free-text section names.
- *TAF:* thresholds are the FAA flight categories (LIFR/IFR/MVFR) plus
  thunderstorms, freezing precipitation, snow and gusts, the conditions that
  actually trigger ground delay programs. PROB30/40 groups are downgraded one
  level. TAFs only reach ~24-30 h, and the tool says so instead of guessing.
- *NWS:* queried per point, so only zones on the trip count. ~70% of active
  U.S. alerts are marine; those and agricultural ones (frost/freeze) are
  dropped. Travel-critical events get a severity floor (Dense Fog Advisory →
  moderate, Blizzard Warning → severe); watches are capped at moderate.
- *Open-Meteo:* the only source covering 16 days and every road sample, in
  one batched request. Fog matters for flights; steady rain only for drives.

**Risk engine (step 4)**: [app/risk.py](app/risk.py)
- *Deterministic and auditable.* Every point in the 0-100 score maps to a
  `Driver` line naming the signal behind it. The LLM never sets the score.
- *Scoring:* the worst finding sets the base (low 10 / moderate 25 / high 50
  / severe 80). Each other **independent source** that sees moderate+
  conditions **in the same part of the trip** (origin / destination / route)
  adds +10 (max 2). Remaining findings add +3 each (max +10). Levels: <10
  Minimal, <25 Low, <50 Moderate, <75 High, else Severe.
- *Why "same part of the trip":* an early version counted gusty winds at
  Norfolk as "corroborated" by an alert in Atlanta. Independent agreement
  only means something about the same place. Covered by a regression test.
- *Confidence is separate from risk* (high/medium/low). It drops for failed
  sources, trips more than 3 or 7 days out, airports whose TAF doesn't reach
  the travel date yet, and route points beyond the forecast range. A quiet
  result with low confidence is **not** reported as "no action": it says
  "re-check closer to the date".
- *Actions are tiered* (none → monitor → mitigate → rebook) and specific:
  if one airport in a multi-airport city is the problem, the action names a
  clear alternate ("consider flying via EWR"). For drives, it names the
  affected stretches. Every failed source becomes a "verify manually" step.

**Interface (step 6)**: [static/index.html](static/index.html)
- One static page, vanilla JS, no build step: nothing to install beyond
  the Python server, and it's easy to read in a review.
- Laid out in the order an ops person asks the questions: *what did you
  understand* (resolved cities, airports and date echoed back) → *how bad*
  (score, level, confidence, summary labelled AI or rule-based) → *what do I
  do* (action steps) → *why* (point-by-point breakdown) → *show me*
  (evidence by Origin / Route / Destination, raw TAF/NOTAM/alert text, links
  to each source) → *what was checked* (per-source status and latency).
- "Checked and clear" items are folded away but countable, so quiet results
  still show their work. Context-only items (e.g. live FAA delays for a
  future trip) are tagged as not counted in the score.
- All external text is HTML-escaped and only http(s) links are rendered:
  NWS alert text is third-party input.
- Results live in the URL (`/?origin=…&destination=…&date=…&mode=…`), so an
  assessment can be shared as a link and re-run.
- Light/dark mode; verified at desktop width and at a 390 px phone width.

## Testing

```bash
.venv/bin/python -m pytest -q
```

The live FAA feed is often quiet, so `tests/test_faa.py` uses fixtures
built from the DTD to prove ground stops, delay programs, delays and closure
filtering work. Rule tests cover TAF categories, NWS filtering/severity and
Open-Meteo thresholds.

Debug endpoints: `/api/resolve` (what we understood) and `/api/signals`
(raw evidence per source, before scoring).

## Assumptions

- "Today" is the server's local date; a real deployment should use the
  origin's timezone.
- Long drives (e.g. NY → SF, ~51 h) are timed as if driven non-stop.
- No flight number or departure time is asked for, so a flight is exposed
  to the whole operating day at both ends; drives depart at 08:00 local.
- The flight path itself (en-route weather) isn't checked; origin and
  destination conditions drive the vast majority of delays.
- Scope is the contiguous U.S. + AK/HI/PR airports present in OurAirports.

## How and where AI was used

**1. Inside the product: the plain-English briefing only** ([app/summary.py](app/summary.py))

The score, level, confidence and action come from the deterministic rules
engine. Claude (`claude-opus-5-5`) only turns that result into 2-4 sentences
an ops person can act on. That boundary is deliberate: a risk score must be
reproducible and auditable, and an LLM is neither. Writing clear, jargon-free
prose from structured facts is where it adds real value.

Guardrails:
- *Grounded input:* the model gets the computed assessment plus evidence
  signals with ids (S1, S2…). It isn't asked to find anything out.
- *Structured output* (`output_config.format` JSON schema):
  `{summary, cited_signals}`, so it must say which evidence it used.
- *Grounding check* before display. It rejects text that cites
  non-existent evidence, cites nothing for a non-zero risk, names an airport
  not on the trip, or quotes a different score than the engine computed.
- *Graceful degradation:* no key, timeout (30 s, 1 retry), rate limit,
  refusal, malformed JSON or a failed check all fall back to the rule-based
  summary. `summary_source` and `summary_note` tell the UI which one
  you're reading and why.
- *Refusal fallback:* `fallbacks: "default"` (server-side) re-runs a
  safety-declined request on Anthropic's recommended fallback model.
- *Effort `low`:* summarising given facts is simple; low effort keeps
  latency and cost down.
- Tested against a mock Claude endpoint (`tests/test_summary.py`): request
  shape, plus every failure and hallucination path. No key needed.

**2. Building it: Claude Code as a pair programmer**

I used Claude Code throughout, step by step: I set the scope and stack and
made the product decisions, it drafted code, and every step was checked
against live data and tests before moving on. Things that verification
caught, where the first AI-written version was wrong:
- Pinned `anthropic==0.42.0` from memory; the current SDK is 1.x.
  Corrected against the official API reference before writing LLM code.
- "Most populous match" turned *Aspen* into Aspen Hill, MD.
- Corroboration counted an Atlanta alert as confirming Norfolk winds.
- Assumed FAA section names; switched to the tags guaranteed by the FAA's
  published DTD after reading it.
- macOS system Python's old LibreSSL couldn't reach OSRM (environment, not
  code).

The lesson: AI is fast at producing plausible code, and the work is in
checking it against reality: real payloads, published schemas, live runs.

## Before deploying to real users

_Filled in as we build._
