"""Trip -> signals -> score/confidence/action -> summary."""
from datetime import datetime, timezone

from app import risk
from app.connectors import gather_signals
from app.models import Assessment, Trip
from app.summary import summarize


async def assess(trip: Trip) -> Assessment:
    signals, sources = await gather_signals(trip)
    signals.sort(key=lambda s: (-int(s.severity), not s.valid_for_travel_date, s.source))

    score, drivers = risk.score(signals)
    level, severity = risk.level_for(score)
    conf, conf_reasons = risk.confidence(trip, signals, sources)
    action = risk.recommend(trip, level, conf, signals, sources)
    rules_text = risk.rule_summary(trip, score, level, conf, drivers, action)
    summary, summary_source, summary_note = await summarize(
        trip, score, level, conf, conf_reasons, drivers, action, signals, rules_text,
    )

    return Assessment(
        trip=trip, score=score, overall_severity=severity, level=level, drivers=drivers,
        confidence=conf, confidence_reasons=conf_reasons, action=action,
        summary=summary, summary_source=summary_source, summary_note=summary_note,
        signals=signals, sources=sources, generated_at=datetime.now(timezone.utc),
    )
