"""CosmoMedic app offline support: local-copy bundle, changes since a cursor and the queue upload."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cosmo import nutrition, offline, orbi, reflection, telemetry, views
from app.cosmo.models import AppAstronaut, AppChatMessage, AppFood, AppJournalEntry, AppLog, AppSyncOp
from app.cosmo.schemas import AnswerIn, ConfirmIn, GroundMessageIn, LogIn, TelemetryIn
from app.cosmo.service import astronaut_logs, evaluate, new_id
from app.db import get_db
from app.routers import app_me
from app.routers.app_me import current_astronaut, mission_day
from app.models import utcnow
from app.schemas import APIModel

router = APIRouter(prefix="/api/v1/app/me", tags=["app: offline"])


class SyncOp(APIModel):
    op_id: str = Field(min_length=8, max_length=64, description="Id made by the app (UUID). Applied at most once.")
    type: Literal["log.create", "log.update", "telemetry", "confirmation.confirm", "confirmation.answer",
                  "confirmation.dismiss", "message.send", "message.read", "chat.save", "consultation.request"]
    recorded_at: datetime | None = Field(None, description="When the astronaut did it, on the device clock")
    mission_day: int | None = Field(None, ge=1, description="log.create: mission day it belongs to (default: today)")
    target_id: str | None = Field(None, max_length=70, description="Log, confirmation or message id. For a log created "
                                                                    "offline in this queue: op:<opId of its log.create>")
    base_updated_at: datetime | None = Field(None, description="log.update: when the copy edited offline was fetched; "
                                                               "if the log changed on the server since, it is a conflict")
    payload: dict[str, Any] = Field(default_factory=dict, description="Body of the matching online route")


class ChatSaveIn(APIModel):
    """A journal-mode exchange with Orbi that happened on the device (Orbi's rules run in the app when offline)."""

    text: str = Field(min_length=1, max_length=4000)
    reply: str = Field(min_length=1, max_length=4000)
    summary: str | None = Field(None, max_length=400, description="Journal line; default: made from the text")
    mood_level: int | None = Field(None, ge=1, le=5)


class SyncIn(APIModel):
    device_id: str | None = Field(None, max_length=64)
    client_time: datetime | None = Field(None, description="Device clock when sending, to report clock drift")
    since: str | None = Field(None, description="Cursor of the last answer; the answer brings what changed after it")
    queued_remaining: int | None = Field(None, ge=0, description="Operations still on the device after this batch")
    ops: list[SyncOp] = Field(default_factory=list, max_length=offline.MAX_OPS)


def _validate(adapter_type, payload: dict):
    try:
        return TypeAdapter(adapter_type).validate_python(payload)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json(include_url=False))) from None


def _resolve(db: Session, astronaut: AppAstronaut, ref: str | None, what: str) -> str:
    """An id, or op:<opId> for something created earlier in the offline queue."""
    if not ref:
        raise HTTPException(422, f"targetId is required ({what} id)")
    if not ref.startswith("op:"):
        return ref
    op = offline.find_op(db, astronaut.id, ref[3:])
    if op is None or op.status != "applied" or not op.entity_id:
        raise HTTPException(422, f"{ref!r} does not point to an operation that was applied")
    return op.entity_id


def _resolve_confirmation(db: Session, astronaut: AppAstronaut, op: SyncOp) -> str:
    """A confirmation id, or op:<opId of the telemetry operation> for a question the app asked on the device.

    Offline, the app runs the device checks itself (bundle `rules.deviceChecks`), so it has no
    confirmation id yet: it points to the telemetry operation that carries the reading, and
    `payload.metric` picks the confirmation when that batch opened more than one.
    """
    ref = op.target_id or ""
    if not ref.startswith("op:"):
        return _resolve(db, astronaut, op.target_id, "confirmation")
    source = offline.find_op(db, astronaut.id, ref[3:])
    confs = (source.result or {}).get("confirmations", []) if source and source.status == "applied" else []
    metric = op.payload.get("metric")
    match = [c for c in confs if metric is None or c.get("metric") == metric]
    if len(match) != 1:
        raise HTTPException(422, f"{ref!r} opened {len(match)} confirmations"
                                 + (f" for metric {metric!r}" if metric else "; send payload.metric to pick one"))
    return match[0]["id"]


def _without_metric(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "metric"}


def _json(value) -> Any:
    return value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value


def _run(db: Session, astronaut: AppAstronaut, op: SyncOp, recorded_at: datetime | None, today: int):
    """Apply one queued operation with the same code as its online route. Returns (entity id, result)."""
    if op.type == "log.create":
        body = _validate(LogIn, {"timestamp": recorded_at, **op.payload})
        if op.mission_day is not None and op.mission_day > today:
            raise HTTPException(422, f"missionDay {op.mission_day} is after today (mission day {today})")
        saved = app_me.save_new_log(db, astronaut, body, log_day=op.mission_day)
        return saved.log.id, _json(saved)
    if op.type == "log.update":
        log_id = _resolve(db, astronaut, op.target_id, "log")
        body = _validate(LogIn, op.payload)
        log = db.get(AppLog, log_id)
        if (log is not None and log.astronaut_id == astronaut.id and op.base_updated_at is not None
                and telemetry._as_utc(log.updated_at) > telemetry._as_utc(op.base_updated_at)):
            raise offline.Conflict("The log changed on the server after the copy edited offline; the server copy is in result",
                                   _json(views.log_out(log)))
        return log_id, _json(app_me.update_log(log_id, body, astronaut, db))
    if op.type == "telemetry":
        return None, app_me.sync_telemetry(_validate(TelemetryIn, op.payload), astronaut, db)
    if op.type == "confirmation.confirm":
        conf_id = _resolve_confirmation(db, astronaut, op)
        return conf_id, app_me.confirm(conf_id, _validate(ConfirmIn, _without_metric(op.payload)), astronaut, db)
    if op.type == "confirmation.answer":
        conf_id = _resolve_confirmation(db, astronaut, op)
        return conf_id, app_me.answer(conf_id, _validate(AnswerIn, _without_metric(op.payload)), astronaut, db)
    if op.type == "confirmation.dismiss":
        conf_id = _resolve_confirmation(db, astronaut, op)
        return conf_id, app_me.dismiss(conf_id, astronaut, db)
    if op.type == "message.send":
        payload = dict(op.payload)
        payload["attachments"] = [_resolve(db, astronaut, a, "log") for a in payload.get("attachments") or []]
        out = app_me.post_ground_message(db, astronaut, _validate(GroundMessageIn, payload))
        return out.id, _json(out)
    if op.type == "consultation.request":
        out = app_me.request_consultation(db, astronaut, _validate(app_me.ConsultationIn, op.payload))
        return str(out["requestId"]), out
    if op.type == "chat.save":
        body = _validate(ChatSaveIn, op.payload)
        at = recorded_at or utcnow()
        mine = AppChatMessage(id=new_id("msg"), astronaut_id=astronaut.id, author="astronaut", text=body.text.strip(),
                              timestamp=at, mode="journal", source="app")
        answer = AppChatMessage(id=new_id("msg"), astronaut_id=astronaut.id, author="orbi", text=body.reply.strip(),
                                timestamp=at + timedelta(milliseconds=1), mode="journal", source="device")
        entry = AppJournalEntry(id=new_id("jrn"), astronaut_id=astronaut.id, timestamp=at,
                                summary=body.summary or orbi.journal_summary(body.text), mood_level=body.mood_level,
                                source_message_ids=[mine.id, answer.id])
        db.add_all([mine, answer, entry])
        db.commit()
        return entry.id, {"messageIds": [mine.id, answer.id], "journalEntryId": entry.id}
    message_id = _resolve(db, astronaut, op.target_id, "message")
    return message_id, _json(app_me.read_ground(message_id, astronaut, db))


@router.post("/sync", summary="Send the offline queue (each operation is applied at most once) and get what changed")
def sync(body: SyncIn, astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    """Operations run in the order sent, with the same rules as the online routes.

    Each result has a status: `applied`, `rejected` (the server refused it: drop it from the
    queue and show `error`) or `conflict` (the record changed on the server: `result` has the
    server copy). `duplicate: true` means the operation had already arrived and the first
    answer is returned. A 5xx or a network error means nothing is known: send the batch again.
    """
    m, today = mission_day(db)
    now = datetime.now(timezone.utc)
    since = offline.parse_cursor(body.since)
    results, lags, adjusted = [], [], 0
    for op in body.ops:
        recorded_at, clamped = offline.clamp_recorded(op.recorded_at, now)
        adjusted += clamped
        record, duplicate = offline.apply(db, astronaut, op.op_id, op.type, recorded_at,
                                          lambda op=op, recorded_at=recorded_at: _run(db, astronaut, op, recorded_at, today))
        out = offline.op_out(record, duplicate)
        if record.type != op.type:
            out |= {"status": "rejected", "httpStatus": 422, "result": None,
                    "error": {"detail": f"opId already used for {record.type!r}"}}
        results.append(out)
        if not duplicate and out["lagSec"] is not None:
            lags.append(out["lagSec"])
    counts = Counter("duplicate" if r["duplicate"] else r["status"] for r in results)
    batch = None
    if body.ops:
        recorded = [op.recorded_at for op in body.ops if op.recorded_at]
        batch = {"ops": len(body.ops), "applied": counts["applied"], "duplicates": counts["duplicate"],
                 "rejected": counts["rejected"], "conflicts": counts["conflict"],
                 "oldestRecordedAt": offline.iso(min(recorded)) if recorded else None,
                 "maxLagSec": max(lags) if lags else None}
    offline.touch(db, astronaut, batch=batch, queued=body.queued_remaining, device_id=body.device_id)
    skew = round((now - telemetry._as_utc(body.client_time)).total_seconds()) if body.client_time else None
    return {
        "serverTime": offline.iso(now),
        "clockSkewSec": skew,
        "clockAdjustedOps": adjusted,
        "summary": {"received": len(body.ops), "applied": counts["applied"], "duplicates": counts["duplicate"],
                    "rejected": counts["rejected"], "conflicts": counts["conflict"]},
        "results": results,
        "changes": offline.changes(db, astronaut, m, today, since),
    }


@router.get("/changes", summary="What changed since a cursor: logs, alerts, confirmations, ground messages, home")
def changes(since: str | None = Query(None, description="Cursor of a previous answer; empty: the recent data"),
            astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    m, today = mission_day(db)
    out = offline.changes(db, astronaut, m, today, offline.parse_cursor(since))
    offline.touch(db, astronaut)
    return out


@router.get("/offline-bundle", summary="Everything the app needs to keep working with no link (local copy)")
def offline_bundle(astronaut: AppAstronaut = Depends(current_astronaut), db: Session = Depends(get_db)):
    """Fetch it after login and whenever there is a link; then keep it up to date with /sync or /changes."""
    m, today = mission_day(db)
    logs = astronaut_logs(db, astronaut.id)
    ev = evaluate(db, astronaut, today, m, logs)
    base = offline.changes(db, astronaut, m, today, None)
    dump = lambda model: model.model_dump(mode="json", by_alias=True)
    first_day = max(1, today - 6)
    bundle = {
        "generatedAt": base["serverTime"],
        "cursor": base["cursor"],
        "validForHours": offline.BUNDLE_VALID_HOURS,
        "missionDay": today,
        "profile": app_me.profile(astronaut, db),
        "preferences": app_me._preferences(db, astronaut),
        "home": base["home"],
        "status": views.status(db, astronaut, m, today),
        "summaries": [dump(views.summary_out(evaluate(db, astronaut, d, m, logs))) for d in range(first_day, today + 1)],
        "logs": base["logs"],
        "logsFromDay": max(1, today - offline.BUNDLE_LOG_DAYS + 1),
        "alerts": base["alerts"],
        "confirmations": base["confirmations"],
        "groundMessages": base["groundMessages"],
        "mealPlan": [views.meal_plan(db, astronaut, d, logs)
                     for d in range(today, min(today + offline.BUNDLE_PLAN_DAYS, m.length_days + 1))],
        "foods": [nutrition.food_out(f) for f in db.scalars(select(AppFood).order_by(AppFood.name))],
        "telemetryMetrics": [{"metric": s.code, "unit": s.unit, "label": s.label, "min": s.lo, "max": s.hi}
                             for s in telemetry.METRICS.values()],
        "rules": offline.rules(m),
        "reflection": reflection.build(db, astronaut, m, today, ai=False),
        "orbi": offline.orbi_offline(astronaut, ev),
        "sync": {"endpoint": "/api/v1/app/me/sync", "changesEndpoint": "/api/v1/app/me/changes",
                 "maxOpsPerBatch": offline.MAX_OPS, "opTypes": offline.OP_TYPES,
                 "onlineOnly": ["orbi/chat (use the bundled replies)", "uploads", "avatar/purchase", "meal-plan edits"]},
        "provenance": "RULE_DERIVED",
    }
    offline.touch(db, astronaut)
    return bundle


@router.get("/sync/ops", summary="Operations received from my offline queue, newest first")
def my_ops(limit: int = Query(100, ge=1, le=500), astronaut: AppAstronaut = Depends(current_astronaut),
           db: Session = Depends(get_db)):
    rows = db.scalars(select(AppSyncOp).where(AppSyncOp.astronaut_id == astronaut.id)
                      .order_by(AppSyncOp.received_at.desc(), AppSyncOp.id.desc()).limit(limit))
    return [{k: v for k, v in offline.op_out(op).items() if k != "result"} for op in rows]
