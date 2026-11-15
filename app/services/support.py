"""Human support: requests to the care team (flight surgeon, psychologist, family support)."""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SupportRequest, utcnow

log = logging.getLogger(__name__)

TOPICS = {
    "medical": "Flight surgeon",
    "psychological": "Behavioral health / psychologist",
    "family": "Family support",
    "operational": "Mission operations",
    "other": "Care team",
}
# Patterns that open a request to the care team on their own (no message to the astronaut).
PATTERN_TOPICS = {
    "fever": "medical",
    "rising_blood_pressure": "medical",
    "elevated_glucose": "medical",
    "wellbeing_cluster": "psychological",
    "low_mood": "psychological",
}


def notify(request: SupportRequest) -> bool:
    """POST the request to SUPPORT_WEBHOOK_URL (e.g. a Slack or Teams incoming webhook)."""
    url = get_settings().support_webhook_url
    if not url:
        return False
    text = (
        f"[{request.priority.upper()}] Support request #{request.id} from {request.astronaut_id} "
        f"(mission day {request.mission_day}, {TOPICS.get(request.topic, request.topic)}): {request.message}"
    )
    try:
        httpx.post(url, json={"text": text, "request": {
            "id": request.id, "astronautId": request.astronaut_id, "priority": request.priority,
            "topic": request.topic, "reason": request.reason, "missionDay": request.mission_day,
        }}, timeout=5)
        return True
    except httpx.HTTPError as exc:
        log.warning("Support webhook failed for request %s: %s", request.id, exc)
        return False


def open_request(
    session: Session,
    *,
    astronaut_id: str,
    mission_id: str,
    mission_day: int,
    reason: str,
    topic: str,
    priority: str,
    message: str,
    detail: str = "",
    dedupe: bool = True,
    send: bool = True,
) -> SupportRequest:
    """Open a request; automatic ones are not duplicated while a matching one is still open."""
    if dedupe:
        existing = session.scalar(
            select(SupportRequest).where(
                SupportRequest.astronaut_id == astronaut_id,
                SupportRequest.reason == reason,
                SupportRequest.detail == detail,
                SupportRequest.status != "resolved",
            )
        )
        if existing is not None:
            if priority == "urgent" and existing.priority != "urgent":
                existing.priority = "urgent"
                existing.updated_at = utcnow()
            return existing
    request = SupportRequest(
        astronaut_id=astronaut_id, mission_id=mission_id, mission_day=mission_day, reason=reason,
        detail=detail, topic=topic, priority=priority, message=message,
    )
    session.add(request)
    session.flush()
    if send:
        request.notified = notify(request)
    return request
