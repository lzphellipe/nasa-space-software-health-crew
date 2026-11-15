"""CosmoMedic app: endpoints of the logged-in astronaut (Crew ID + PIN)."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.config import get_settings
from app.cosmo import logic, nutrition, offline, orbi, telemetry, views
from app.cosmo.models import (
    AppAlert,
    AppAstronaut,
    AppCalendarEvent,
    AppChatMessage,
    AppConfirmation,
    AppDeviceReading,
    AppGroundMessage,
    AppJournalEntry,
    AppLog,
    AppPlanMeal,
    AppShopItem,
)
from app.cosmo.schemas import (
    AlertOut,
    DailySummaryOut,
    GroundMessageIn,
    GroundMessageOut,
    LogIn,
    LogOut,
    LogSaved,
    AnswerIn,
    ConfirmIn,
    OrbiChatIn,
    SimulateIn,
    TelemetryIn,
)
from app.cosmo.service import evaluate, maybe_note, new_id, send_message, sync_alerts
from app.db import get_db
from app.models import AstronautPreference, ProactiveMessage, SupportRequest, User, utcnow
from app.provenance import DEVICE, SIMULATED
from app.schemas import APIModel
from app.services.support import open_request

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


def _meal_data(db: Session, body, astronaut: AppAstronaut, log_id: str | None) -> dict:
    """Compute what was eaten: from the planned meal and its adherence, from swaps, or as sent (free meal)."""
    d = body.data
    out: dict = {}
    scale = lambda total, pct: {k: round(v * pct / 100, 1) for k, v in total.items()}
    swapped, foods = None, []
    if d.swaps:
        try:
            swapped, foods = nutrition.nutrition_of(db, [(x.food_id, x.servings) for x in d.swaps])
        except KeyError as exc:
            raise HTTPException(422, f"Unknown foodId {exc.args[0]!r}; see GET /api/v1/app/foods") from None
        out["swaps"] = [{"foodId": x.food_id, "servings": x.servings, "name": f.name} for x, f in zip(d.swaps, foods)]

    if d.plan_meal_id:
        pm = db.get(AppPlanMeal, d.plan_meal_id)
        if pm is None or pm.astronaut_id != astronaut.id:
            raise HTTPException(422, f"planMealId {d.plan_meal_id!r} is not in your meal plan")
        other = next((l for l in views.astronaut_logs(db, astronaut.id)
                      if l.type == "meal" and l.data.get("planMealId") == pm.id and l.id != log_id), None)
        if other is not None:
            raise HTTPException(409, f"This planned meal is already logged ({other.id}); edit it with PUT /logs/{other.id}")
        planned = {n: getattr(pm, n) for n in nutrition.NUTRIENTS}
        if d.adherence == "swapped":
            pct = d.eaten_pct if d.eaten_pct is not None else 100
            total = scale(swapped, pct)
            name, items = " + ".join(f.name for f in foods[:2]), [f.name for f in foods]
        else:
            pct = {"followed": 100, "skipped": 0}.get(d.adherence, d.eaten_pct)
            total = scale(planned, pct)
            name, items = pm.name, pm.items
        out |= {"planMealId": pm.id, "slot": pm.slot, "adherence": d.adherence, "eatenPct": pct,
                "plannedKcal": pm.kcal, "name": name, "items": items}
    elif swapped is not None:
        pct = d.eaten_pct if d.eaten_pct is not None else 100
        total = scale(swapped, pct)
        out |= {"name": d.name or " + ".join(f.name for f in foods[:2]), "items": d.items or [f.name for f in foods],
                "eatenPct": pct}
    else:
        total = {"kcal": d.kcal, "protein_g": d.protein_g or 0, "carbs_g": d.carbs_g or 0, "fat_g": d.fat_g or 0}
        out |= {"name": d.name, "items": d.items}
    out |= {"kcal": total["kcal"], "proteinG": total["protein_g"], "carbsG": total["carbs_g"], "fatG": total["fat_g"]}
    if d.taste_rating is not None:
        out["tasteRating"] = d.taste_rating
    return out


def _store_data(db: Session, body, astronaut: AppAstronaut, log_id: str | None = None) -> dict:
    if body.type == "meal":
        return _meal_data(db, body, astronaut, log_id)
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


IDEMPOTENCY_KEY = Header(None, alias="Idempotency-Key", min_length=8, max_length=64,
                         description="Id made by the app for this request. Sending it again returns the first "
                                     "answer instead of saving twice (safe retry after a dropped connection).")


@router.post("/logs", response_model=LogSaved, status_code=201, summary="Save a log (+10 coins)")
def create_log(body: LogIn, idempotency_key: str | None = IDEMPOTENCY_KEY,
               astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    if idempotency_key is None:
        return save_new_log(db, astronaut, body)
    # The stored answer is already in the response format: send it as it is.
    return JSONResponse(offline.once(db, astronaut, idempotency_key, "log.create", body.timestamp,
                                     lambda: _saved_json(save_new_log(db, astronaut, body))), status_code=201)


def _saved_json(saved: LogSaved) -> tuple[str, dict]:
    return saved.log.id, saved.model_dump(mode="json", by_alias=True)


def save_new_log(db: Session, astronaut: AppAstronaut, body, source: str = "app", log_day: int | None = None) -> LogSaved:
    """Save a new log: coins, streak bonus, alerts and Orbi note. `source` is app, device or simulator.

    `log_day` is the mission day the log belongs to when it was recorded offline on an earlier
    day; alerts are always evaluated for today (their 7-day window covers the late log).
    """
    m, today = mission_day(db)
    day = today if log_day is None else log_day
    _check_environment(body, m)
    coins = 0 if body.type == "meditation" else logic.COINS_PER_LOG
    data = _store_data(db, body, astronaut)
    # Streak bonus: the first mood check-in of the day that completes 7, 14, 21... days in a row.
    bonus = None
    if body.type == "mood":
        logs = views.astronaut_logs(db, astronaut.id)
        if not any(l.type == "mood" and l.mission_day == day for l in logs):
            run = views.streak(logs, day - 1) + 1 if day > 1 else 1
            if run % logic.STREAK_DAYS == 0:
                bonus = {"type": "mood_streak_7", "coins": logic.STREAK_BONUS, "streak": run}
                coins += logic.STREAK_BONUS
    log = AppLog(
        id=new_id("log"), astronaut_id=astronaut.id, type=body.type,
        timestamp=body.timestamp or datetime.now(timezone.utc), mission_day=day,
        environment_id=body.environment_id, note=body.note, coins_earned=coins,
        data=data, source=source,
    )
    db.add(log)
    astronaut.avatar = {**astronaut.avatar, "coins": astronaut.avatar.get("coins", 0) + coins}
    db.flush()
    changed, request_id = _after_save(db, astronaut, today, log)
    db.commit()
    message = f"Log saved to your mission record · +{coins} mission coins"
    if bonus:
        message += f" ({bonus['streak']}-day check-in streak: +{logic.STREAK_BONUS})"
    if request_id:
        message += ". Severe injury: flagged to the ground medical team."
    return LogSaved(log=views.log_out(log), coins=astronaut.avatar["coins"], coins_earned=coins,
                    logged_today=day == today, alerts=[views.alert_out(a) for a in changed],
                    support_request_id=request_id, bonus=bonus, message=message)


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
    log.data, log.note, log.environment_id = _store_data(db, body, astronaut, log.id), body.note, body.environment_id
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


# --- chat with Orbi (rule-based for now) -----------------------------------------------------


def _chat_out(c: AppChatMessage) -> dict:
    return {"id": c.id, "astronautId": c.astronaut_id, "author": c.author, "text": c.text,
            "timestamp": views.iso(c.timestamp), "mode": c.mode,
            "provenance": "SIMULATED_TELEMETRY" if c.source == "seed"
            else "USER_REPORTED" if c.author == "astronaut" else "RULE_DERIVED"}


@router.get("/orbi/messages", summary="Chat history with Orbi (journal mode only), oldest first")
def orbi_messages(limit: int = Query(200, ge=1, le=1000), astronaut: AppAstronaut = Depends(current_astronaut),
                  db: Session = Depends(get_db)):
    rows = db.scalars(select(AppChatMessage).where(AppChatMessage.astronaut_id == astronaut.id)
                      .order_by(AppChatMessage.timestamp.desc(), AppChatMessage.id.desc()).limit(limit)).all()
    return [_chat_out(c) for c in reversed(rows)]


@router.post("/orbi/chat", summary="Talk to Orbi. journal: saved with a journal entry; private: nothing is saved")
def orbi_chat(body: OrbiChatIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    """Answers come from rules (keywords plus the day's summary), labelled RULE_DERIVED.

    A language model can replace the rules later behind this same route.
    """
    m, day = mission_day(db)
    text = body.text.strip()
    reply, topic = orbi.reply_to(text, astronaut.name.split()[0], evaluate(db, astronaut, day, m))
    out = {"reply": reply, "mode": body.mode, "topic": topic, "saved": body.mode == "journal",
           "journalEntry": None, "messages": [], "provenance": "RULE_DERIVED"}
    if body.mode == "private":
        return out  # nothing is written, not even the reply
    now = utcnow()
    mine = AppChatMessage(id=new_id("msg"), astronaut_id=astronaut.id, author="astronaut", text=text,
                          timestamp=now, mode="journal", source="app")
    answer = AppChatMessage(id=new_id("msg"), astronaut_id=astronaut.id, author="orbi", text=reply,
                            timestamp=now + timedelta(milliseconds=1), mode="journal", source="rules")
    moods = [l for l in views.astronaut_logs(db, astronaut.id) if l.type == "mood" and l.mission_day == day]
    mood_level = max(moods, key=lambda l: l.timestamp).data["level"] if moods else orbi.MOOD_HINT.get(topic)
    entry = AppJournalEntry(id=new_id("jrn"), astronaut_id=astronaut.id, timestamp=now,
                            summary=orbi.journal_summary(text), mood_level=mood_level,
                            source_message_ids=[mine.id, answer.id])
    db.add_all([mine, answer, entry])
    db.commit()
    out["messages"] = [_chat_out(mine), _chat_out(answer)]
    out["journalEntry"] = {"id": entry.id, "astronautId": entry.astronaut_id, "timestamp": views.iso(entry.timestamp),
                           "summary": entry.summary, "moodLevel": entry.mood_level,
                           "sourceMessageIds": entry.source_message_ids}
    return out


# --- uploads (voice note of the mood check-in, injury photo) ----------------------------------


@router.post("/uploads", status_code=201, summary="Upload an audio or image file; returns its url")
async def upload(request: Request, file: UploadFile = File(...), astronaut: AppAstronaut = Depends(current_astronaut)):
    """Use the returned `url` in `voiceNote.url` (mood) or `photoUrl` (injury).

    Accepts audio (m4a, aac, mp3, wav, webm, ogg, 3gp) and images (jpg, png, webp, heic).
    The file gets a random name; whoever has the url can open it.
    """
    from app.routers.app_public import UPLOAD_TYPES

    settings = get_settings()
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    ext = UPLOAD_TYPES.get(content_type)
    if ext is None:
        raise HTTPException(415, f"Unsupported file type {content_type or 'unknown'!r}; send audio or an image")
    limit = settings.upload_max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File larger than {settings.upload_max_mb} MB")
    if not data:
        raise HTTPException(422, "Empty file")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (settings.upload_dir / name).write_bytes(data)
    path = f"/api/v1/app/uploads/{name}"
    return {"url": str(request.base_url).rstrip("/") + path, "path": path, "contentType": content_type,
            "sizeBytes": len(data), "kind": content_type.split("/")[0]}


# --- device telemetry and Orbi confirmations ---------------------------------------------------


def _ingest(db: Session, astronaut: AppAstronaut, day: int, source: str, devices: dict[str, list[dict]],
            simulated: bool) -> dict:
    stored, dupes, confs, batches = [], 0, [], []
    for device, items in devices.items():
        res = telemetry.ingest(db, astronaut, day, source if not simulated else "simulator", device, items, simulated)
        stored += res.stored
        dupes += res.duplicates
        confs += res.confirmations
        batches.append(res.batch_id)
    db.commit()
    return {"accepted": len(stored), "duplicates": dupes, "batchIds": batches,
            "byMetric": dict(Counter(r.metric for r in stored)),
            "confirmations": [telemetry.confirmation_out(c) for c in confs],
            "sync": telemetry.sync_status(db, astronaut),
            "provenance": SIMULATED if simulated else DEVICE}


@router.post("/telemetry", status_code=201, summary="Sync readings from a wearable, cuff, dosimeter or EVA suit")
def sync_telemetry(body: TelemetryIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    """Send what the phone read from Health Connect / HealthKit / Bluetooth, in batches.

    Resending the same readings is safe (duplicates are skipped). Readings that match a daily
    log, or fall outside their usual range, open a confirmation for Orbi to ask about.
    """
    _, day = mission_day(db)
    items, errors = [], []
    for i, r in enumerate(body.readings):
        try:
            items.append({"metric": r.metric, "measuredAt": r.measured_at,
                          "values": telemetry.normalize(r.metric, r.value, r.values)})
        except ValueError as exc:
            errors.append({"loc": ["body", "readings", i], "msg": str(exc), "type": "value_error"})
    if errors:
        raise HTTPException(422, errors)
    return _ingest(db, astronaut, day, body.source, {body.device: items}, simulated=False)


@router.post("/telemetry/simulate", status_code=201,
             summary="Simulate a sync (wrist band, cuff, dosimeter, optional EVA suit): no hardware in the prototype")
def simulate_telemetry(body: SimulateIn = SimulateIn(), astronaut: AppAstronaut = Depends(current_astronaut),
                       db: Session = Depends(get_db)):
    """Readings are labelled SIMULATED_TELEMETRY. `anomaly` injects a reading Orbi will ask about."""
    _, day = mission_day(db)
    devices = telemetry.simulate(astronaut, day, datetime.now(timezone.utc), body.hours, body.eva, body.anomaly)
    return _ingest(db, astronaut, day, "simulator", devices, simulated=True)


@router.get("/telemetry", summary="My device readings, newest first, with a summary per metric")
def my_telemetry(metric: str | None = Query(None), hours: int = Query(24, ge=1, le=24 * 30),
                 limit: int = Query(500, ge=1, le=5000), astronaut: AppAstronaut = Depends(current_astronaut),
                 db: Session = Depends(get_db)):
    return telemetry_view(db, astronaut.id, metric, hours, limit)


def telemetry_view(db: Session, astronaut_id: str, metric: str | None, hours: int, limit: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = select(AppDeviceReading).where(AppDeviceReading.astronaut_id == astronaut_id,
                                          AppDeviceReading.measured_at >= since)
    if metric:
        if metric not in telemetry.METRICS:
            raise HTTPException(422, f"Unknown metric {metric!r}")
        stmt = stmt.where(AppDeviceReading.metric == metric)
    rows = list(db.scalars(stmt.order_by(AppDeviceReading.measured_at.desc()).limit(limit)))
    return {"astronautId": astronaut_id, "hours": hours, "summary": telemetry.summary(rows),
            "readings": [telemetry.reading_out(r) for r in rows]}


@router.get("/devices/status", summary="Last sync per device, paired devices and pending confirmations")
def devices_status(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    return telemetry.sync_status(db, astronaut)


def _confirmation(db: Session, astronaut: AppAstronaut, conf_id: str) -> AppConfirmation:
    conf = db.get(AppConfirmation, conf_id)
    if conf is None or conf.astronaut_id != astronaut.id:
        raise HTTPException(404, "Confirmation not found")
    if conf.status != "pending":
        raise HTTPException(409, f"Already {conf.status}")
    return conf


@router.get("/confirmations", summary="What Orbi needs you to confirm: readings to save and quick checks")
def confirmations(status: Literal["pending", "all"] = "pending", astronaut: AppAstronaut = Depends(current_astronaut),
                  db: Session = Depends(get_db)):
    stmt = select(AppConfirmation).where(AppConfirmation.astronaut_id == astronaut.id)
    if status == "pending":
        stmt = stmt.where(AppConfirmation.status == "pending")
    rows = db.scalars(stmt.order_by(AppConfirmation.created_at.desc(), AppConfirmation.id))
    return [telemetry.confirmation_out(c) for c in rows]


@router.post("/confirmations/{conf_id}/confirm", summary="Save a reading as a log, as proposed or corrected (+10 coins)")
def confirm(conf_id: str, body: ConfirmIn = ConfirmIn(), astronaut: AppAstronaut = Depends(current_astronaut),
            db: Session = Depends(get_db)):
    conf = _confirmation(db, astronaut, conf_id)
    if conf.kind != "log":
        raise HTTPException(422, "This is a quick check: answer it with /answer")
    _, day = mission_day(db)
    if telemetry._log_exists(db, astronaut.id, day, conf.log_type, conf.metric):
        raise HTTPException(409, "You already logged this today; dismiss the confirmation or edit the log")
    data = {**conf.proposed, **(body.correction or {})}
    reading = db.get(AppDeviceReading, conf.reading_ids[0]) if conf.reading_ids else None
    payload = {"type": conf.log_type, "data": data, "note": body.note,
               "timestamp": reading.measured_at if reading else None,
               "environmentId": "sleep" if conf.log_type == "sleep" else None}
    try:
        log_in = TypeAdapter(LogIn).validate_python(payload)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json(include_url=False))) from None
    saved = save_new_log(db, astronaut, log_in, source="simulator" if conf.simulated else "device")
    conf.status = "corrected" if body.correction else "confirmed"
    conf.log_id, conf.resolved_at = saved.log.id, utcnow()
    conf.answer = {"correction": body.correction} if body.correction else None
    db.commit()
    return {"confirmation": telemetry.confirmation_out(conf), "saved": saved.model_dump(mode="json", by_alias=True)}


@router.post("/confirmations/{conf_id}/answer", summary="Answer a quick check: ok, or not_ok with symptoms")
def answer(conf_id: str, body: AnswerIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    """not_ok raises an alert for the ground team and opens a support request (urgent for SpO2 and EVA checks)."""
    conf = _confirmation(db, astronaut, conf_id)
    if conf.kind != "check":
        raise HTTPException(422, "This is a reading to save: use /confirm or /dismiss")
    _, day = mission_day(db)
    conf.status, conf.resolved_at = "answered", utcnow()
    conf.answer = {"answer": body.answer, "symptoms": body.symptoms, "note": body.note}
    alert = None
    reply = "Thanks. I'll keep an eye on it; tell me if anything changes."
    if body.answer == "not_ok":
        urgent = telemetry.CHECKS[conf.metric][2]
        label = telemetry.METRICS[conf.metric].label
        value = conf.proposed.get("value")
        symptoms = ", ".join(s.replace("_", " ") for s in body.symptoms) or "not specified"
        alert = AppAlert(id=new_id("alr"), astronaut_id=astronaut.id, type="device_check",
                         level="high" if urgent else "medium",
                         text=f"{label} {value:g} {conf.proposed.get('unit', '')} and the astronaut is not feeling well "
                              f"(symptoms: {symptoms}).", evidence=conf.reading_ids, timestamp=utcnow(), mission_day=day,
                         sent_to_ground=True, status="active", source="device")
        db.add(alert)
        db.flush()
        conf.alert_id = alert.id
        open_request(db, astronaut_id=astronaut.id, mission_id=astronaut.mission_id, mission_day=day,
                     reason="urgent_symptom" if urgent else "pattern", detail=f"device_check:{conf.metric}",
                     topic="medical", priority="urgent" if urgent else "routine",
                     message=f"CosmoMedic device check: {alert.text}" + (f" Note: {body.note}" if body.note else ""))
        reply = ("I've told the ground medical team. Stop what you are doing, tell a crewmate, and send an urgent "
                 "message to Mission Control if it gets worse." if urgent else
                 "I've let the ground medical team know. Rest, drink water, and log how you feel later.")
    db.commit()
    return {"confirmation": telemetry.confirmation_out(conf), "reply": reply,
            "alert": views.alert_out(alert).model_dump(mode="json", by_alias=True) if alert else None}


@router.post("/confirmations/{conf_id}/dismiss", summary="Discard a reading or a check (nothing is saved)")
def dismiss(conf_id: str, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    conf = _confirmation(db, astronaut, conf_id)
    conf.status, conf.resolved_at = "dismissed", utcnow()
    db.commit()
    return telemetry.confirmation_out(conf)


# --- meal plan ----------------------------------------------------------------------------------


@router.get("/meal-plan", summary="Planned meals of a day, with what was logged for each")
def my_meal_plan(day: int | None = Query(None, ge=1, description="Mission day (default: today)"),
                 astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, today = mission_day(db)
    return views.meal_plan(db, astronaut, min(day or today, m.length_days))


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
def send_ground(body: GroundMessageIn, idempotency_key: str | None = IDEMPOTENCY_KEY,
                astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    if idempotency_key is None:
        return post_ground_message(db, astronaut, body)

    def run():
        out = post_ground_message(db, astronaut, body)
        return out.id, out.model_dump(mode="json", by_alias=True)
    return JSONResponse(offline.once(db, astronaut, idempotency_key, "message.send", None, run), status_code=201)


def post_ground_message(db: Session, astronaut: AppAstronaut, body: GroundMessageIn) -> GroundMessageOut:
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
