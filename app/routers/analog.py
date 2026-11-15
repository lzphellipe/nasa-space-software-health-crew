"""AGBRESA bed rest study: real heart rate, blood pressure, sleepiness, motion sickness and mood."""

from __future__ import annotations

import csv
import io
import statistics
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analog import agbresa
from app.db import get_db
from app.models import AnalogObservation, AnalogSubject, SourceFile
from app.provenance import REAL
from app.schemas import APIModel, SourceFileOut
from app.services.analytics import STATUS_ORDER

router = APIRouter(prefix="/api/v1/analog/agbresa", tags=["AGBRESA bed rest (real data, Earth analog)"])

NOTICE = (
    "Real data from healthy volunteers in a 60-day head-down-tilt bed rest study on Earth "
    "(an analog of some effects of microgravity), not astronauts in space. Not medical advice."
)
# For the worst-status summary; "invalid" and "no_reference" never count as alerts.
ANALOG_ORDER = {**STATUS_ORDER, "no_reference": 0, "invalid": 0}


# --- response models ------------------------------------------------------------


class AnalogMetricOut(APIModel):
    code: str
    label: str
    unit: str
    source: str
    description: str
    thresholds: dict[str, float]
    threshold_moments: list[str]


class GroupOut(APIModel):
    group: str
    subjects: int
    men: int
    women: int


class AnalogStudyOut(APIModel):
    id: str
    title: str
    doi: str
    url: str
    context: str
    design: str
    notice: str
    groups: list[GroupOut]
    moments: dict[str, str]
    metrics: list[AnalogMetricOut]
    files: list[SourceFileOut]
    invalid_values: int
    provenance: str = REAL


class SessionRef(APIModel):
    session: str
    session_index: int
    phase: str
    bed_rest_day: int


class AnalogSubjectOut(APIModel):
    id: str
    code: str
    group: str
    sex: str
    age_group: int | None
    campaign: str | None
    sessions: list[SessionRef]
    latest_status: str


class AnalogObservationOut(APIModel):
    subject_id: str
    session: str
    session_index: int
    phase: str
    bed_rest_day: int
    month: str | None
    metric: str
    label: str
    unit: str
    moment: str
    value: float | None
    raw_value: float | None
    quality: str
    quality_note: str | None
    status: str
    delta_vs_first: float | None
    pct_vs_first: float | None
    provenance: str = REAL


class SessionDetail(APIModel):
    session: str
    session_index: int
    phase: str
    bed_rest_day: int
    month: str | None
    status: str
    values: dict[str, dict[str, float | None]]
    metric_status: dict[str, dict[str, str]]
    pct_vs_first: dict[str, dict[str, float | None]]


class AnalogSubjectDetail(APIModel):
    notice: str
    subject: AnalogSubjectOut
    sessions: list[SessionDetail]
    provenance: str = REAL


class GroupPoint(APIModel):
    session_index: int
    phase: str
    bed_rest_days: list[int]
    n: int
    mean: float | None
    sd: float | None
    min: float | None
    max: float | None


class GroupSeries(APIModel):
    group: str
    points: list[GroupPoint]


class GroupComparison(APIModel):
    metric: AnalogMetricOut
    moment: str
    groups: list[GroupSeries]


# --- helpers ----------------------------------------------------------------------


def metric_out(m: agbresa.AnalogMetric) -> AnalogMetricOut:
    return AnalogMetricOut(
        code=m.code, label=m.label, unit=m.unit, source=m.source, description=m.description,
        thresholds=m.thresholds(), threshold_moments=list(m.threshold_moments),
    )


def obs_out(o: AnalogObservation) -> AnalogObservationOut:
    m = agbresa.METRICS_BY_CODE[o.metric]
    return AnalogObservationOut(
        subject_id=o.subject_id, session=o.session, session_index=o.session_index,
        phase=agbresa.SESSION_PHASES.get(o.session_index, "other"), bed_rest_day=o.bed_rest_day,
        month=o.month, metric=o.metric, label=m.label, unit=m.unit, moment=o.moment, value=o.value,
        raw_value=o.raw_value, quality=o.quality, quality_note=o.quality_note, status=o.status,
        delta_vs_first=o.delta_vs_first, pct_vs_first=o.pct_vs_first,
    )


def worst(statuses) -> str:
    result = "no_data"
    for s in statuses:
        if ANALOG_ORDER.get(s, 0) > ANALOG_ORDER[result]:
            result = s
    if result == "no_data" and any(s == "no_reference" for s in statuses):
        return "no_reference"
    return result


def sessions_of(observations: list[AnalogObservation]) -> list[SessionRef]:
    seen = {}
    for o in observations:
        seen[(o.bed_rest_day, o.session)] = SessionRef(
            session=o.session, session_index=o.session_index,
            phase=agbresa.SESSION_PHASES.get(o.session_index, "other"), bed_rest_day=o.bed_rest_day,
        )
    return [seen[k] for k in sorted(seen)]


def subject_out(subject: AnalogSubject, observations: list[AnalogObservation]) -> AnalogSubjectOut:
    sessions = sessions_of(observations)
    last_day = sessions[-1].bed_rest_day if sessions else None
    latest = [o.status for o in observations if o.bed_rest_day == last_day]
    return AnalogSubjectOut(
        id=subject.id, code=subject.code, group=subject.group, sex=subject.sex, age_group=subject.age_group,
        campaign=subject.campaign, sessions=sessions, latest_status=worst(latest),
    )


def observations_for(db: Session, subject_id: str | None = None) -> list[AnalogObservation]:
    stmt = select(AnalogObservation)
    if subject_id:
        stmt = stmt.where(AnalogObservation.subject_id == subject_id)
    return list(db.scalars(stmt.order_by(AnalogObservation.subject_id, AnalogObservation.bed_rest_day)))


def subject_or_404(db: Session, subject_id: str) -> AnalogSubject:
    sid = subject_id.upper()
    subject = db.get(AnalogSubject, sid if sid.startswith("AGB-") else f"AGB-{sid}")
    if subject is None:
        raise HTTPException(404, f"Unknown AGBRESA subject {subject_id!r}")
    return subject


# --- endpoints ----------------------------------------------------------------------


@router.get("", response_model=AnalogStudyOut, summary="Study description, groups, metrics and source files")
def study(db: Session = Depends(get_db)):
    subjects = db.scalars(select(AnalogSubject)).all()
    if not subjects:
        raise HTTPException(503, "AGBRESA data not ingested yet. POST /api/v1/ingest first.")
    by_group = defaultdict(list)
    for s in subjects:
        by_group[s.group].append(s)
    files = db.scalars(select(SourceFile).where(SourceFile.study_id == agbresa.STUDY_ID).order_by(SourceFile.dataset_key))
    invalid = len(db.scalars(select(AnalogObservation.id).where(AnalogObservation.quality != "ok")).all())
    return AnalogStudyOut(
        id=agbresa.STUDY_ID, title=agbresa.STUDY_TITLE, doi=agbresa.STUDY_DOI, url=agbresa.STUDY_URL,
        context=agbresa.STUDY_CONTEXT, design=agbresa.DESIGN, notice=NOTICE,
        groups=[
            GroupOut(group=g, subjects=len(items), men=sum(s.sex == "male" for s in items),
                     women=sum(s.sex == "female" for s in items))
            for g in ("intermittent", "continuous", "control") if (items := by_group.get(g))
        ],
        moments=agbresa.MOMENTS,
        metrics=[metric_out(m) for m in agbresa.METRICS],
        files=[SourceFileOut.model_validate(f) for f in files],
        invalid_values=invalid,
    )


@router.get("/subjects", response_model=list[AnalogSubjectOut], summary="Volunteers")
def list_subjects(
    group: str | None = Query(None, description="intermittent | continuous | control"),
    db: Session = Depends(get_db),
):
    stmt = select(AnalogSubject).order_by(AnalogSubject.group, AnalogSubject.code)
    if group:
        stmt = stmt.where(AnalogSubject.group == group)
    obs = defaultdict(list)
    for o in observations_for(db):
        obs[o.subject_id].append(o)
    return [subject_out(s, obs[s.id]) for s in db.scalars(stmt)]


@router.get("/subjects/{subject_id}", response_model=AnalogSubjectDetail, summary="History of one volunteer, session by session")
def subject_detail(subject_id: str, db: Session = Depends(get_db)):
    subject = subject_or_404(db, subject_id)
    observations = observations_for(db, subject.id)
    by_session = defaultdict(list)
    for o in observations:
        by_session[(o.bed_rest_day, o.session)].append(o)
    sessions = []
    for (day, name), items in sorted(by_session.items()):
        values, status, pct = defaultdict(dict), defaultdict(dict), defaultdict(dict)
        for o in items:
            values[o.metric][o.moment] = o.value
            status[o.metric][o.moment] = o.status
            pct[o.metric][o.moment] = o.pct_vs_first
        sessions.append(
            SessionDetail(
                session=name, session_index=items[0].session_index,
                phase=agbresa.SESSION_PHASES.get(items[0].session_index, "other"), bed_rest_day=day,
                month=next((o.month for o in items if o.month), None), status=worst(o.status for o in items),
                values=values, metric_status=status, pct_vs_first=pct,
            )
        )
    return AnalogSubjectDetail(notice=NOTICE, subject=subject_out(subject, observations), sessions=sessions)


@router.get("/subjects/{subject_id}/observations", response_model=list[AnalogObservationOut], summary="Raw observations of one volunteer")
def subject_observations(
    subject_id: str,
    metric: str | None = None,
    moment: str | None = Query(None, description="pre | during | post | single"),
    db: Session = Depends(get_db),
):
    subject = subject_or_404(db, subject_id)
    return [
        obs_out(o) for o in observations_for(db, subject.id)
        if (metric is None or o.metric == metric) and (moment is None or o.moment == moment)
    ]


@router.get("/groups/compare", response_model=GroupComparison, summary="Group means at the begin, middle and end of bed rest")
def compare_groups(
    metric: str = Query("heart_rate", description="See the metrics list in GET /api/v1/analog/agbresa"),
    moment: str = Query("during", description="pre | during | post | single"),
    db: Session = Depends(get_db),
):
    m = agbresa.METRICS_BY_CODE.get(metric)
    if m is None:
        raise HTTPException(404, f"Unknown metric {metric!r}")
    stmt = (
        select(AnalogObservation, AnalogSubject.group)
        .join(AnalogSubject, AnalogObservation.subject_id == AnalogSubject.id)
        .where(AnalogObservation.metric == metric, AnalogObservation.moment == moment)
    )
    cells = defaultdict(list)
    days = defaultdict(set)
    for o, group in db.execute(stmt):
        days[(group, o.session_index)].add(o.bed_rest_day)
        if o.value is not None:
            cells[(group, o.session_index)].append(o.value)
    groups = []
    for g in ("intermittent", "continuous", "control"):
        points = []
        for idx in (1, 2, 3):
            values = cells.get((g, idx), [])
            if not values and (g, idx) not in days:
                continue
            points.append(
                GroupPoint(
                    session_index=idx, phase=agbresa.SESSION_PHASES[idx], bed_rest_days=sorted(days[(g, idx)]),
                    n=len(values), mean=statistics.fmean(values) if values else None,
                    sd=statistics.stdev(values) if len(values) > 1 else None,
                    min=min(values) if values else None, max=max(values) if values else None,
                )
            )
        if points:
            groups.append(GroupSeries(group=g, points=points))
    return GroupComparison(metric=metric_out(m), moment=moment, groups=groups)


@router.get("/quality", response_model=list[AnalogObservationOut], summary="Values withheld as physically impossible")
def quality_issues(db: Session = Depends(get_db)):
    return [obs_out(o) for o in observations_for(db) if o.quality != "ok"]


@router.get("/export.csv", summary="All observations as CSV", response_class=StreamingResponse)
def export_csv(db: Session = Depends(get_db)):
    subjects = {s.id: s for s in db.scalars(select(AnalogSubject))}
    columns = ["subject_id", "group", "sex", "session", "session_index", "bed_rest_day", "metric", "unit",
               "moment", "value", "raw_value", "quality", "status", "pct_vs_first"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for o in observations_for(db):
        s = subjects[o.subject_id]
        writer.writerow([
            o.subject_id, s.group, s.sex, o.session, o.session_index, o.bed_rest_day, o.metric,
            agbresa.METRICS_BY_CODE[o.metric].unit, o.moment,
            "" if o.value is None else o.value, "" if o.raw_value is None else o.raw_value,
            o.quality, o.status, "" if o.pct_vs_first is None else round(o.pct_vs_first, 2),
        ])
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="agbresa_observations.csv"'},
    )
