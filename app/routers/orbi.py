
"""Authenticated Orbi insights and immutable consultation handoff."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import (
    assert_can_access, get_current_user, require_astronaut, require_care_team,
)
from app.db import get_db
from app.models import Astronaut, Mission, SupportRequest, User
from app.report.report_models import MedicalBriefingSnapshot
from app.services.orbi_service.briefing import (
    create_snapshot, decode_packet, to_fhir_bundle, verified_packet,
)
from app.services.orbi_service.guidance import (
    get_relevant_categories, select_supported_guidance,
)
from app.services.orbi_service.home_context import get_home_context
from app.services.orbi_service.insights import PAGE_METRICS, build_insight
from app.services.orbi_service.knowledge import retrieve_scientific_evidence
from app.services.orbi_service.narrative import generate_narrative
from app.services.support import open_request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/orbi", tags=["orbi"])


@router.get("/insights", summary="Daily Orbi insight for a health page")
def get_insight(
    page: str = Query("home"),
    mission_id: str = Query("SYN-LDM-01", alias="missionId"),
    astronaut_id: str | None = Query(None, alias="astronautId"),
    day: int | None = Query(None, ge=1, alias="missionDay"),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Return a personalized insight with grounded guidance."""
    if page not in PAGE_METRICS:
        raise HTTPException(422, "Unsupported insight page")
    target_id = astronaut_id or user.astronaut_id
    if not target_id:
        raise HTTPException(422, "astronautId is required for this account")
    assert_can_access(user, target_id)
    mission = session.get(Mission, mission_id)
    if mission is None:
        raise HTTPException(404, "Mission not found")
    astronaut = session.get(Astronaut, target_id)
    if astronaut is None or astronaut.mission_id != mission.id:
        raise HTTPException(404, "Astronaut not found in this mission")
    target_day = mission.last_day if day is None else day
    if not 1 <= target_day <= mission.last_day:
        raise HTTPException(422, "missionDay is outside the supported range")
    insight = build_insight(session, mission, astronaut, page, target_day)
    if insight.get("status") != "available":
        return {
            **insight,
            "headline": None,
            "insight": insight.get("message"),
            "suggestedAction": None,
            "actionKind": None,
            "sources": [],
            "messageSource": "data_status",
            "aiGenerated": False,
            "consultationOffer": {
                "available": False, "message": None,
                "triggerMetrics": [], "requiresConfirmation": True,
            },
        }
    categories = None
    if page == "home":
        insight["homePatterns"] = get_home_context(
            session, mission_id=mission.id, astronaut_id=astronaut.id,
            mission_day=target_day,
        )
        categories = get_relevant_categories(insight)
    evidence = retrieve_scientific_evidence(session, insight)
    guidance = select_supported_guidance(
        page, evidence, relevant_categories=categories
    )
    response = generate_narrative(
        insight, scientific_evidence=evidence, supported_guidance=guidance
    )
    flagged = [
        metric for metric in insight.get("metrics", [])
        if metric.get("status") == "available"
        and metric.get("screeningStatus") in ("watch", "out_of_range")
        and (metric.get("persistence") or {}).get("flaggedCount", 0) >= 3
    ]
    response["consultationOffer"] = {
        "available": bool(flagged),
        "message": (
            "Several measurements have remained outside your usual screening "
            "pattern. Would you like to request a private medical consultation?"
            if flagged else None
        ),
        "triggerMetrics": [metric["metric"] for metric in flagged],
        "basis": "illustrative_demo_rule_not_nasa_clinical_criteria",
        "requiresConfirmation": True,
    }
    return response


class ConsultationRequestIn(BaseModel):
    """Explicit consent to create a consultation and freeze its briefing."""

    mission_id: str = Field("SYN-LDM-01", alias="missionId")
    mission_day: int | None = Field(None, alias="missionDay", ge=1)
    confirmed: bool
    message: str = Field(
        "I would like a private medical consultation.", max_length=300
    )


def _get_consultation(
    session: Session, request_id: int
) -> tuple[SupportRequest, Mission, Astronaut, MedicalBriefingSnapshot]:
    """Load an existing consultation and its saved briefing."""
    consultation = session.get(SupportRequest, request_id)
    if consultation is None or consultation.reason != "orbi_consultation":
        raise HTTPException(404, "Consultation not found")
    mission = session.get(Mission, consultation.mission_id)
    astronaut = session.get(Astronaut, consultation.astronaut_id)
    if mission is None or astronaut is None or astronaut.mission_id != mission.id:
        raise HTTPException(404, "Consultation mission not found")
    snapshot = session.scalar(
        select(MedicalBriefingSnapshot).where(
            MedicalBriefingSnapshot.request_id == request_id
        )
    )
    if snapshot is None:
        raise HTTPException(409, "Consultation has no stored medical briefing")
    return consultation, mission, astronaut, snapshot


def _get_verified_packet(snapshot: MedicalBriefingSnapshot) -> bytes:
    """Check stored bytes before sending or decoding them."""
    try:
        return verified_packet(snapshot)
    except ValueError as exc:
        logger.error("Stored briefing integrity failure: %s", snapshot.report_id)
        raise HTTPException(409, "Medical briefing integrity check failed") from exc


@router.post("/consultations", status_code=201)
def request_consultation(
    body: ConsultationRequestIn,
    session: Session = Depends(get_db),
    user: User = Depends(require_astronaut),
) -> dict:
    """Atomically create a support request and its immutable AI briefing."""
    if not body.confirmed:
        raise HTTPException(422, "Explicit confirmation is required")
    mission = session.get(Mission, body.mission_id)
    astronaut = session.get(Astronaut, user.astronaut_id)
    if mission is None or astronaut is None or astronaut.mission_id != mission.id:
        raise HTTPException(404, "Mission or astronaut not found")
    day = body.mission_day if body.mission_day is not None else mission.last_day
    if not 1 <= day <= mission.last_day:
        raise HTTPException(422, "Invalid mission day")
    try:
        consultation = open_request(
            session, astronaut_id=astronaut.id, mission_id=mission.id,
            mission_day=day, reason="orbi_consultation", topic="medical",
            priority="routine", message=body.message,
            dedupe=False, send=False,
        )
        snapshot = create_snapshot(
            session, mission, astronaut, day, consultation
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("Unable to create consultation briefing")
        raise HTTPException(
            503, "Unable to prepare the consultation briefing"
        ) from exc
    return {
        "requestId": consultation.id, "reportId": snapshot.report_id,
        "reportVersion": snapshot.version,
        "generatedAt": snapshot.created_at.isoformat(),
        "sha256": snapshot.sha256,
        "status": consultation.status, "scheduled": False,
        "message": "Consultation requested and medical briefing saved.",
        "packetUrl": f"/api/v1/orbi/consultations/{consultation.id}/packet",
    }


@router.get("/consultations/{request_id}/packet")
def download_consultation_packet(
    request_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Return the exact bytes frozen when the astronaut confirmed."""
    _, _, astronaut, snapshot = _get_consultation(session, request_id)
    assert_can_access(user, astronaut.id)
    packet = _get_verified_packet(snapshot)
    return Response(
        content=packet, media_type="application/gzip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="orbi-briefing-{snapshot.report_id}.json.gz"'
            ),
            "Cache-Control": "no-store", "X-Report-SHA256": snapshot.sha256,
        },
    )


@router.get("/ground/consultations/{request_id}/fhir")
def ground_consultation_fhir(
    request_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(require_care_team),
) -> dict:
    """Convert the stored, integrity-checked packet to FHIR R4 JSON."""
    _, _, _, snapshot = _get_consultation(session, request_id)
    return to_fhir_bundle(decode_packet(_get_verified_packet(snapshot)))


@router.post("/ground/consultations/{request_id}/receive")
async def receive_consultation_packet(
    request_id: int,
    request_body: Request,
    session: Session = Depends(get_db),
    user: User = Depends(require_care_team),
) -> dict:
    """Verify received binary matches the saved snapshot before conversion."""
    consultation, mission, astronaut, snapshot = _get_consultation(
        session, request_id
    )
    if request_body.headers.get("content-type", "").split(";")[0] != "application/gzip":
        raise HTTPException(415, "Expected application/gzip")
    payload = await request_body.body()
    expected = _get_verified_packet(snapshot)
    if payload != expected:
        raise HTTPException(422, "Received packet differs from stored briefing")
    try:
        briefing = decode_packet(payload)
    except (ValueError, OSError, EOFError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Invalid medical briefing packet") from exc
    if (
        briefing.get("reportId") != snapshot.report_id
        or briefing.get("requestId") != consultation.id
        or briefing.get("missionId") != mission.id
        or briefing.get("astronautId") != astronaut.id
        or briefing.get("missionDay") != consultation.mission_day
    ):
        raise HTTPException(422, "Briefing metadata does not match consultation")
    return to_fhir_bundle(briefing)
