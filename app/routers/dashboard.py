"""Crew-wide aggregates for the monitoring dashboard."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Analyte, CrewMember, IngestRun, Sample, Study
from app.osdr.datasets import MISSION
from app.routers.crew import crew_member_out
from app.schemas import (
    Alert,
    AnalyteTrend,
    CrewAggregate,
    CrewTrend,
    Heatmap,
    HeatmapCell,
    IngestRunOut,
    Overview,
    StudyOut,
    TimepointStatus,
    TrendPoint,
)
from app.services import queries as q

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("/overview", response_model=Overview, summary="Mission-wide overview")
def overview(timepoint: str | None = Query(None, description="Timepoint for the crew cards"), db: Session = Depends(get_db)):
    tp = q.resolve_timepoint(db, timepoint)
    crew = db.scalars(select(CrewMember).order_by(CrewMember.id)).all()
    per_crew = q.status_by_crew(db, tp.code)

    tp_statuses = defaultdict(list)
    for m, _, t in q.fetch_rows(db, q.base_query()):
        tp_statuses[t.code].append(m.status)

    last = db.scalars(select(IngestRun).order_by(IngestRun.id.desc()).limit(1)).first()
    return Overview(
        mission=MISSION,
        studies=[StudyOut.model_validate(s) for s in db.scalars(select(Study).order_by(Study.id))],
        crew_count=len(crew),
        sample_count=db.scalar(select(func.count()).select_from(Sample)) or 0,
        measurement_count=q.measurement_count(db),
        analyte_count=db.scalar(select(func.count()).select_from(Analyte)) or 0,
        default_timepoint=tp.code,
        timepoints=[
            TimepointStatus(timepoint=q.timepoint_out(t), counts=q.count_statuses(tp_statuses[t.code]))
            for t in q.timepoints(db)
        ],
        crew=[crew_member_out(c, tp.code, per_crew.get(c.id, [])) for c in crew],
        last_ingest=IngestRunOut.model_validate(last) if last else None,
    )


@router.get("/alerts", response_model=list[Alert], summary="Alerts across the whole crew")
def crew_wide_alerts(
    timepoint: str | None = Query(None, description="Omit for every timepoint"),
    severity: Literal["high", "medium"] | None = None,
    db: Session = Depends(get_db),
):
    tp = q.resolve_timepoint(db, timepoint).code if timepoint else None
    status = {"high": "out_of_range", "medium": "watch"}.get(severity or "", "out_of_range,watch")
    return q.alerts_for(q.fetch_rows(db, q.filtered_query(timepoint=tp, status=status)))


@router.get("/heatmap", response_model=Heatmap, summary="Crew × analyte matrix at one timepoint")
def heatmap(
    timepoint: str | None = Query(None, description="Default: first post-flight"),
    metric: Literal["delta_pct", "z_score", "value"] = "delta_pct",
    panel: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    tp = q.resolve_timepoint(db, timepoint)
    rows = q.fetch_rows(db, q.filtered_query(timepoint=tp.code, panel=panel, category=category))
    analytes = {a.code: a for _, a, _ in rows}
    ordered = sorted(analytes.values(), key=lambda a: (a.panel, a.category, a.display_name))
    return Heatmap(
        timepoint=tp.code,
        metric=metric,
        crew=sorted({m.crew_id for m, _, _ in rows}),
        analytes=[q.analyte_out(a) for a in ordered],
        cells=[
            HeatmapCell(crew_id=m.crew_id, analyte_code=m.analyte_code, value=getattr(m, metric), status=m.status)
            for m, _, _ in rows
        ],
    )


@router.get("/analytes/{analyte_code}/trend", response_model=AnalyteTrend, summary="One analyte for every crew member")
def analyte_trend(analyte_code: str, db: Session = Depends(get_db)):
    analyte = q.get_analyte_or_404(db, analyte_code)
    rows = q.fetch_rows(db, q.filtered_query(analyte=analyte.code))
    tps = q.timepoints(db)

    series: dict[str, list[TrendPoint]] = defaultdict(list)
    by_tp = defaultdict(list)
    for m, _, t in rows:
        series[m.crew_id].append(TrendPoint(timepoint=t.code, mission_day=t.mission_day, value=m.value, status=m.status))
        by_tp[t.code].append(m)

    aggregate = []
    for t in tps:
        items = by_tp.get(t.code, [])
        values = [m.value for m in items if m.value is not None]
        aggregate.append(
            CrewAggregate(
                timepoint=t.code,
                mission_day=t.mission_day,
                n=len(values),
                mean=statistics.fmean(values) if values else None,
                min=min(values) if values else None,
                max=max(values) if values else None,
                out_of_range=sum(1 for m in items if m.status == "out_of_range"),
            )
        )
    return AnalyteTrend(
        analyte=q.analyte_out(analyte),
        timepoints=[q.timepoint_out(t) for t in tps],
        series=[CrewTrend(crew_id=c, points=p) for c, p in sorted(series.items())],
        crew_aggregate=aggregate,
    )
