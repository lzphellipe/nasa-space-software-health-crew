"""Services of the CosmoMedic app layer: alerts sync, Orbi proactive notes, ground message status."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cosmo import logic
from app.cosmo.models import AppAlert, AppAstronaut, AppGroundMessage, AppLog, AppMission, AppPlanMeal
from app.models import AstronautPreference, ProactiveMessage, utcnow
from app.provenance import DERIVED, DEVICE, SIMULATED, USER
from app.services.support import open_request

PROACTIVE_INTERVAL_DAYS = 7

ALERT_SUGGESTIONS = {
    "poor_sleep": "Dimming the lights and screens an hour before bed helps on station. Want a 21:00 wind-down reminder?",
    "low_mood": "Low days happen on long missions. A call with family, time with the crew or a chat with Orbi can help; "
                "you can also ask Mission Control to talk.",
    "fitness_drop": "Keeping the daily 90 minutes of exercise protects muscles and bones. The team can adjust your plan.",
    "bone_loss": "Resistive exercise is the main protection for bones in microgravity. Keep your sessions regular.",
    "menu_fatigue": "Eating enough matters for energy and bones. Try a different item from the pantry or ask the team "
                    "for alternatives.",
    "injury": "Rest the area and follow the medical team's guidance. Update the injury log if it changes.",
}
OPT_OUT_NOTE = "You can turn these messages off at any time in Settings."
LEVEL_ORDER = {"low": 1, "medium": 2, "high": 3}
TYPE_PRIORITY = ["injury", "poor_sleep", "low_mood", "fitness_drop", "bone_loss", "menu_fatigue"]


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def astronaut_logs(session: Session, astronaut_id: str) -> list[AppLog]:
    return list(session.scalars(select(AppLog).where(AppLog.astronaut_id == astronaut_id)))


def evaluate(session: Session, astronaut: AppAstronaut, day: int, m: AppMission | None = None,
             logs: list[AppLog] | None = None) -> logic.DayEvaluation:
    m = m or logic.mission(session)
    return logic.evaluate_day(astronaut, logs if logs is not None else astronaut_logs(session, astronaut.id), day,
                              m.goals if m else {}, plan_kcal_by_day(session, astronaut.id))


def plan_kcal_by_day(session: Session, astronaut_id: str) -> dict[int, float]:
    rows = session.execute(select(AppPlanMeal.mission_day, func.sum(AppPlanMeal.kcal))
                           .where(AppPlanMeal.astronaut_id == astronaut_id).group_by(AppPlanMeal.mission_day))
    return {day: float(kcal or 0) for day, kcal in rows}


def log_provenance(log: AppLog) -> str:
    """seed and simulator: simulated; device: measured by a real device and confirmed; app: typed in."""
    return {"seed": SIMULATED, "simulator": SIMULATED, "device": DEVICE}.get(log.source, USER)


def alert_provenance(alert: AppAlert) -> str:
    return SIMULATED if alert.source == "seed" else DERIVED


# --- alerts --------------------------------------------------------------------------------


def sync_alerts(session: Session, astronaut: AppAstronaut, day: int, send: bool = True) -> list[AppAlert]:
    """Apply the rules for `day`: create or update alerts; resolve rule alerts that no longer fire.

    Alerts loaded from the demo data (source "seed") are kept, and only updated when
    the rules raise the same type at a higher level. Returns new or escalated alerts.
    """
    m = logic.mission(session)
    logs = astronaut_logs(session, astronaut.id)
    ev = evaluate(session, astronaut, day, m, logs)
    candidates = logic.alert_candidates(ev, logs)
    active = list(session.scalars(select(AppAlert).where(
        AppAlert.astronaut_id == astronaut.id, AppAlert.status == "active")))

    def key(a_type: str, detail: str | None) -> tuple:
        return (a_type, detail or "")

    def alert_key(a: AppAlert) -> tuple:
        detail = a.evidence[0] if a.type == "injury" and a.source == "rules" and a.evidence else ""
        return key(a.type, detail)

    by_key = {alert_key(a): a for a in active}
    changed: list[AppAlert] = []
    fired = set()
    now = utcnow()
    for c in candidates:
        k = key(c["type"], c.get("detail"))
        if c["type"] == "injury":
            # a seed injury alert covers the same injury log
            seed = next((a for a in active if a.type == "injury" and a.source == "seed"
                         and set(a.evidence) & set(c["evidence"])), None)
            if seed is not None:
                k = alert_key(seed)
        fired.add(k)
        existing = by_key.get(k)
        sent = c["level"] in ("medium", "high")
        if existing is None:
            alert = AppAlert(
                id=new_id("alr"), astronaut_id=astronaut.id, type=c["type"], level=c["level"], text=c["text"],
                evidence=c["evidence"], timestamp=now, mission_day=day, sent_to_ground=sent, status="active",
                source="rules",
            )
            session.add(alert)
            changed.append(alert)
        elif existing.source == "rules" or LEVEL_ORDER[c["level"]] > LEVEL_ORDER[existing.level]:
            escalated = LEVEL_ORDER[c["level"]] > LEVEL_ORDER[existing.level]
            existing.level, existing.text, existing.evidence = c["level"], c["text"], c["evidence"]
            existing.sent_to_ground = existing.sent_to_ground or sent
            existing.source = "rules"
            if escalated:
                existing.timestamp, existing.mission_day = now, day
                changed.append(existing)
    for k, a in by_key.items():
        if a.source == "rules" and k not in fired:
            a.status = "resolved"
    session.flush()

    for a in changed:
        if a.level == "high" or (a.type == "injury" and any(
                l.id in a.evidence and l.data.get("severity") == "severe" for l in logs)):
            urgent = a.type == "injury" and a.level == "high"
            open_request(
                session, astronaut_id=astronaut.id, mission_id=astronaut.mission_id, mission_day=day,
                reason="urgent_symptom" if urgent else "pattern", detail=a.type,
                topic="medical" if a.type != "low_mood" else "psychological",
                priority="urgent" if urgent else "routine", message=f"CosmoMedic alert: {a.text}", send=send,
            )
    return changed


# --- Orbi proactive notes (at most one every 7 mission days, opt-out) ---------------------------


def proactive_enabled(session: Session, astronaut_id: str) -> bool:
    pref = session.get(AstronautPreference, astronaut_id)
    return pref is None or pref.proactive_messages


def maybe_note(session: Session, astronaut: AppAstronaut, day: int, alerts: list[AppAlert]) -> ProactiveMessage | None:
    if not alerts or not proactive_enabled(session, astronaut.id):
        return None
    last = session.scalar(select(func.max(ProactiveMessage.mission_day)).where(
        ProactiveMessage.astronaut_id == astronaut.id))
    if last is not None and day - last < PROACTIVE_INTERVAL_DAYS:
        return None
    relevant = sorted((a for a in alerts if a.level in ("medium", "high")),
                      key=lambda a: (-LEVEL_ORDER[a.level], TYPE_PRIORITY.index(a.type) if a.type in TYPE_PRIORITY else 99))
    if not relevant:
        return None
    main = relevant[0]
    first_name = astronaut.name.split()[0]
    body = [f"{first_name}, {main.text[0].lower()}{main.text[1:]}", ALERT_SUGGESTIONS.get(main.type, "")]
    if len(relevant) > 1:
        body.append("Also noticed: " + " ".join(a.text for a in relevant[1:3]))
    body.append(OPT_OUT_NOTE)
    note = ProactiveMessage(
        astronaut_id=astronaut.id, mission_id=astronaut.mission_id, mission_day=day,
        patterns=[a.type for a in relevant], title=f"Orbi noticed: {main.type.replace('_', ' ')}",
        body="\n\n".join(x for x in body if x), actions=["open_log", "message_ground", "turn_off_notes"],
    )
    session.add(note)
    session.flush()
    return note


# --- ground messages with communication delay --------------------------------------------------


def message_status(msg: AppGroundMessage, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if msg.read_at is not None:
        return "read"
    return "delivered" if msg.estimated_arrival_at <= now else "in_transit"


def send_message(session: Session, astronaut_id: str, sender: str, text: str, urgent: bool,
                 attachments: list[str], delay_sec: int, author: str | None) -> AppGroundMessage:
    sent = utcnow()
    msg = AppGroundMessage(
        id=new_id("gm"), astronaut_id=astronaut_id, sender=sender, author=author, text=text, urgent=urgent,
        sent_at=sent, estimated_arrival_at=sent + timedelta(seconds=delay_sec), attachments=attachments,
        source="app",
    )
    session.add(msg)
    session.flush()
    return msg
