
"""Authenticated API for Orbi's contextual, evidence-based health insights."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth.deps import assert_can_access, get_current_user
from app.db import get_db
from app.models import Astronaut, Mission, User
from app.services.orbi_service.guidance import (
    get_relevant_categories,
    select_supported_guidance,
)
from app.services.orbi_service.home_context import get_home_context
from app.services.orbi_service.insights import PAGE_METRICS, build_insight
from app.services.orbi_service.knowledge import retrieve_scientific_evidence
from app.services.orbi_service.narrative import generate_narrative

router = APIRouter(prefix="/api/v1/orbi", tags=["orbi"])


@router.get("/insights", summary="Daily Orbi insight for a health page")
def get_insight(
    page: str = Query(
        "home",
        description="home, sleep, mood, body, meals, exercise, radiation",
    ),
    mission_id: str = Query("SYN-LDM-01", alias="missionId"),
    astronaut_id: str | None = Query(None, alias="astronautId"),
    day: int | None = Query(None, ge=1, alias="missionDay"),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Return authorized health insights and grounded guidance.

    Parameters
    ----------
    page : str
        Requested frontend section.
    mission_id : str
        Mission identifier.
    astronaut_id : str or None
        Target astronaut, inferred from the authenticated user.
    day : int or None
        Requested mission day.
    session : Session
        Database session.
    user : User
        Authenticated caller.

    Returns
    -------
    dict
        Statistical insight, narrative, and supporting references.
    """
    if page not in PAGE_METRICS:
        raise HTTPException(422, "Unsupported insight page")

    target_id = astronaut_id or user.astronaut_id

    if not target_id:
        raise HTTPException(
            422,
            "astronautId is required for this account",
        )

    assert_can_access(user, target_id)

    mission = session.get(Mission, mission_id)

    if mission is None:
        raise HTTPException(404, "Mission not found")

    astronaut = session.get(Astronaut, target_id)

    if astronaut is None or astronaut.mission_id != mission.id:
        raise HTTPException(
            404,
            "Astronaut not found in this mission",
        )

    target_day = mission.last_day if day is None else day

    if not 1 <= target_day <= mission.last_day:
        raise HTTPException(
            422,
            "missionDay is outside the supported mission range",
        )

    insight = build_insight(
        session,
        mission,
        astronaut,
        page,
        target_day,
    )

    relevant_categories = None

    if page == "home":
        insight["homePatterns"] = get_home_context(
            session,
            mission_id=mission.id,
            astronaut_id=astronaut.id,
            mission_day=target_day,
        )

        relevant_categories = get_relevant_categories(insight)

    scientific_evidence = retrieve_scientific_evidence(
        session,
        insight,
    )

    supported_guidance = select_supported_guidance(
        page,
        scientific_evidence,
        relevant_categories=relevant_categories,
    )

    return generate_narrative(
        insight,
        scientific_evidence=scientific_evidence,
        supported_guidance=supported_guidance,
    )
