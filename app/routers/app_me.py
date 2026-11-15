"""CosmoMedic app: endpoints of the logged-in astronaut (Crew ID + PIN)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.cosmo import logic, views
from app.cosmo.models import AppAlert, AppAstronaut, AppCalendarEvent, AppGroundMessage, AppJournalEntry, AppLog, AppShopItem
from app.cosmo.schemas import (
    AlertOut,
    DailySummaryOut,
    GroundMessageIn,
    GroundMessageOut,
    LogIn,
    LogOut,
    LogSaved,
)
from app.cosmo.service import evaluate, maybe_note, new_id, send_message, sync_alerts
from app.db import get_db
from app.models import AstronautPreference, ProactiveMessage, SupportRequest, User, utcnow
from app.schemas import APIModel

router = APIRouter(prefix="/api/v1/app/me", tags=["app: astronaut"])


def current_astronaut(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AppAstronaut:
    astronaut = db.get(AppAstronaut, user.astronaut_id) if user.role == "astronaut" and user.astronaut_id else None
    if astronaut is None:
        raise HTTPException(403, "Only CosmoMedic crew accounts (Crew ID + PIN) have these endpoints")
    return astronaut


def mission_day(db: Session):
    m = logic.mission(db)
    if m is None:
        raise HTTPException(503, "CosmoMedic data not loaded")
    return m, logic.today(db, m)


# --- profile and onboarding -------------------------------------------------------------


class OnboardingIn(APIModel):
    avatar_name: str | None = Field(None, min_length=1, max_length=30)
    daily_reminders: bool | None = None


class DevicesIn(APIModel):
    wrist_band_paired: bool | None = None
    bp_cuff: bool | None = None


@router.get("/profile", summary="Astronaut, pre-flight baseline (read-only), devices, avatar and mission")
def profile(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, day = mission_day(db)
    return {"id": astronaut.id, "name": astronaut.name, "crewId": astronaut.crew_id, "role": astronaut.role,
            "profile": astronaut.profile, "baseline": astronaut.baseline, "devices": astronaut.devices,
            "avatar": astronaut.avatar, "onboardingDone": astronaut.onboarding_done,
            "dailyReminders": astronaut.daily_reminders, "mission": views.mission_out(m, day),
            "baselineNote": "Filled by the medical team before flight; read-only in the app.",
            "provenance": "SIMULATED_TELEMETRY"}


@router.post("/onboarding", summary="Confirm onboarding (name the avatar, reminders)")
def complete_onboarding(body: OnboardingIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    if body.avatar_name:
        astronaut.avatar = {**astronaut.avatar, "name": body.avatar_name.strip()}
    if body.daily_reminders is not None:
        astronaut.daily_reminders = body.daily_reminders
    astronaut.onboarding_done = True
    db.commit()
    return profile(astronaut, db)


@router.patch("/devices", summary="Pair or unpair devices")
def update_devices(body: DevicesIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    devices = dict(astronaut.devices)
    if body.wrist_band_paired is not None:
        devices["wristBand"] = {**devices.get("wristBand", {}), "paired": body.wrist_band_paired}
    if body.bp_cuff is not None:
        devices["bpCuff"] = body.bp_cuff
    astronaut.devices = devices
    db.commit()
    return devices


# --- home, status, summaries -------------------------------------------------------------


@router.get("/home", summary="Home: logs of the day (x/7), coins, score, Orbi note, unread messages")
def home(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, day = mission_day(db)
    return views.home(db, astronaut, m, day)


@router.get("/status", summary="Crew status: score, 7-day scrubber, body areas and callouts")
def status(day: int | None = Query(None, description="Mission day (default: today)"),
           astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, today = mission_day(db)
    day = today if day is None else max(1, min(day, today))
    return views.status(db, astronaut, m, day)


@router.get("/summaries", response_model=list[DailySummaryOut], summary="Daily summaries (computed from the logs)")
def summaries(from_day: int | None = Query(None, alias="fromDay"), to_day: int | None = Query(None, alias="toDay"),
              astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, today = mission_day(db)
    to_day = min(to_day or today, today)
    from_day = max(1, from_day or 1)
    logs = views.astronaut_logs(db, astronaut.id)
    return [views.summary_out(evaluate(db, astronaut, d, m, logs)) for d in range(from_day, to_day + 1)]


# --- logs ---------------------------------------------------------------------------------


def _store_data(body, astronaut: AppAstronaut) -> dict:
    data = body.data.model_dump(mode="json", by_alias=True, exclude_none=True)  # JSON column: datetimes as ISO text
    if body.type == "measurement" and body.data.metric != "blood_pressure":
        delta = logic.baseline_delta(astronaut, body.data.metric, body.data.values["value"])
        if delta is not None:
            data["deltaVsBaselinePct"] = delta
    if body.type == "measurement" and body.data.metric == "blood_pressure" and data["values"].get("pulse") is None:
        data["values"].pop("pulse", None)
    return data


def _check_environment(body, m) -> None:
    if body.environment_id and body.environment_id not in {e["id"] for e in m.environments}:
        raise HTTPException(422, f"Unknown environmentId {body.environment_id!r}")


def _after_save(db: Session, astronaut: AppAstronaut, day: int, log: AppLog) -> tuple[list[AppAlert], int | None]:
    changed = sync_alerts(db, astronaut, day)
    maybe_note(db, astronaut, day, changed)
    request_id = None
    if log.type == "injury" and log.data.get("severity") == "severe":
        request = db.scalar(select(SupportRequest).where(
            SupportRequest.astronaut_id == astronaut.id, SupportRequest.status != "resolved",
            SupportRequest.detail == "injury").order_by(SupportRequest.id.desc()))
        request_id = request.id if request else None
    return changed, request_id


@router.post("/logs", response_model=LogSaved, status_code=201, summary="Save a log (+10 coins)")
def create_log(body: LogIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, day = mission_day(db)
    _check_environment(body, m)
    coins = 0 if body.type == "meditation" else logic.COINS_PER_LOG
    log = AppLog(
        id=new_id("log"), astronaut_id=astronaut.id, type=body.type,
        timestamp=body.timestamp or datetime.now(timezone.utc), mission_day=day,
        environment_id=body.environment_id, note=body.note, coins_earned=coins,
        data=_store_data(body, astronaut), source="app",
    )
    db.add(log)
    astronaut.avatar = {**astronaut.avatar, "coins": astronaut.avatar.get("coins", 0) + coins}
    db.flush()
    changed, request_id = _after_save(db, astronaut, day, log)
    db.commit()
    message = f"Log saved to your mission record · +{coins} mission coins"
    if request_id:
        message += ". Severe injury: flagged to the ground medical team."
    return LogSaved(log=views.log_out(log), coins=astronaut.avatar["coins"], coins_earned=coins,
                    logged_today=True, alerts=[views.alert_out(a) for a in changed],
                    support_request_id=request_id, message=message)


@router.put("/logs/{log_id}", response_model=LogSaved, summary="Edit a log (same type, no extra coins)")
def update_log(log_id: str, body: LogIn, astronaut: AppAstronaut = Depends(current_astronaut),
               db: Session = Depends(get_db)):
    m, day = mission_day(db)
    log = db.get(AppLog, log_id)
    if log is None or log.astronaut_id != astronaut.id:
        raise HTTPException(404, "Log not found")
    if log.type != body.type:
        raise HTTPException(422, f"This log is of type {log.type!r}")
    _check_environment(body, m)
    log.data, log.note, log.environment_id = _store_data(body, astronaut), body.note, body.environment_id
    log.updated_at = utcnow()
    db.flush()
    changed, request_id = _after_save(db, astronaut, day, log)
    db.commit()
    return LogSaved(log=views.log_out(log), coins=astronaut.avatar.get("coins", 0), coins_earned=0,
                    logged_today=log.mission_day == day, alerts=[views.alert_out(a) for a in changed],
                    support_request_id=request_id, message="Log updated")


@router.get("/logs", response_model=list[LogOut], summary="My logs, newest first")
def list_logs(type: str | None = Query(None, description="mood | sleep | radiation | measurement | injury | meal | exercise | meditation"),
              from_day: int | None = Query(None, alias="fromDay"), to_day: int | None = Query(None, alias="toDay"),
              limit: int = Query(200, ge=1, le=1000),
              astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    stmt = select(AppLog).where(AppLog.astronaut_id == astronaut.id)
    if type:
        stmt = stmt.where(AppLog.type == type)
    if from_day is not None:
        stmt = stmt.where(AppLog.mission_day >= from_day)
    if to_day is not None:
        stmt = stmt.where(AppLog.mission_day <= to_day)
    stmt = stmt.order_by(AppLog.mission_day.desc(), AppLog.timestamp.desc()).limit(limit)
    return [views.log_out(l) for l in db.scalars(stmt)]


@router.get("/logs/{module}/overview", summary="Overview screen of one log module")
def log_overview(module: Literal["mood", "sleep", "radiation", "body", "meals", "exercise", "relax"],
                 day: int | None = Query(None), astronaut: AppAstronaut = Depends(current_astronaut),
                 db: Session = Depends(get_db)):
    m, today = mission_day(db)
    day = today if day is None else max(1, min(day, today))
    return views.overview(db, astronaut, m, day, module)


# --- alerts and Orbi notes ---------------------------------------------------------------


class PreferencesIn(APIModel):
    proactive_messages: bool | None = Field(None, description="Orbi notes (max one every 7 days)")
    daily_reminders: bool | None = None


def _preferences(db: Session, astronaut: AppAstronaut) -> dict:
    pref = db.get(AstronautPreference, astronaut.id)
    return {"proactiveMessages": pref is None or pref.proactive_messages,
            "dailyReminders": astronaut.daily_reminders, "minDaysBetweenNotes": 7}


@router.get("/preferences", summary="Notification preferences")
def get_preferences(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    return _preferences(db, astronaut)


@router.put("/preferences", summary="Turn Orbi notes or daily reminders on or off")
def put_preferences(body: PreferencesIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    if body.proactive_messages is not None:
        pref = db.get(AstronautPreference, astronaut.id) or AstronautPreference(astronaut_id=astronaut.id)
        pref.proactive_messages = body.proactive_messages
        pref.updated_at = utcnow()
        db.add(pref)
    if body.daily_reminders is not None:
        astronaut.daily_reminders = body.daily_reminders
    db.commit()
    return _preferences(db, astronaut)


@router.get("/alerts", response_model=list[AlertOut], summary="My alerts (rules over the last 7 days)")
def my_alerts(status: Literal["active", "resolved", "all"] = "active",
              astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    stmt = select(AppAlert).where(AppAlert.astronaut_id == astronaut.id)
    if status != "all":
        stmt = stmt.where(AppAlert.status == status)
    return [views.alert_out(a) for a in db.scalars(stmt.order_by(AppAlert.timestamp.desc()))]


@router.get("/orbi-notes", summary="Proactive notes from Orbi (max one every 7 mission days)")
def orbi_notes(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    notes = db.scalars(select(ProactiveMessage).where(ProactiveMessage.astronaut_id == astronaut.id)
                       .order_by(ProactiveMessage.mission_day.desc(), ProactiveMessage.id.desc()))
    return [{"id": n.id, "missionDay": n.mission_day, "title": n.title, "body": n.body, "types": n.patterns,
             "actions": n.actions, "status": n.status, "provenance": "RULE_DERIVED"} for n in notes]


@router.post("/orbi-notes/{note_id}/{action}", summary="Mark an Orbi note read or dismissed")
def update_note(note_id: int, action: Literal["read", "dismiss"],
                astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    note = db.get(ProactiveMessage, note_id)
    if note is None or note.astronaut_id != astronaut.id:
        raise HTTPException(404, "Note not found")
    note.status = "read" if action == "read" else "dismissed"
    db.commit()
    return {"id": note.id, "status": note.status}


# --- messages with the ground team (communication delay) ----------------------------------


def _visible_thread(db: Session, astronaut_id: str) -> list[AppGroundMessage]:
    now = datetime.now(timezone.utc)
    msgs = db.scalars(select(AppGroundMessage).where(AppGroundMessage.astronaut_id == astronaut_id)
                      .order_by(AppGroundMessage.sent_at))
    # Ground messages appear only once they arrive; the astronaut's own show "in transit" until then.
    return [g for g in msgs if g.sender == "astronaut" or g.estimated_arrival_at <= now]


@router.get("/ground-messages", summary="Thread with Mission Control")
def ground_thread(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, _ = mission_day(db)
    return {"commDelaySec": m.comm_delay_sec,
            "delayText": f"Earth is about {round(m.comm_delay_sec / 60)} minutes away. "
                         f"Replies can take {round(2 * m.comm_delay_sec / 60)} minutes or more.",
            "messages": [views.message_out(g).model_dump(mode="json", by_alias=True) for g in _visible_thread(db, astronaut.id)]}


@router.post("/ground-messages", response_model=GroundMessageOut, status_code=201, summary="Send a message to the ground")
def send_ground(body: GroundMessageIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, _ = mission_day(db)
    for log_id in body.attachments:
        log = db.get(AppLog, log_id)
        if log is None or log.astronaut_id != astronaut.id:
            raise HTTPException(422, f"Attachment {log_id!r} is not one of your logs")
    msg = send_message(db, astronaut.id, "astronaut", body.text.strip(), body.urgent, body.attachments,
                       m.comm_delay_sec, astronaut.crew_id)
    db.commit()
    return views.message_out(msg)


@router.post("/ground-messages/{message_id}/read", response_model=GroundMessageOut, summary="Mark a ground message read")
def read_ground(message_id: str, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    msg = db.get(AppGroundMessage, message_id)
    if msg is None or msg.astronaut_id != astronaut.id or msg.sender != "ground":
        raise HTTPException(404, "Message not found")
    if msg.read_at is None and views.message_status(msg) == "delivered":
        msg.read_at = utcnow()
        db.commit()
    return views.message_out(msg)


# --- calendar, journal ---------------------------------------------------------------------


@router.get("/calendar", summary="Calendar events (screen is 'coming soon')")
def calendar(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    events = db.scalars(select(AppCalendarEvent).where(AppCalendarEvent.astronaut_id == astronaut.id)
                        .order_by(AppCalendarEvent.date))
    return [{"id": e.id, "astronautId": e.astronaut_id, "date": e.date, "title": e.title, "kind": e.kind,
             "linkedScreen": e.linked_screen} for e in events]


@router.get("/journal", summary="Journal entries shared with Orbi (shown in Mood history)")
def journal(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    entries = db.scalars(select(AppJournalEntry).where(AppJournalEntry.astronaut_id == astronaut.id)
                         .order_by(AppJournalEntry.timestamp.desc()))
    return [{"id": j.id, "astronautId": j.astronaut_id, "timestamp": views.iso(j.timestamp), "summary": j.summary,
             "moodLevel": j.mood_level, "sourceMessageIds": j.source_message_ids,
             "provenance": "SIMULATED_TELEMETRY"} for j in entries]


# --- avatar and shop -----------------------------------------------------------------------


class AvatarIn(APIModel):
    name: str = Field(min_length=1, max_length=30)


class PurchaseIn(APIModel):
    item_id: str


class EquipIn(APIModel):
    item_id: str | None = Field(None, description="null removes the pet")
    category: Literal["helmet", "suit", "pet", "window", "wall", "floor"] | None = None


@router.get("/avatar", summary="Avatar with its animation state and the shop marked owned/equipped")
def avatar(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, day = mission_day(db)
    return views.avatar_view(db, astronaut, m, day)


@router.put("/avatar", summary="Rename the avatar")
def rename_avatar(body: AvatarIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    astronaut.avatar = {**astronaut.avatar, "name": body.name.strip()}
    db.commit()
    return avatar(astronaut, db)


@router.post("/avatar/purchase", summary="Buy a shop item with coins")
def purchase(body: PurchaseIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    item = db.get(AppShopItem, body.item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    owned = list(astronaut.avatar.get("owned", []))
    if item.id in owned:
        raise HTTPException(409, "Already owned")
    coins = astronaut.avatar.get("coins", 0)
    if coins < item.price:
        raise HTTPException(409, f"Not enough coins: {coins} of {item.price}")
    astronaut.avatar = {**astronaut.avatar, "coins": coins - item.price, "owned": sorted(owned + [item.id])}
    db.commit()
    return avatar(astronaut, db)


@router.post("/avatar/equip", summary="Equip an owned item (or remove the pet)")
def equip(body: EquipIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    equipped = {**astronaut.avatar.get("equipped", {})}
    room = {**equipped.get("room", {})}
    if body.item_id is None:
        if body.category != "pet":
            raise HTTPException(422, "Only the pet can be removed")
        equipped["pet"] = None
    else:
        item = db.get(AppShopItem, body.item_id)
        if item is None:
            raise HTTPException(404, "Item not found")
        if item.id not in astronaut.avatar.get("owned", []) and item.price > 0:
            raise HTTPException(409, "Buy the item first")
        if item.category in ("window", "wall", "floor"):
            room[item.category] = item.id
        else:
            equipped[item.category] = item.id
    equipped["room"] = room
    astronaut.avatar = {**astronaut.avatar, "equipped": equipped}
    db.commit()
    return avatar(astronaut, db)
