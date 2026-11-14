"""Relational model.

Raw OSDR rows are wide (one row per sample, three columns per analyte). They are
stored here in long form: one Measurement per (sample, analyte), with the lab
reference range, the flag against it and the deviation from the crew member's
own pre-flight baseline.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
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


# --- Synthetic long-duration mission (daily vital signs) ------------------
# OSDR has no daily vitals for any crew, so these tables hold a generated
# dataset (see app/synthetic). Every row belongs to a mission flagged synthetic.


class Mission(Base):
    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    launch_date: Mapped[date] = mapped_column(Date)
    first_day: Mapped[int] = mapped_column(Integer)
    last_day: Mapped[int] = mapped_column(Integer)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Astronaut(Base):
    __tablename__ = "astronauts"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id", ondelete="CASCADE"))
    display_name: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(60))


class DailyRecord(Base):
    __tablename__ = "daily_records"
    __table_args__ = (
        UniqueConstraint("astronaut_id", "mission_day", name="uq_daily_astronaut_day"),
        Index("ix_daily_mission_day", "mission_id", "mission_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("astronauts.id", ondelete="CASCADE"))
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id", ondelete="CASCADE"))
    mission_day: Mapped[int] = mapped_column(Integer)
    date: Mapped[date] = mapped_column(Date)
    phase: Mapped[str] = mapped_column(String(20))  # preflight | inflight

    # Required daily metrics
    systolic_bp: Mapped[float] = mapped_column(Float)
    diastolic_bp: Mapped[float] = mapped_column(Float)
    heart_rate: Mapped[float] = mapped_column(Float)
    blood_glucose: Mapped[float] = mapped_column(Float)
    body_temperature: Mapped[float] = mapped_column(Float)
    sleep_hours: Mapped[float] = mapped_column(Float)
    mood: Mapped[int] = mapped_column(Integer)
    # Optional metrics (null on days without a measurement)
    bone_density: Mapped[float | None] = mapped_column(Float, nullable=True)
    calorie_intake: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_intake: Mapped[float | None] = mapped_column(Float, nullable=True)
    white_blood_cells: Mapped[float | None] = mapped_column(Float, nullable=True)

    metric_status: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20))  # worst metric status of the day


class VitalBaseline(Base):
    __tablename__ = "vital_baselines"

    astronaut_id: Mapped[str] = mapped_column(ForeignKey("astronauts.id", ondelete="CASCADE"), primary_key=True)
    metric: Mapped[str] = mapped_column(String(40), primary_key=True)
    n: Mapped[int] = mapped_column(Integer)
    mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    sd: Mapped[float | None] = mapped_column(Float, nullable=True)
    sd_effective: Mapped[float | None] = mapped_column(Float, nullable=True)


class SyntheticEvent(Base):
    """Ground truth injected by the generator, for evaluating the AI agent."""

    __tablename__ = "synthetic_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id", ondelete="CASCADE"))
    astronaut_id: Mapped[str] = mapped_column(ForeignKey("astronauts.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(60))
    start_day: Mapped[int] = mapped_column(Integer)
    end_day: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    affected_metrics: Mapped[list] = mapped_column(JSON, default=list)


# --- AGBRESA bed rest study (real data, Earth analog) ----------------------
# Healthy volunteers in 60-day head-down-tilt bed rest; not astronauts.
# The study and its two files are recorded in `studies` / `source_files`.


class AnalogSubject(Base):
    __tablename__ = "analog_subjects"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # "AGB-A"
    study_id: Mapped[str] = mapped_column(ForeignKey("studies.id"))
    code: Mapped[str] = mapped_column(String(10))  # "A" (S2) / "0A" (S1) in the source
    group: Mapped[str] = mapped_column(String(20))  # intermittent | continuous | control
    sex: Mapped[str] = mapped_column(String(10))
    age_group: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign: Mapped[str | None] = mapped_column(String(10), nullable=True)


class AnalogObservation(Base):
    __tablename__ = "analog_observations"
    __table_args__ = (
        UniqueConstraint("subject_id", "session", "metric", "moment", name="uq_analog_obs"),
        Index("ix_analog_obs_metric", "metric", "moment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("analog_subjects.id", ondelete="CASCADE"))
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id", ondelete="CASCADE"))
    session: Mapped[str] = mapped_column(String(20))  # "HDT1", "HDT29", ...
    session_index: Mapped[int] = mapped_column(Integer)  # 1 begin, 2 middle, 3 end
    bed_rest_day: Mapped[int] = mapped_column(Integer)
    month: Mapped[str | None] = mapped_column(String(10), nullable=True)
    metric: Mapped[str] = mapped_column(String(40))
    moment: Mapped[str] = mapped_column(String(10))  # pre | during | post | single
    raw_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)  # null when implausible
    quality: Mapped[str] = mapped_column(String(20))  # ok | implausible
    quality_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    delta_vs_first: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_vs_first: Mapped[float | None] = mapped_column(Float, nullable=True)


# --- Users, check-ins, patterns, proactive messages, human support ----------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # astronaut | medical | admin
    # Astronaut this account belongs to (role astronaut only). Not a foreign key:
    # the synthetic mission can be regenerated without touching accounts.
    astronaut_id: Mapped[str | None] = mapped_column(String(10), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserRefreshToken(Base):
    __tablename__ = "user_refresh_tokens"
    __table_args__ = (
        Index("ix_user_refresh_tokens_user_id", "user_id"),
        Index("ix_user_refresh_tokens_family_id", "family_id"),
        Index("ix_user_refresh_tokens_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AstronautPreference(Base):
    __tablename__ = "astronaut_preferences"

    astronaut_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    proactive_messages: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckIn(Base):
    __tablename__ = "checkins"
    __table_args__ = (UniqueConstraint("astronaut_id", "mission_day", name="uq_checkin_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    astronaut_id: Mapped[str] = mapped_column(String(10), index=True)
    mission_id: Mapped[str] = mapped_column(String(30))
    mission_day: Mapped[int] = mapped_column(Integer)
    stress: Mapped[int] = mapped_column(Integer)  # 1 very low … 5 very high
    fatigue: Mapped[int] = mapped_column(Integer)  # 1 very low … 5 very high
    symptoms: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20))  # app | synthetic
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PatternDetection(Base):
    __tablename__ = "pattern_detections"
    __table_args__ = (
        UniqueConstraint("astronaut_id", "as_of_day", "pattern", "detail", name="uq_detection"),
        Index("ix_detection_astronaut_day", "astronaut_id", "as_of_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    astronaut_id: Mapped[str] = mapped_column(String(10))
    mission_id: Mapped[str] = mapped_column(String(30))
    as_of_day: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[int] = mapped_column(Integer)
    window_end: Mapped[int] = mapped_column(Integer)
    pattern: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(40), default="")
    severity: Mapped[str] = mapped_column(String(10))  # low | medium | high
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProactiveMessage(Base):
    __tablename__ = "proactive_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    astronaut_id: Mapped[str] = mapped_column(String(10), index=True)
    mission_id: Mapped[str] = mapped_column(String(30))
    mission_day: Mapped[int] = mapped_column(Integer)
    patterns: Mapped[list] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="unread")  # unread | read | dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SupportRequest(Base):
    __tablename__ = "support_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    astronaut_id: Mapped[str] = mapped_column(String(10), index=True)
    mission_id: Mapped[str] = mapped_column(String(30))
    mission_day: Mapped[int] = mapped_column(Integer)
    # astronaut_request | urgent_symptom | pattern
    reason: Mapped[str] = mapped_column(String(30))
    detail: Mapped[str] = mapped_column(String(60), default="")
    topic: Mapped[str] = mapped_column(String(30))  # medical | psychological | family | operational | other
    priority: Mapped[str] = mapped_column(String(10))  # routine | urgent
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | acknowledged | resolved
    assigned_to: Mapped[str | None] = mapped_column(String(60), nullable=True)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)  # shown to the astronaut
    care_notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # care team only
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    file_name: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    pages: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))  # bundled | upload
    uploaded_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    chunks: Mapped[list[KnowledgeChunk]] = relationship(back_populates="document", cascade="all, delete-orphan")


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"))
    page: Mapped[int] = mapped_column(Integer)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")
