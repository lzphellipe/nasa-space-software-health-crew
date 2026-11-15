"""CosmoMedic app: the whole local database of the app in one answer, in the frontend's own format.

The frontend keeps an in-memory database with the shape of `src/mocks/mock-db.json` and its
screens read it synchronously. `GET /api/v1/app/snapshot` returns that same shape with live
data, so the app replaces the mock at login and after each sync, with no change to the screens.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.cosmo import logic, offline, telemetry, views
from app.cosmo.models import (
    AppAlert,
    AppAstronaut,
    AppCalendarEvent,
    AppChatMessage,
    AppEnvironmentReading,
    AppGroundMessage,
    AppJournalEntry,
    AppLog,
    AppShopItem,
)
from app.cosmo.service import evaluate
from app.db import get_db
from app.models import User

router = APIRouter(prefix="/api/v1/app", tags=["app: offline"])

LEVEL = {"low": 1, "medium": 2, "high": 3}


def _full(a: AppAstronaut, state: str) -> dict:
    return {"id": a.id, "name": a.name, "crewId": a.crew_id, "role": "astronaut", "profile": a.profile,
            "baseline": a.baseline, "devices": a.devices, "avatar": {**a.avatar, "state": state},
            "onboardingDone": a.onboarding_done}


def _public(a: AppAstronaut) -> dict:
    """A crewmate as the leaderboard shows them: name and avatar, no health or profile data."""
    avatar = a.avatar
    return {"id": a.id, "name": a.name, "crewId": a.crew_id, "role": "astronaut",
            "profile": {"age": 0, "heightCm": 0, "weightKg": 0, "foodPreferences": []},
            "baseline": {"boneDensity": 0, "muscleMassKg": 0, "strengthScore": 0, "aerobicScore": 0, "bodyFatPct": 0,
                         "restingHr": 0, "restingBp": "", "usualSleepH": 0, "usualBedtime": ""},
            "devices": {"wristBand": {"model": "", "paired": False}, "dosimeterBadgeId": "", "bpCuff": False},
            "avatar": {"name": avatar.get("name"), "level": avatar.get("level"), "coins": avatar.get("coins", 0),
                       "owned": [], "equipped": avatar.get("equipped"), "state": "idle"},
            "onboardingDone": True}


@router.get("/snapshot", summary="The app's local database in the frontend format (mock-db.json shape), with live data")
def snapshot(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Astronaut: own data, plus crewmates' names and avatars for the leaderboard.
    Ground team: the data of the whole crew (chat messages with Orbi are never included for the ground).
    """
    m = logic.mission(db)
    if m is None:
        raise HTTPException(503, "CosmoMedic data not loaded")
    today = logic.today(db, m)
    now = datetime.now(timezone.utc)
    me = db.get(AppAstronaut, user.astronaut_id) if user.role == "astronaut" and user.astronaut_id else None
    ground = me is None
    if ground and user.role not in ("medical", "admin"):
        raise HTTPException(403, "This account has no CosmoMedic profile")

    everyone = list(db.scalars(select(AppAstronaut).order_by(AppAstronaut.id)))
    crew = [a for a in everyone if a.role != "test" or (me is not None and a.id == me.id)]
    scope = [a for a in crew if ground or a.id == me.id]
    scope_ids = {a.id for a in scope}
    dump = lambda model: model.model_dump(mode="json", by_alias=True)

    all_logs = list(db.scalars(select(AppLog)))
    counts = Counter(l.astronaut_id for l in all_logs)
    by_astronaut: dict[str, list[AppLog]] = {a.id: [] for a in scope}
    for l in all_logs:
        if l.astronaut_id in scope_ids:
            by_astronaut[l.astronaut_id].append(l)

    astronauts, summaries = [], []
    for a in crew:
        if a.id not in scope_ids:
            astronauts.append(_public(a))
            continue
        logs = by_astronaut[a.id]
        evaluations = [evaluate(db, a, d, m, logs) for d in range(1, today + 1)]
        astronauts.append(_full(a, logic.avatar_state(evaluations[-1])))
        for ev in evaluations:
            s = dump(views.summary_out(ev))
            summaries.append(s)

    logs_out = [dump(views.log_out(l)) for a in scope
                for l in sorted(by_astronaut[a.id], key=lambda l: (l.timestamp, l.id))]
    alerts = sorted((x for x in db.scalars(select(AppAlert).where(AppAlert.status == "active"))
                     if x.astronaut_id in scope_ids), key=lambda x: (-LEVEL.get(x.level, 0), -x.timestamp.timestamp()))
    messages = [] if ground else [
        {"id": c.id, "astronautId": c.astronaut_id, "author": c.author, "text": c.text,
         "timestamp": views.iso(c.timestamp), "mode": c.mode}
        for c in db.scalars(select(AppChatMessage).where(AppChatMessage.astronaut_id == me.id)
                            .order_by(AppChatMessage.timestamp, AppChatMessage.id))]
    journal = [{"id": j.id, "astronautId": j.astronaut_id, "timestamp": views.iso(j.timestamp), "summary": j.summary,
                "moodLevel": j.mood_level, "sourceMessageIds": j.source_message_ids}
               for j in db.scalars(select(AppJournalEntry).order_by(AppJournalEntry.timestamp))
               if j.astronaut_id in scope_ids]
    thread = [g for g in db.scalars(select(AppGroundMessage).order_by(AppGroundMessage.sent_at))
              if g.astronaut_id in scope_ids
              and (ground or g.sender == "astronaut" or telemetry._as_utc(g.estimated_arrival_at) <= now)]
    return {
        "currentAstronautId": me.id if me else (crew[0].id if crew else None),
        "role": "ground" if ground else "astronaut",
        "serverTime": offline.iso(now),
        "cursor": offline.iso(now),
        "mission": {"id": m.id, "name": m.name, "destination": m.destination, "startDate": m.start_date,
                    "lengthDays": m.length_days, "today": today, "commDelaySec": m.comm_delay_sec,
                    "goals": m.goals, "environments": m.environments},
        "astronauts": astronauts,
        "logs": logs_out,
        "daily_summaries": summaries,
        "environment_readings": [
            {"id": r.id, "environmentId": r.environment_id, "timestamp": views.iso(r.timestamp),
             "temperatureC": r.temperature_c, "pressureKpa": r.pressure_kpa, "lightLux": r.light_lux,
             "noiseDb": r.noise_db, "radiationUsvH": r.radiation_usv_h, "crewPresent": r.crew_present}
            for r in db.scalars(select(AppEnvironmentReading).order_by(AppEnvironmentReading.timestamp))],
        "ai_chat": {"messages": messages, "journal": journal, "alerts": [dump(views.alert_out(x)) for x in alerts]},
        "ground_messages": [dump(views.message_out(g)) for g in thread],
        "shop": [{"id": i.id, "category": i.category, "name": i.name, "price": i.price, "asset": i.asset}
                 for i in db.scalars(select(AppShopItem).order_by(AppShopItem.category, AppShopItem.price))],
        "calendar": [{"id": e.id, "astronautId": e.astronaut_id, "date": views.iso(e.date), "title": e.title,
                      "kind": e.kind, "linkedScreen": e.linked_screen}
                     for e in db.scalars(select(AppCalendarEvent).order_by(AppCalendarEvent.date))
                     if e.astronaut_id in scope_ids],
        "leaderboard": [{"astronautId": a.id, "logs": counts.get(a.id, 0), "coins": a.avatar.get("coins", 0)} for a in crew],
        "sync": {"link": None if ground else offline.link(db, me, m)},
        "provenance": "RULE_DERIVED",
    }
