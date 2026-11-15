
"""Immutable medical briefing snapshots associated with support requests."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import UTCDateTime, utcnow


class MedicalBriefingSnapshot(Base):
    """One immutable packet per consultation request."""

    __tablename__ = "medical_briefing_snapshots"

    report_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("support_requests.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    packet: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
