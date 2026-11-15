"""The monitoring cycle: detect patterns, send proactive messages, route to the care team.

Runs once per simulated mission day. At startup the whole mission history is
replayed day by day (so the 7-day message limit holds across history); after
that a background task repeats the cycle for the current mission day every
DETECTION_INTERVAL_MINUTES.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import db
from app.config import Settings, get_settings
from app.models import Mission, PatternDetection
from app.services.patterns import WINDOW_DAYS, run_detection
from app.services.proactive import maybe_send
from app.services.support import PATTERN_TOPICS, TOPICS, open_request

log = logging.getLogger(__name__)


def current_mission(session: Session) -> Mission | None:
    return session.scalar(select(Mission).order_by(Mission.id).limit(1))


def current_day(session: Session, settings: Settings | None = None) -> int | None:
    settings = settings or get_settings()
    mission = current_mission(session)
    if mission is None:
        return None
    if settings.current_mission_day is not None:
        return min(max(settings.current_mission_day, mission.first_day), mission.last_day)
    return mission.last_day


def route_to_care_team(session: Session, mission_id: str, day: int, detections: list[PatternDetection], send: bool) -> int:
    opened = 0
    for d in detections:
        if d.as_of_day != day:
            continue
        if d.pattern == "urgent_symptom":
            open_request(
                session, astronaut_id=d.astronaut_id, mission_id=mission_id, mission_day=day,
                reason="urgent_symptom", detail=d.detail, topic="medical", priority="urgent",
                message=d.summary, send=send,
            )
            opened += 1
        elif d.severity == "high" and d.pattern in PATTERN_TOPICS:
            topic = PATTERN_TOPICS[d.pattern]
            open_request(
                session, astronaut_id=d.astronaut_id, mission_id=mission_id, mission_day=day,
                reason="pattern", detail=d.pattern, topic=topic, priority="routine",
                message=f"Automatic review for {TOPICS[topic].lower()}: {d.summary}", send=send,
            )
            opened += 1
    return opened


def run_cycle(session: Session, days: list[int], send_notifications: bool = True) -> dict:
    settings = get_settings()
    mission = current_mission(session)
    if mission is None or not days:
        return {"days": 0, "detections": 0, "messages": 0}
    detections = run_detection(session, mission.id, days)
    by_day: dict[int, list[PatternDetection]] = {}
    for d in detections:
        by_day.setdefault(d.as_of_day, []).append(d)
    astronauts = sorted({d.astronaut_id for d in detections})
    messages = 0
    for day in sorted(days):
        route_to_care_team(session, mission.id, day, by_day.get(day, []), send_notifications)
        for aid in astronauts:
            if maybe_send(session, mission.id, aid, day, settings.proactive_min_interval_days):
                messages += 1
    session.commit()
    return {"days": len(days), "detections": len(detections), "messages": messages}


def backfill_if_empty(session: Session) -> dict | None:
    if session.scalar(select(func.count()).select_from(PatternDetection)):
        return None
    mission = current_mission(session)
    day = current_day(session)
    if mission is None or day is None:
        return None
    first = mission.first_day + WINDOW_DAYS - 1
    # History is replayed without calling the webhook.
    return run_cycle(session, list(range(first, day + 1)), send_notifications=False)


async def scheduler_loop(stop: asyncio.Event) -> None:
    interval = max(get_settings().detection_interval_minutes, 0.1) * 60
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
        try:
            with db.SessionLocal() as session:
                day = current_day(session)
                if day is not None:
                    result = run_cycle(session, [day])
                    log.info("Scheduled pattern detection for mission day %s: %s", day, result)
        except Exception:  # keep the loop alive
            log.exception("Scheduled pattern detection failed")
