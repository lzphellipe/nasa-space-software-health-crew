
"""Retrieve existing health patterns for Orbi's home overview."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PatternDetection
from app.services.patterns import PATTERN_LABELS

SEVERITY_ORDER = {
    "high": 3,
    "medium": 2,
    "low": 1,
}


def get_home_context(
    session: Session,
    mission_id: str,
    astronaut_id: str,
    mission_day: int,
    limit: int = 4,
) -> list[dict]:
    """Retrieve detected health patterns for the home overview.

    Parameters
    ----------
    session : Session
        Active database session.
    mission_id : str
        Mission identifier.
    astronaut_id : str
        Authorized astronaut identifier.
    mission_day : int
        Requested mission day.
    limit : int
        Maximum number of patterns to return.

    Returns
    -------
    list[dict]
        Existing health patterns, ordered by severity.
    """
    if limit <= 0:
        return []

    statement = (
        select(PatternDetection)
        .where(
            PatternDetection.mission_id == mission_id,
            PatternDetection.astronaut_id == astronaut_id,
            PatternDetection.as_of_day == mission_day,
        )
    )

    patterns = session.scalars(statement).all()

    patterns = sorted(
        patterns,
        key=lambda pattern: (
            -SEVERITY_ORDER.get(pattern.severity, 0),
            pattern.pattern,
        ),
    )

    return [
        {
            "pattern": pattern.pattern,
            "label": PATTERN_LABELS.get(
                pattern.pattern,
                pattern.pattern,
            ),
            "severity": pattern.severity,
            "summary": pattern.summary,
            "evidence": pattern.evidence,
            "windowStart": pattern.window_start,
            "windowEnd": pattern.window_end,
        }
        for pattern in patterns[:limit]
    ]
