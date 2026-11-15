"""Proactive messages to astronauts, built from detected patterns.

Rules:
* at most one message per astronaut every PROACTIVE_MIN_INTERVAL_DAYS mission days (default 7);
* only for medium or high findings; urgent symptoms go to the care team instead;
* never sent to an astronaut who turned proactive messages off;
* plain templates, no AI: what was seen, one practical suggestion, and the
  options to talk to a person or to turn these messages off.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AstronautPreference, PatternDetection, ProactiveMessage
from app.services.patterns import PATTERN_LABELS, SEVERITY_ORDER

# Highest first when several patterns are active.
PRIORITY = [
    "wellbeing_cluster", "fever", "rising_blood_pressure", "sleep_debt", "elevated_stress", "low_mood",
    "fatigue", "elevated_glucose", "elevated_heart_rate", "low_intake", "recurring_symptom",
]

SUGGESTIONS = {
    "wellbeing_cluster": "A lot is piling up at once. Talking it through with someone from the support team often helps, and you can ask for that right here.",
    "fever": "Rest and fluids help, and the flight surgeon may want to check in. Please log any new symptoms in your check-in.",
    "rising_blood_pressure": "Keeping your exercise sessions and fluid intake regular helps. The flight surgeon will review the trend with you.",
    "sleep_debt": "A consistent sleep window, dimmer lights and screens off before bed can help. If the schedule makes that hard, the team can look at it with you.",
    "elevated_stress": "Short breaks, exercise and a call with family or friends can help. If you would like to talk to someone, ask for support here.",
    "low_mood": "Low days happen on long missions. Reaching out to crewmates, family or the support team can make a difference.",
    "fatigue": "Check whether sleep, meals and exercise have changed lately. If fatigue keeps up, the flight surgeon can take a look.",
    "elevated_glucose": "Regular meals and exercise sessions help keep glucose steady. The flight surgeon will keep an eye on the trend.",
    "elevated_heart_rate": "Hydration, sleep and stress all affect resting heart rate. Let the team know if you feel palpitations or dizziness.",
    "low_intake": "Eating enough matters for energy, bones and muscles. If food is unappealing, the team can suggest alternatives.",
    "recurring_symptom": "If it keeps coming back or gets worse, the flight surgeon can help.",
}

OPT_OUT_NOTE = "You can turn these messages off at any time in Settings."


def proactive_enabled(session: Session, astronaut_id: str) -> bool:
    pref = session.get(AstronautPreference, astronaut_id)
    return pref is None or pref.proactive_messages


def last_message_day(session: Session, astronaut_id: str) -> int | None:
    return session.scalar(
        select(func.max(ProactiveMessage.mission_day)).where(ProactiveMessage.astronaut_id == astronaut_id)
    )


def compose(findings: list[PatternDetection]) -> tuple[str, str, list[str]]:
    ranked = sorted(
        findings,
        key=lambda f: (-SEVERITY_ORDER[f.severity], PRIORITY.index(f.pattern) if f.pattern in PRIORITY else 99),
    )
    main = ranked[0]
    title = f"Check-in from your health monitor: {PATTERN_LABELS.get(main.pattern, main.pattern).lower()}"
    others = [f.summary for f in ranked[1:3]]
    lines = [main.summary, SUGGESTIONS.get(main.pattern, "")]
    if others:
        lines.append("Also noticed: " + " ".join(others))
    lines.append(OPT_OUT_NOTE)
    patterns = [f.pattern if not f.detail else f"{f.pattern}:{f.detail}" for f in ranked]
    return title, "\n\n".join(line for line in lines if line), patterns


def maybe_send(session: Session, mission_id: str, astronaut_id: str, day: int, min_interval_days: int) -> ProactiveMessage | None:
    if not proactive_enabled(session, astronaut_id):
        return None
    last = last_message_day(session, astronaut_id)
    if last is not None and day - last < min_interval_days:
        return None
    findings = [
        f for f in session.scalars(
            select(PatternDetection).where(
                PatternDetection.astronaut_id == astronaut_id,
                PatternDetection.mission_id == mission_id,
                PatternDetection.as_of_day == day,
            )
        )
        if f.severity in ("medium", "high") and f.pattern in SUGGESTIONS
    ]
    if not findings:
        return None
    title, body, patterns = compose(findings)
    message = ProactiveMessage(
        astronaut_id=astronaut_id, mission_id=mission_id, mission_day=day, patterns=patterns,
        title=title, body=body, actions=["view_details", "request_support", "turn_off_messages"],
    )
    session.add(message)
    session.flush()
    return message
