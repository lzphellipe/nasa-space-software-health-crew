"""Per-crew-member endpoints, shaped for the React Native app."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import BaselineStat, CrewMember
from app.osdr.analytes import CATEGORIES
from app.schemas import (
    Alert,
    AnalyteSeries,
    BaselineOut,
    CategorySummary,
    CrewMemberOut,
    CrewSummary,
    MeasurementOut,
    SeriesPoint,
    TimelineEntry,
)
from app.services import queries as q
from app.services.analytics import STATUS_ORDER

router = APIRouter(prefix="/api/v1/crew", tags=["crew"])

TIMEPOINT_HELP = "Timepoint code (e.g. R+1), 'default' (first post-flight) or 'latest'"


def crew_member_out(crew: CrewMember, timepoint: str, statuses: list[str]) -> CrewMemberOut:
    return CrewMemberOut(
        id=crew.id,
        display_name=crew.display_name,
        mission=crew.mission,
        timepoint=timepoint,
        overall_status=q.overall(statuses),
        counts=q.count_statuses(statuses),
    )


@router.get("", response_model=list[CrewMemberOut], summary="Crew members with status at a timepoint")
def list_crew(timepoint: str | None = Query(None, description=TIMEPOINT_HELP), db: Session = Depends(get_db)):
    tp = q.resolve_timepoint(db, timepoint)
    statuses = q.status_by_crew(db, tp.code)
    crew = db.scalars(select(CrewMember).order_by(CrewMember.id))
    return [crew_member_out(c, tp.code, statuses.get(c.id, [])) for c in crew]


@router.get("/{crew_id}/summary", response_model=CrewSummary, summary="Health summary by body system")
def crew_summary(
    crew_id: str,
    timepoint: str | None = Query(None, description=TIMEPOINT_HELP),
    highlights: int = Query(3, ge=0, le=20, description="Most notable measurements per category"),
    db: Session = Depends(get_db),
):
    crew = q.get_crew_or_404(db, crew_id)
    tp = q.resolve_timepoint(db, timepoint)
    rows = q.fetch_rows(db, q.filtered_query(crew_id=crew.id, timepoint=tp.code))

    by_category = defaultdict(list)
    for row in rows:
        by_category[row[1].category].append(row)

    categories = []
    for code, label in CATEGORIES.items():
        items = by_category.get(code)
        if not items:
            continue
        statuses = [r[0].status for r in items]
        ranked = sorted(
            items, key=lambda r: (-STATUS_ORDER[r[0].status], -abs(r[0].z_score or 0))
        )
        categories.append(
            CategorySummary(
                category=code,
                label=label,
                status=q.overall(statuses),
                counts=q.count_statuses(statuses),
                highlights=[q.measurement_out(r) for r in ranked[:highlights]],
            )
        )

    statuses = [r[0].status for r in rows]
    return CrewSummary(
        crew_id=crew.id,
        display_name=crew.display_name,
        mission=crew.mission,
        timepoint=q.timepoint_out(tp),
        overall_status=q.overall(statuses),
        counts=q.count_statuses(statuses),
        categories=categories,
        alerts=q.alerts_for(rows),
    )


@router.get("/{crew_id}/timeline", response_model=list[TimelineEntry], summary="Status at every timepoint")
def crew_timeline(crew_id: str, db: Session = Depends(get_db)):
    crew = q.get_crew_or_404(db, crew_id)
    rows = q.fetch_rows(db, q.filtered_query(crew_id=crew.id))
    by_tp = defaultdict(list)
    for m, _, t in rows:
        by_tp[t.code].append(m.status)
    return [
        TimelineEntry(
            timepoint=q.timepoint_out(t),
            overall_status=q.overall(by_tp[t.code]),
            counts=q.count_statuses(by_tp[t.code]),
        )
        for t in q.timepoints(db)
        if t.code in by_tp
    ]


@router.get("/{crew_id}/measurements", response_model=list[MeasurementOut], summary="Measurements of one crew member")
def crew_measurements(
    crew_id: str,
    timepoint: str | None = None,
    panel: str | None = None,
    category: str | None = None,
    status: str | None = Query(None, description="Comma-separated statuses"),
    db: Session = Depends(get_db),
):
    crew = q.get_crew_or_404(db, crew_id)
    stmt = q.filtered_query(crew_id=crew.id, timepoint=timepoint, panel=panel, category=category, status=status)
    return [q.measurement_out(r) for r in q.fetch_rows(db, stmt)]


@router.get("/{crew_id}/alerts", response_model=list[Alert], summary="Out-of-range and baseline-deviation alerts")
def crew_alerts(
    crew_id: str,
    timepoint: str | None = Query(None, description="Omit for every timepoint"),
    db: Session = Depends(get_db),
):
    crew = q.get_crew_or_404(db, crew_id)
    tp = q.resolve_timepoint(db, timepoint).code if timepoint else None
    stmt = q.filtered_query(crew_id=crew.id, timepoint=tp, status="out_of_range,watch")
    return q.alerts_for(q.fetch_rows(db, stmt))


@router.get(
    "/{crew_id}/analytes/{analyte_code}/series",
    response_model=AnalyteSeries,
    summary="Time series of one analyte with the personal baseline",
)
def crew_analyte_series(crew_id: str, analyte_code: str, db: Session = Depends(get_db)):
    crew = q.get_crew_or_404(db, crew_id)
    analyte = q.get_analyte_or_404(db, analyte_code)
    rows = q.fetch_rows(db, q.filtered_query(crew_id=crew.id, analyte=analyte.code))
    stat = db.get(BaselineStat, (crew.id, analyte.code))
    return AnalyteSeries(
        crew_id=crew.id,
        analyte=q.analyte_out(analyte),
        baseline=BaselineOut.model_validate(stat) if stat else None,
        points=[
            SeriesPoint(
                timepoint=t.code,
                phase=t.phase,
                mission_day=t.mission_day,
                value=m.value,
                ref_min=m.ref_min,
                ref_max=m.ref_max,
                delta_pct=m.delta_pct,
                z_score=m.z_score,
                flag=m.flag,
                status=m.status,
            )
            for m, _, t in rows
        ],
    )
