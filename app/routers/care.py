"""Care-team endpoints (flight surgeon, psychologist): whole crew, support queue, detection runs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_care_team
from app.db import get_db
from app.models import CheckIn, PatternDetection, ProactiveMessage, SupportRequest, User, utcnow
from app.routers.me import CheckInOut, MessageOut, PatternOut, SupportOut, pattern_out, support_out
from app.schemas import APIModel
from app.services.monitoring import current_day, current_mission, run_cycle

router = APIRouter(prefix="/api/v1/care", tags=["care team"], dependencies=[Depends(require_care_team)])

PRIORITY_ORDER = {"urgent": 0, "routine": 1}
STATUS_ORDER = {"open": 0, "acknowledged": 1, "resolved": 2}


class CareSupportOut(SupportOut):
    detail: str
    assigned_to: str | None
    care_notes: str | None
    notified: bool


class SupportUpdate(APIModel):
    status: Literal["open", "acknowledged", "resolved"] | None = None
    assigned_to: str | None = Field(None, max_length=60)
    response: str | None = Field(None, max_length=4000, description="Shown to the astronaut")
    care_notes: str | None = Field(None, max_length=8000, description="Care team only")


class CycleResult(APIModel):
    days: int
    detections: int
    messages: int


class CareMessageOut(MessageOut):
    astronaut_id: str


def care_support_out(r: SupportRequest) -> CareSupportOut:
    return CareSupportOut(
        **support_out(r).model_dump(), detail=r.detail, assigned_to=r.assigned_to,
        care_notes=r.care_notes, notified=r.notified,
    )


@router.get("/support", response_model=list[CareSupportOut], summary="Support queue (urgent and open first)")
def support_queue(
    status: str | None = Query(None, description="Comma-separated: open,acknowledged,resolved"),
    astronaut_id: str | None = Query(None, alias="astronautId"),
    db: Session = Depends(get_db),
):
    stmt = select(SupportRequest)
    if status:
        stmt = stmt.where(SupportRequest.status.in_(status.split(",")))
    if astronaut_id:
        stmt = stmt.where(SupportRequest.astronaut_id == astronaut_id.upper())
    items = sorted(db.scalars(stmt), key=lambda r: (STATUS_ORDER[r.status], PRIORITY_ORDER[r.priority], -r.id))
    return [care_support_out(r) for r in items]


@router.patch("/support/{request_id}", response_model=CareSupportOut, summary="Acknowledge, answer or resolve a request")
def update_support(request_id: int, body: SupportUpdate, user: User = Depends(require_care_team), db: Session = Depends(get_db)):
    request = db.get(SupportRequest, request_id)
    if request is None:
        raise HTTPException(404, "Support request not found")
    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(request, field_name, value)
    if body.status == "acknowledged" and not request.assigned_to:
        request.assigned_to = user.username
    request.updated_at = utcnow()
    db.commit()
    return care_support_out(request)


@router.get("/checkins", response_model=list[CheckInOut], summary="Check-ins of the crew")
def crew_checkins(
    astronaut_id: str | None = Query(None, alias="astronautId"),
    from_day: int | None = Query(None, alias="fromDay"),
    to_day: int | None = Query(None, alias="toDay"),
    db: Session = Depends(get_db),
):
    stmt = select(CheckIn)
    if astronaut_id:
        stmt = stmt.where(CheckIn.astronaut_id == astronaut_id.upper())
    if from_day is not None:
        stmt = stmt.where(CheckIn.mission_day >= from_day)
    if to_day is not None:
        stmt = stmt.where(CheckIn.mission_day <= to_day)
    stmt = stmt.order_by(CheckIn.astronaut_id, CheckIn.mission_day)
    return [CheckInOut.model_validate(c, from_attributes=True) for c in db.scalars(stmt)]


@router.get("/patterns", response_model=list[PatternOut], summary="Detected patterns of the crew")
def crew_patterns(
    day: int | None = Query(None, description="One day (default: current mission day)"),
    from_day: int | None = Query(None, alias="fromDay", description="Use with toDay for a range instead of one day"),
    to_day: int | None = Query(None, alias="toDay"),
    astronaut_id: str | None = Query(None, alias="astronautId"),
    min_severity: Literal["low", "medium", "high"] = Query("low", alias="minSeverity"),
    db: Session = Depends(get_db),
):
    stmt = select(PatternDetection)
    if from_day is not None or to_day is not None:
        if from_day is not None:
            stmt = stmt.where(PatternDetection.as_of_day >= from_day)
        if to_day is not None:
            stmt = stmt.where(PatternDetection.as_of_day <= to_day)
    else:
        stmt = stmt.where(PatternDetection.as_of_day == (current_day(db) if day is None else day))
    if astronaut_id:
        stmt = stmt.where(PatternDetection.astronaut_id == astronaut_id.upper())
    allowed = {"low": ["low", "medium", "high"], "medium": ["medium", "high"], "high": ["high"]}[min_severity]
    stmt = stmt.where(PatternDetection.severity.in_(allowed))
    stmt = stmt.order_by(PatternDetection.as_of_day, PatternDetection.astronaut_id, PatternDetection.pattern)
    return [pattern_out(d) for d in db.scalars(stmt)]


@router.get("/messages", response_model=list[CareMessageOut], summary="Proactive messages sent to the crew")
def crew_messages(astronaut_id: str | None = Query(None, alias="astronautId"), db: Session = Depends(get_db)):
    stmt = select(ProactiveMessage)
    if astronaut_id:
        stmt = stmt.where(ProactiveMessage.astronaut_id == astronaut_id.upper())
    stmt = stmt.order_by(ProactiveMessage.mission_day, ProactiveMessage.astronaut_id)
    return [CareMessageOut.model_validate(m, from_attributes=True) for m in db.scalars(stmt)]


@router.post("/patterns/run", response_model=CycleResult, summary="Run detection, messages and routing for one mission day now")
def run_now(day: int | None = Query(None, description="Default: current mission day"), db: Session = Depends(get_db)):
    mission = current_mission(db)
    if mission is None:
        raise HTTPException(503, "No mission loaded")
    day = current_day(db) if day is None else day
    if not mission.first_day <= day <= mission.last_day:
        raise HTTPException(422, f"day must be between {mission.first_day} and {mission.last_day}")
    return run_cycle(db, [day])
