"""Offline support for the CosmoMedic app.

Between the spacecraft and Earth there is a communication delay and the link drops, so the
app must keep working with no server: it saves what the astronaut does in a local queue and
keeps a copy of what it needs to show. This module is the server side of that:

- `once` / `apply`: each queued operation carries an id made by the app and is applied at
  most once, so the app can resend a batch after a dropped connection without duplicates.
- `changes`: what changed for the astronaut since a cursor, to refresh the local copy.
- `rules` and `orbi_offline`: the thresholds and Orbi's rule-based replies, so the app can
  run the same checks and answer on the device while there is no link.
- `link`: how long ago the app last reached the server, for the ground team to know how old
  the data on their dashboard is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cosmo import logic, orbi, telemetry, views
from app.cosmo.models import (
    AppAlert,
    AppAstronaut,
    AppConfirmation,
    AppGroundMessage,
    AppLog,
    AppMission,
    AppSyncOp,
    AppSyncState,
)
from app.cosmo.service import astronaut_logs, evaluate
from app.models import utcnow

OP_TYPES = ["log.create", "log.update", "telemetry", "confirmation.confirm", "confirmation.answer",
            "confirmation.dismiss", "message.send", "message.read"]
MAX_OPS = 500
BUNDLE_LOG_DAYS = 14
BUNDLE_PLAN_DAYS = 3
BUNDLE_VALID_HOURS = 72
FUTURE_TOLERANCE = timedelta(minutes=5)
# Contact age shown to the ground team.
ONLINE_MINUTES = 15
DELAYED_HOURS = 12


class Conflict(Exception):
    """The record changed on the server after the copy the app edited offline."""

    def __init__(self, message: str, server: dict):
        super().__init__(message)
        self.message, self.server = message, server


def iso(dt: datetime | None) -> str | None:
    return telemetry._iso(telemetry._as_utc(dt)) if dt else None


# --- operations applied at most once ---------------------------------------------------------


def find_op(db: Session, astronaut_id: str, op_id: str) -> AppSyncOp | None:
    return db.scalar(select(AppSyncOp).where(AppSyncOp.astronaut_id == astronaut_id, AppSyncOp.op_id == op_id))


def apply(db: Session, astronaut: AppAstronaut, op_id: str, op_type: str, recorded_at: datetime | None,
          run: Callable[[], tuple[str | None, object]]) -> tuple[AppSyncOp, bool]:
    """Run `run` unless this op id was already handled. Returns (record, duplicate).

    `run` returns (entity id, JSON result) and commits its own work; the record of the op is
    added to the same transaction, so a crash never leaves the work saved without the record.
    A request the server refuses (4xx) is recorded too: resending it gets the same answer.
    """
    astronaut_id = astronaut.id
    existing = find_op(db, astronaut_id, op_id)
    if existing is not None:
        return existing, True
    op = AppSyncOp(astronaut_id=astronaut_id, op_id=op_id, type=op_type, status="applied", http_status=200,
                   recorded_at=recorded_at, received_at=utcnow())
    db.add(op)
    try:
        db.flush()
    except IntegrityError:  # the same op arrived twice at the same time
        db.rollback()
        return find_op(db, astronaut_id, op_id), True
    try:
        entity_id, result = run()
        op.entity_id, op.result = entity_id, jsonable_encoder(result, by_alias=True)
        db.commit()
        return op, False
    except Conflict as exc:
        db.rollback()
        status, code, error, result = "conflict", 409, {"detail": exc.message}, exc.server
    except HTTPException as exc:
        db.rollback()
        if exc.status_code >= 500:
            raise
        status, code, error, result = "rejected", exc.status_code, {"detail": jsonable_encoder(exc.detail)}, None
    # The work may have committed part of itself (and this record with it) before failing.
    op = find_op(db, astronaut_id, op_id)
    if op is None:
        op = AppSyncOp(astronaut_id=astronaut_id, op_id=op_id, type=op_type, recorded_at=recorded_at, received_at=utcnow())
        db.add(op)
    op.status, op.http_status, op.error, op.result = status, code, error, result
    db.commit()
    return op, False


def once(db: Session, astronaut: AppAstronaut, key: str, op_type: str, recorded_at: datetime | None,
         run: Callable[[], tuple[str | None, object]]):
    """For single routes called with an Idempotency-Key header: the first answer is returned again."""
    op, _ = apply(db, astronaut, key, op_type, recorded_at, run)
    if op.type != op_type:
        raise HTTPException(422, f"Idempotency-Key {key!r} was already used for {op.type!r}")
    if op.status != "applied":
        raise HTTPException(op.http_status, (op.error or {}).get("detail", "Request refused"))
    return op.result


def op_out(op: AppSyncOp, duplicate: bool = False) -> dict:
    lag = (op.received_at - op.recorded_at).total_seconds() if op.recorded_at else None
    return {"opId": op.op_id, "type": op.type, "status": op.status, "duplicate": duplicate,
            "httpStatus": op.http_status, "entityId": op.entity_id, "recordedAt": iso(op.recorded_at),
            "receivedAt": iso(op.received_at), "lagSec": max(0, round(lag)) if lag is not None else None,
            "result": op.result, "error": op.error}


def clamp_recorded(recorded_at: datetime | None, now: datetime) -> tuple[datetime | None, bool]:
    """A device clock ahead of the server would put logs in the future: bring them back to now."""
    if recorded_at is None:
        return None, False
    recorded_at = telemetry._as_utc(recorded_at)
    if recorded_at > now + FUTURE_TOLERANCE:
        return now, True
    return recorded_at, False


# --- contact with the app ---------------------------------------------------------------------


def touch(db: Session, astronaut: AppAstronaut, *, batch: dict | None = None, queued: int | None = None,
          device_id: str | None = None) -> AppSyncState:
    state = db.get(AppSyncState, astronaut.id)
    if state is None:
        state = AppSyncState(astronaut_id=astronaut.id)
        db.add(state)
    state.last_contact_at = utcnow()
    if batch is not None:
        state.last_upload_at, state.last_batch = state.last_contact_at, batch
    if queued is not None:
        state.queued_on_device = queued
    if device_id:
        state.device_id = device_id
    db.commit()
    return state


def link(db: Session, astronaut: AppAstronaut, m: AppMission) -> dict:
    """How fresh the astronaut's data is on the ground: last contact of the app and of the devices."""
    state = db.get(AppSyncState, astronaut.id)
    contact = telemetry._as_utc(state.last_contact_at) if state else None
    device = telemetry.last_sync(db, astronaut.id)
    device = telemetry._as_utc(device) if device else None
    latest = max((t for t in (contact, device) if t), default=None)
    minutes = round((datetime.now(timezone.utc) - latest).total_seconds() / 60) if latest else None
    status = ("unknown" if minutes is None else "online" if minutes <= ONLINE_MINUTES
              else "delayed" if minutes <= DELAYED_HOURS * 60 else "offline")
    return {
        "status": status,
        "lastContactAt": iso(contact),
        "lastDeviceSyncAt": iso(device),
        "minutesSinceContact": minutes,
        "lastUploadAt": iso(state.last_upload_at) if state else None,
        "lastBatch": state.last_batch if state else None,
        "queuedOnDevice": state.queued_on_device if state else None,
        "commDelaySec": m.comm_delay_sec,
        "note": "online: contact in the last 15 min; delayed: up to 12 h; offline: longer. Data older than the last "
                "contact may still be queued on the device.",
    }


# --- what changed since a cursor --------------------------------------------------------------


def parse_cursor(since: str | None) -> datetime | None:
    if not since:
        return None
    try:
        return telemetry._as_utc(datetime.fromisoformat(since.replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(422, "since must be the cursor of a previous answer (an ISO date-time)") from None


def visible_thread(db: Session, astronaut_id: str, now: datetime) -> list[AppGroundMessage]:
    msgs = db.scalars(select(AppGroundMessage).where(AppGroundMessage.astronaut_id == astronaut_id)
                      .order_by(AppGroundMessage.sent_at))
    return [g for g in msgs if g.sender == "astronaut" or telemetry._as_utc(g.estimated_arrival_at) <= now]


def changes(db: Session, astronaut: AppAstronaut, m: AppMission, day: int, since: datetime | None) -> dict:
    """Records created or changed after `since` (all of the recent ones when there is no cursor)."""
    now = datetime.now(timezone.utc)
    after = lambda dt: dt is not None and (since is None or telemetry._as_utc(dt) > since)
    first_day = max(1, day - BUNDLE_LOG_DAYS + 1)
    logs = [l for l in astronaut_logs(db, astronaut.id)
            if (l.mission_day >= first_day if since is None else after(l.updated_at) or after(l.created_at))]
    logs.sort(key=lambda l: (l.mission_day, l.timestamp))
    alerts = list(db.scalars(select(AppAlert).where(AppAlert.astronaut_id == astronaut.id)))
    confs = list(db.scalars(select(AppConfirmation).where(AppConfirmation.astronaut_id == astronaut.id)))
    thread = visible_thread(db, astronaut.id, now)
    dump = lambda model: model.model_dump(mode="json", by_alias=True)
    return {
        "cursor": iso(now),
        "since": iso(since),
        "serverTime": iso(now),
        "missionDay": day,
        "logs": [dump(views.log_out(l)) for l in logs],
        "alerts": [dump(views.alert_out(a)) for a in alerts
                   if (a.status == "active" if since is None else after(a.timestamp))],
        "activeAlertIds": sorted(a.id for a in alerts if a.status == "active"),
        "confirmations": [telemetry.confirmation_out(c) for c in confs
                          if (c.status == "pending" if since is None else after(c.created_at) or after(c.resolved_at))],
        "pendingConfirmationIds": sorted(c.id for c in confs if c.status == "pending"),
        "groundMessages": [dump(views.message_out(g)) for g in thread
                           if since is None or after(g.sent_at) or after(g.read_at)
                           or (g.sender == "ground" and after(g.estimated_arrival_at))
                           or (g.sender == "astronaut" and since < telemetry._as_utc(g.estimated_arrival_at) <= now)],
        "home": views.home(db, astronaut, m, day),
    }


# --- what the app needs to work with no link ----------------------------------------------------


def rules(m: AppMission) -> dict:
    """The rules of this API in data form, for the app to apply on the device while offline."""
    return {
        "score": {"healthWeight": 0.8, "completenessWeight": 0.2, "countedLogTypes": logic.COUNTED_TYPES,
                  "statusLevels": [{"level": "ok", "minScore": 70}, {"level": "warn", "minScore": 50},
                                   {"level": "alert", "minScore": 0}],
                  "note": "The server recomputes the score when the queue is sent; the offline value is an estimate."},
        "coins": {"perLog": logic.COINS_PER_LOG, "noCoinsFor": ["meditation"], "streakDays": logic.STREAK_DAYS,
                  "streakBonus": logic.STREAK_BONUS},
        "goals": m.goals,
        "deviceChecks": [{"metric": metric, "op": op, "threshold": threshold, "unit": telemetry.METRICS[metric].unit,
                          "urgent": urgent, "question": text,
                          "skipWhenContext": ["exercise", "eva"] if metric == "heart_rate" else []}
                         for metric, (op, threshold, text, urgent) in telemetry.CHECK_RULES.items()],
        "symptoms": list(telemetry.SYMPTOMS),
        "provenance": "RULE_DERIVED",
    }


OFFLINE_URGENT_NOTE = ("There is no link to Earth right now, so this will reach the ground team only when it is back. "
                       "Tell a crewmate now and follow the on-board medical checklist.")


def orbi_offline(astronaut: AppAstronaut, ev: logic.DayEvaluation | None) -> dict:
    """Orbi's rule-based replies per topic, built with today's data, to answer with no link."""
    first = astronaut.name.split()[0]
    topics = []
    for topic, words in orbi.TOPICS:
        reply, found = orbi.reply_to(words[0], first, ev)
        if found != topic:  # an earlier topic also matches this word: skip rather than give the wrong reply
            continue
        topics.append({"topic": topic, "keywords": list(words), "reply": reply,
                       "needsHuman": topic in ("crisis", "urgent"),
                       "offlineNote": OFFLINE_URGENT_NOTE if topic in ("crisis", "urgent") else None})
    return {"matching": "Lower-case the text and use the first topic, in this order, with a keyword in it.",
            "topics": topics, "fallback": orbi.reply_to("", first, ev)[0], "provenance": "RULE_DERIVED"}
