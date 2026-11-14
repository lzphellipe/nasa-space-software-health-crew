"""Relational model.

Raw OSDR rows are wide (one row per sample, three columns per analyte). They are
stored here in long form: one Measurement per (sample, analyte), with the lab
reference range, the flag against it and the deviation from the crew member's
own pre-flight baseline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Study(Base):
    __tablename__ = "studies"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # "OSD-569"
    title: Mapped[str] = mapped_column(Text)
    mission: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(300))

    files: Mapped[list[SourceFile]] = relationship(back_populates="study")


class SourceFile(Base):
    __tablename__ = "source_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_key: Mapped[str] = mapped_column(String(40), unique=True)  # "cbc", "cmp", "cardio"
    study_id: Mapped[str] = mapped_column(ForeignKey("studies.id"))
    file_name: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(600))
    origin: Mapped[str] = mapped_column(String(20))  # "remote" | "bundled"
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    row_count: Mapped[int] = mapped_column(Integer)
    measurement_count: Mapped[int] = mapped_column(Integer)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    study: Mapped[Study] = relationship(back_populates="files")


class CrewMember(Base):
    __tablename__ = "crew_members"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)  # "C001" (de-identified in OSDR)
    display_name: Mapped[str] = mapped_column(String(60))
    mission: Mapped[str] = mapped_column(String(80))


class Timepoint(Base):
    __tablename__ = "timepoints"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)  # "L-92", "R+1"
    phase: Mapped[str] = mapped_column(String(20))  # preflight | inflight | postflight
    offset_days: Mapped[int] = mapped_column(Integer)  # relative to launch (L) or return (R)
    mission_day: Mapped[int] = mapped_column(Integer)  # days from launch; sorts all timepoints
    label: Mapped[str] = mapped_column(String(80))


class Analyte(Base):
    __tablename__ = "analytes"

    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    panel: Mapped[str] = mapped_column(String(20))
    panel_name: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))
    unit: Mapped[str] = mapped_column(String(60))
    unit_raw: Mapped[str | None] = mapped_column(String(120), nullable=True)
    unit_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_reference_range: Mapped[bool] = mapped_column(Boolean, default=False)


class Sample(Base):
    __tablename__ = "samples"
    # The CMP and the multiplex panel were run on the same serum sample, so the
    # OSDR sample name repeats across panels.
    __table_args__ = (UniqueConstraint("sample_name", "panel", name="uq_sample_name_panel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_name: Mapped[str] = mapped_column(String(120))
    crew_id: Mapped[str] = mapped_column(ForeignKey("crew_members.id"))
    timepoint_code: Mapped[str] = mapped_column(ForeignKey("timepoints.code"))
    specimen: Mapped[str] = mapped_column(String(40))
    panel: Mapped[str] = mapped_column(String(20))
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id", ondelete="CASCADE"))

    measurements: Mapped[list[Measurement]] = relationship(
        back_populates="sample", cascade="all, delete-orphan"
    )


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (
        UniqueConstraint("sample_id", "analyte_code", name="uq_measurement_sample_analyte"),
        Index("ix_measurement_crew_analyte", "crew_id", "analyte_code"),
        Index("ix_measurement_timepoint", "timepoint_code"),
        Index("ix_measurement_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id", ondelete="CASCADE"))
    # Denormalized from Sample so the common queries need no join.
    crew_id: Mapped[str] = mapped_column(ForeignKey("crew_members.id"))
    timepoint_code: Mapped[str] = mapped_column(ForeignKey("timepoints.code"))
    analyte_code: Mapped[str] = mapped_column(ForeignKey("analytes.code"))

    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    ref_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    ref_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Multiplex panels report a "percent" column next to the concentration; kept as reported.
    relative_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    # low | normal | high | no_reference | missing
    flag: Mapped[str] = mapped_column(String(20))

    # Derived from the crew member's own pre-flight samples.
    baseline_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta_abs: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    z_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # out_of_range | watch | normal | no_data
    status: Mapped[str] = mapped_column(String(20), default="normal")

    sample: Mapped[Sample] = relationship(back_populates="measurements")
    analyte: Mapped[Analyte] = relationship()
    timepoint: Mapped[Timepoint] = relationship()


class BaselineStat(Base):
    __tablename__ = "baseline_stats"

    crew_id: Mapped[str] = mapped_column(ForeignKey("crew_members.id"), primary_key=True)
    analyte_code: Mapped[str] = mapped_column(ForeignKey("analytes.code"), primary_key=True)
    n: Mapped[int] = mapped_column(Integer)
    mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    sd: Mapped[float | None] = mapped_column(Float, nullable=True)
    sd_effective: Mapped[float | None] = mapped_column(Float, nullable=True)
    min: Mapped[float | None] = mapped_column(Float, nullable=True)
    max: Mapped[float | None] = mapped_column(Float, nullable=True)


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_source: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | success | failed
    files: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
