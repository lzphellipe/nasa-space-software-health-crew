"""CosmoMedic ground dashboard (web, section 6 of the design): care team only."""

from __future__ import annotations

import statistics
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_care_team
from app.cosmo import logic, views
from app.cosmo.models import AppAlert, AppAstronaut, AppEnvironmentReading, AppGroundMessage, AppLog
from app.cosmo.schemas import AlertOut, DailySummaryOut, GroundMessageOut, GroundReplyIn, LogOut
from app.cosmo.service import astronaut_logs, evaluate, message_status, send_message
from app.db import get_db
from app.models import User, utcnow

router = APIRouter(prefix="/api/v1/app/ground", tags=["app: ground team"], dependencies=[Depends(require_care_team)])

LEVEL = {"low": 1, "medium": 2, "high": 3}


def _mission(db: Session):
    m = logic.mission(db)
    if m is None:
        raise HTTPException(503, "CosmoMedic data not loaded")
    return m, logic.today(db, m)


def _astronaut(db: Session, astronaut_id: str) -> AppAstronaut:
    a = db.get(AppAstronaut, astronaut_id)
    if a is None:
        raise HTTPException(404, f"Unknown astronaut {astronaut_id!r}")
    return a


@router.get("/crew", summary="Crew table: score, mood, sleep, radiation*, latest alert, unread messages")
def crew(db: Session = Depends(get_db)):
    m, day = _mission(db)
    rows = []
    for a in db.scalars(select(AppAstronaut).order_by(AppAstronaut.id)):
        logs = astronaut_logs(db, a.id)
        ev = evaluate(db, a, day, m, logs)
        moods = sorted((l for l in logs if l.type == "mood" and l.mission_day <= day), key=lambda l: (l.mission_day, l.timestamp))
        sleeps = [l.data["durationMin"] for l in logs if l.type == "sleep" and day - 6 <= l.mission_day <= day]
        personal = [l for l in logs if l.type == "radiation" and l.data.get("source") == "personal" and l.mission_day <= day]
        alerts = sorted(db.scalars(select(AppAlert).where(AppAlert.astronaut_id == a.id, AppAlert.status == "active")),
                        key=lambda x: (-LEVEL[x.level], -x.timestamp.timestamp()))
        unread = [g for g in db.scalars(select(AppGroundMessage).where(
            AppGroundMessage.astronaut_id == a.id, AppGroundMessage.sender == "astronaut",
            AppGroundMessage.read_at.is_(None))) if message_status(g) == "delivered"]
        rows.append({
            "astronaut": {"id": a.id, "name": a.name, "crewId": a.crew_id},
            "overallScore": ev.score, "statusLabel": ev.label, "logsToday": len(ev.logs_done),
            "areas": ev.areas,
            "mood": {"level": moods[-1].data["level"], "label": moods[-1].data["label"],
                     "missionDay": moods[-1].mission_day} if moods else None,
            "sleepAvg7dHours": round(statistics.fmean(sleeps) / 60, 2) if sleeps else None,
            "radiationTodayUsv": sum(l.data["doseUsv"] for l in personal if l.mission_day == day),
            "radiationMissionUsv": round(sum(l.data["doseUsv"] for l in personal), 1),
            "radiationIllustrative": True,
            "latestAlert": views.alert_out(alerts[0]).model_dump(mode="json", by_alias=True) if alerts else None,
            "activeAlerts": len(alerts),
            "unreadMessages": len(unread),
        })
    return {"missionDay": day, "mission": views.mission_out(m, day), "crew": rows, "provenance": "RULE_DERIVED"}


@router.get("/alerts", response_model=list[AlertOut], summary="Orbi alerts of the crew with evidence")
def alerts(astronaut_id: str | None = Query(None, alias="astronautId"),
           status: str = Query("active", description="active | resolved | all"),
           min_level: str = Query("low", alias="minLevel", description="low | medium | high"),
           db: Session = Depends(get_db)):
    stmt = select(AppAlert)
    if astronaut_id:
        stmt = stmt.where(AppAlert.astronaut_id == astronaut_id)
    if status != "all":
        stmt = stmt.where(AppAlert.status == status)
    items = [a for a in db.scalars(stmt) if LEVEL[a.level] >= LEVEL.get(min_level, 1)]
    items.sort(key=lambda a: (-LEVEL[a.level], -a.timestamp.timestamp()))
    return [views.alert_out(a) for a in items]


@router.get("/sleep", summary="Sleep over the last 7 days per crew member")
def sleep(db: Session = Depends(get_db)):
    m, day = _mission(db)
    days = list(range(max(1, day - 6), day + 1))
    out = []
    for a in db.scalars(select(AppAstronaut).order_by(AppAstronaut.id)):
        by_day = defaultdict(int)
        for l in astronaut_logs(db, a.id):
            if l.type == "sleep" and l.mission_day in days:
                by_day[l.mission_day] += l.data["durationMin"]
        values = [by_day[d] for d in days if by_day.get(d)]
        out.append({"astronautId": a.id, "name": a.name,
                    "averageHours": round(statistics.fmean(values) / 60, 2) if values else None,
                    "days": [{"missionDay": d, "durationMin": by_day.get(d)} for d in days]})
    return {"goalHours": m.goals.get("sleepHours", 7), "crew": out}


@router.get("/environment", summary="Habitat readings per module (temperature, pressure, light, noise, radiation*)")
def environment(environment_id: str | None = Query(None, alias="environmentId"), db: Session = Depends(get_db)):
    m, _ = _mission(db)
    stmt = select(AppEnvironmentReading).order_by(AppEnvironmentReading.timestamp)
    if environment_id:
        stmt = stmt.where(AppEnvironmentReading.environment_id == environment_id)
    readings = [{"id": r.id, "environmentId": r.environment_id, "timestamp": views.iso(r.timestamp),
                 "temperatureC": r.temperature_c, "pressureKpa": r.pressure_kpa, "lightLux": r.light_lux,
                 "noiseDb": r.noise_db, "radiationUsvH": r.radiation_usv_h, "crewPresent": r.crew_present}
                for r in db.scalars(stmt)]
    latest = {}
    for r in readings:
        latest[r["environmentId"]] = r
    return {"environments": m.environments, "latest": list(latest.values()), "readings": readings,
            "radiationIllustrative": True, "provenance": "SIMULATED_TELEMETRY"}


@router.get("/astronauts/{astronaut_id}/status", summary="Status screen of one astronaut")
def astronaut_status(astronaut_id: str, day: int | None = None, db: Session = Depends(get_db)):
    m, today = _mission(db)
    return views.status(db, _astronaut(db, astronaut_id), m, today if day is None else max(1, min(day, today)))


@router.get("/astronauts/{astronaut_id}/summaries", response_model=list[DailySummaryOut], summary="Daily summaries of one astronaut")
def astronaut_summaries(astronaut_id: str, db: Session = Depends(get_db)):
    m, today = _mission(db)
    a = _astronaut(db, astronaut_id)
    logs = astronaut_logs(db, a.id)
    return [views.summary_out(evaluate(db, a, d, m, logs)) for d in range(1, today + 1)]


@router.get("/astronauts/{astronaut_id}/logs", response_model=list[LogOut], summary="Logs of one astronaut")
def astronaut_logs_endpoint(astronaut_id: str, type: str | None = None,
                            from_day: int | None = Query(None, alias="fromDay"), to_day: int | None = Query(None, alias="toDay"),
                            db: Session = Depends(get_db)):
    a = _astronaut(db, astronaut_id)
    stmt = select(AppLog).where(AppLog.astronaut_id == a.id)
    if type:
        stmt = stmt.where(AppLog.type == type)
    if from_day is not None:
        stmt = stmt.where(AppLog.mission_day >= from_day)
    if to_day is not None:
        stmt = stmt.where(AppLog.mission_day <= to_day)
    return [views.log_out(l) for l in db.scalars(stmt.order_by(AppLog.mission_day.desc(), AppLog.timestamp.desc()))]


@router.get("/messages", response_model=list[GroundMessageOut], summary="Message threads (urgent and unread first)")
def messages(astronaut_id: str | None = Query(None, alias="astronautId"), db: Session = Depends(get_db)):
    stmt = select(AppGroundMessage)
    if astronaut_id:
        stmt = stmt.where(AppGroundMessage.astronaut_id == astronaut_id)
    items = list(db.scalars(stmt))
    items.sort(key=lambda g: (g.sent_at,))
    return [views.message_out(g) for g in items]


@router.post("/messages", response_model=GroundMessageOut, status_code=201, summary="Send a message to an astronaut")
def reply(body: GroundReplyIn, user: User = Depends(require_care_team), db: Session = Depends(get_db)):
    m, _ = _mission(db)
    _astronaut(db, body.astronaut_id)
    msg = send_message(db, body.astronaut_id, "ground", body.text.strip(), body.urgent, body.attachments,
                       m.comm_delay_sec, user.username)
    db.commit()
    return views.message_out(msg)


@router.post("/messages/{message_id}/read", response_model=GroundMessageOut, summary="Mark an astronaut message read")
def read(message_id: str, db: Session = Depends(get_db)):
    msg = db.get(AppGroundMessage, message_id)
    if msg is None or msg.sender != "astronaut":
        raise HTTPException(404, "Message not found")
    if msg.read_at is None and message_status(msg) == "delivered":
        msg.read_at = utcnow()
        db.commit()
    return views.message_out(msg)
