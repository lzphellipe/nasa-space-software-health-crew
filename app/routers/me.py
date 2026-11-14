"""Personal endpoints for the logged-in astronaut (React Native app)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_astronaut
from app.config import get_settings
from app.db import get_db
from app.models import (
    AstronautPreference,
    CheckIn,
    PatternDetection,
    ProactiveMessage,
    SupportRequest,
    User,
    utcnow,
)
from app.schemas import APIModel
from app.services.monitoring import current_day, current_mission
from app.services.patterns import PATTERN_LABELS
from app.services.proactive import proactive_enabled
from app.services.support import TOPICS, open_request
from app.synthetic.checkins import SCALE_LABELS, SYMPTOMS, URGENT_SYMPTOMS

router = APIRouter(prefix="/api/v1/me", tags=["me (astronaut)"])


# --- models -------------------------------------------------------------------


class CheckInIn(APIModel):
    stress: int = Field(ge=1, le=5, description="1 very low … 5 very high")
    fatigue: int = Field(ge=1, le=5, description="1 very low … 5 very high")
    symptoms: list[str] = Field(default_factory=list, description="Codes from GET /api/v1/me/checkins/options")
    notes: str | None = Field(None, max_length=2000)

    @field_validator("symptoms")
    @classmethod
    def known_symptoms(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(SYMPTOMS))
        if unknown:
            raise ValueError(f"Unknown symptoms: {', '.join(unknown)}")
        return sorted(set(value))


class CheckInOut(APIModel):
    id: int
    astronaut_id: str
    mission_day: int
    stress: int
    fatigue: int
    symptoms: list[str]
    notes: str | None
    source: str


class CheckInResult(APIModel):
    checkin: CheckInOut
    urgent_symptoms: list[str]
    support_request_id: int | None
    message: str


class OptionOut(APIModel):
    code: str
    label: str
    urgent: bool = False


class CheckInOptions(APIModel):
    scale: dict[int, str]
    symptoms: list[OptionOut]


class PatternOut(APIModel):
    astronaut_id: str
    as_of_day: int
    window_start: int
    window_end: int
    pattern: str
    label: str
    detail: str
    severity: str
    summary: str
    evidence: dict


class MessageOut(APIModel):
    id: int
    mission_day: int
    title: str
    body: str
    patterns: list[str]
    actions: list[str]
    status: str


class PreferencesIn(APIModel):
    proactive_messages: bool


class PreferencesOut(APIModel):
    proactive_messages: bool
    min_days_between_messages: int


class SupportIn(APIModel):
    topic: Literal["medical", "psychological", "family", "operational", "other"] = "other"
    message: str = Field(min_length=1, max_length=4000)
    urgent: bool = False


class SupportOut(APIModel):
    id: int
    astronaut_id: str
    mission_day: int
    reason: str
    topic: str
    topic_label: str
    priority: str
    message: str
    status: str
    response: str | None
    created_at: datetime
    updated_at: datetime


class MeOut(APIModel):
    username: str
    display_name: str
    astronaut_id: str
    mission_id: str | None
    mission_day: int | None
    preferences: PreferencesOut
    unread_messages: int
    open_support_requests: int


# --- helpers ------------------------------------------------------------------


def pattern_out(d: PatternDetection) -> PatternOut:
    return PatternOut(
        astronaut_id=d.astronaut_id, as_of_day=d.as_of_day, window_start=d.window_start, window_end=d.window_end,
        pattern=d.pattern, label=PATTERN_LABELS.get(d.pattern, d.pattern), detail=d.detail, severity=d.severity,
        summary=d.summary, evidence=d.evidence,
    )


def support_out(r: SupportRequest) -> SupportOut:
    return SupportOut(
        id=r.id, astronaut_id=r.astronaut_id, mission_day=r.mission_day, reason=r.reason, topic=r.topic,
        topic_label=TOPICS.get(r.topic, r.topic), priority=r.priority, message=r.message, status=r.status,
        response=r.response, created_at=r.created_at, updated_at=r.updated_at,
    )


def preferences_out(db: Session, astronaut_id: str) -> PreferencesOut:
    return PreferencesOut(
        proactive_messages=proactive_enabled(db, astronaut_id),
        min_days_between_messages=get_settings().proactive_min_interval_days,
    )


def mission_and_day(db: Session) -> tuple[str, int]:
    mission = current_mission(db)
    day = current_day(db)
    if mission is None or day is None:
        raise HTTPException(503, "No mission loaded")
    return mission.id, day


# --- endpoints ----------------------------------------------------------------


@router.get("", response_model=MeOut, summary="Profile, mission day and counters")
def me(user: User = Depends(require_astronaut), db: Session = Depends(get_db)):
    mission = current_mission(db)
    aid = user.astronaut_id
    unread = len(db.scalars(select(ProactiveMessage.id).where(
        ProactiveMessage.astronaut_id == aid, ProactiveMessage.status == "unread")).all())
    open_requests = len(db.scalars(select(SupportRequest.id).where(
        SupportRequest.astronaut_id == aid, SupportRequest.status != "resolved")).all())
    return MeOut(
        username=user.username, display_name=user.display_name, astronaut_id=aid,
        mission_id=mission.id if mission else None, mission_day=current_day(db),
        preferences=preferences_out(db, aid), unread_messages=unread, open_support_requests=open_requests,
    )


@router.get("/checkins/options", response_model=CheckInOptions, summary="Scale labels and symptom list for the form")
def checkin_options(_: User = Depends(require_astronaut)):
    return CheckInOptions(
        scale=SCALE_LABELS,
        symptoms=[OptionOut(code=c, label=label, urgent=urgent) for c, (label, urgent) in SYMPTOMS.items()],
    )


@router.post("/checkins", response_model=CheckInResult, summary="Send today's check-in (replaces today's earlier one)")
def submit_checkin(body: CheckInIn, user: User = Depends(require_astronaut), db: Session = Depends(get_db)):
    mission_id, day = mission_and_day(db)
    aid = user.astronaut_id
    checkin = db.scalar(select(CheckIn).where(CheckIn.astronaut_id == aid, CheckIn.mission_day == day))
    if checkin is None:
        checkin = CheckIn(astronaut_id=aid, mission_id=mission_id, mission_day=day)
        db.add(checkin)
    checkin.stress, checkin.fatigue = body.stress, body.fatigue
    checkin.symptoms, checkin.notes = body.symptoms, body.notes
    checkin.source, checkin.updated_at = "app", utcnow()
    db.flush()

    urgent = [s for s in body.symptoms if s in URGENT_SYMPTOMS]
    request_id = None
    if urgent:
        labels = ", ".join(SYMPTOMS[s][0].lower() for s in urgent)
        request = open_request(
            db, astronaut_id=aid, mission_id=mission_id, mission_day=day, reason="urgent_symptom",
            detail=",".join(urgent), topic="medical", priority="urgent",
            message=f"Check-in reports {labels}." + (f" Notes: {body.notes}" if body.notes else ""),
        )
        request_id = request.id
        message = (
            "Thanks. Because you reported " + labels + ", an urgent request is now in the flight surgeon's "
            "queue. If it gets worse, call the crew medical officer or the ground right away."
        )
    else:
        message = "Thanks, your check-in was saved."
    db.commit()
    db.refresh(checkin)
    return CheckInResult(
        checkin=CheckInOut.model_validate(checkin, from_attributes=True),
        urgent_symptoms=urgent, support_request_id=request_id, message=message,
    )


@router.get("/checkins", response_model=list[CheckInOut], summary="My check-ins")
def my_checkins(
    from_day: int | None = Query(None, alias="fromDay"),
    to_day: int | None = Query(None, alias="toDay"),
    user: User = Depends(require_astronaut),
    db: Session = Depends(get_db),
):
    stmt = select(CheckIn).where(CheckIn.astronaut_id == user.astronaut_id)
    if from_day is not None:
        stmt = stmt.where(CheckIn.mission_day >= from_day)
    if to_day is not None:
        stmt = stmt.where(CheckIn.mission_day <= to_day)
    return [CheckInOut.model_validate(c, from_attributes=True) for c in db.scalars(stmt.order_by(CheckIn.mission_day))]


@router.get("/patterns", response_model=list[PatternOut], summary="Patterns found in my last 7 days")
def my_patterns(
    day: int | None = Query(None, description="Default: current mission day"),
    user: User = Depends(require_astronaut),
    db: Session = Depends(get_db),
):
    day = current_day(db) if day is None else day
    stmt = select(PatternDetection).where(
        PatternDetection.astronaut_id == user.astronaut_id, PatternDetection.as_of_day == day
    )
    return [pattern_out(d) for d in db.scalars(stmt)]


@router.get("/messages", response_model=list[MessageOut], summary="Proactive messages, newest first")
def my_messages(
    status: str | None = Query(None, description="unread | read | dismissed"),
    user: User = Depends(require_astronaut),
    db: Session = Depends(get_db),
):
    stmt = select(ProactiveMessage).where(ProactiveMessage.astronaut_id == user.astronaut_id)
    if status:
        stmt = stmt.where(ProactiveMessage.status == status)
    return [MessageOut.model_validate(m, from_attributes=True)
            for m in db.scalars(stmt.order_by(ProactiveMessage.mission_day.desc()))]


@router.post("/messages/{message_id}/{action}", response_model=MessageOut, summary="Mark a message read or dismissed")
def update_message(
    message_id: int,
    action: Literal["read", "dismiss"],
    user: User = Depends(require_astronaut),
    db: Session = Depends(get_db),
):
    message = db.get(ProactiveMessage, message_id)
    if message is None or message.astronaut_id != user.astronaut_id:
        raise HTTPException(404, "Message not found")
    message.status = "read" if action == "read" else "dismissed"
    db.commit()
    return MessageOut.model_validate(message, from_attributes=True)


@router.get("/preferences", response_model=PreferencesOut, summary="My preferences")
def get_preferences(user: User = Depends(require_astronaut), db: Session = Depends(get_db)):
    return preferences_out(db, user.astronaut_id)


@router.put("/preferences", response_model=PreferencesOut, summary="Turn proactive messages on or off")
def put_preferences(body: PreferencesIn, user: User = Depends(require_astronaut), db: Session = Depends(get_db)):
    pref = db.get(AstronautPreference, user.astronaut_id) or AstronautPreference(astronaut_id=user.astronaut_id)
    pref.proactive_messages = body.proactive_messages
    pref.updated_at = utcnow()
    db.add(pref)
    db.commit()
    return preferences_out(db, user.astronaut_id)


@router.post("/support", response_model=SupportOut, status_code=201, summary="Ask to talk to a person")
def request_support(body: SupportIn, user: User = Depends(require_astronaut), db: Session = Depends(get_db)):
    mission_id, day = mission_and_day(db)
    request = open_request(
        db, astronaut_id=user.astronaut_id, mission_id=mission_id, mission_day=day,
        reason="astronaut_request", topic=body.topic, priority="urgent" if body.urgent else "routine",
        message=body.message, dedupe=False,
    )
    db.commit()
    return support_out(request)


@router.get("/support", response_model=list[SupportOut], summary="My support requests")
def my_support(user: User = Depends(require_astronaut), db: Session = Depends(get_db)):
    stmt = select(SupportRequest).where(SupportRequest.astronaut_id == user.astronaut_id)
    return [support_out(r) for r in db.scalars(stmt.order_by(SupportRequest.id.desc()))]
