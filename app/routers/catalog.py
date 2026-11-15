"""Reference data and the generic measurement query/export."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Analyte, Study
from app.osdr.analytes import CATEGORIES
from app.schemas import AnalyteOut, CategoryOut, Page, StudyDetail, TimepointOut
from app.services import queries as q

router = APIRouter(prefix="/api/v1", tags=["catalog"])


@router.get("/studies", response_model=list[StudyDetail], summary="OSDR studies and ingested files")
def list_studies(db: Session = Depends(get_db)):
    return db.scalars(select(Study).options(selectinload(Study.files)).order_by(Study.id)).all()


@router.get("/timepoints", response_model=list[TimepointOut], summary="Sample collection timepoints")
def list_timepoints(db: Session = Depends(get_db)):
    return q.timepoints(db)


@router.get("/analytes", response_model=list[AnalyteOut], summary="Measured analytes")
def list_analytes(
    panel: str | None = Query(None, description="cbc | cmp | cardio | immune | alamar"),
    category: str | None = Query(None, description="See /categories"),
    db: Session = Depends(get_db),
):
    stmt = select(Analyte).order_by(Analyte.panel, Analyte.category, Analyte.display_name)
    if panel:
        stmt = stmt.where(Analyte.panel == panel)
    if category:
        stmt = stmt.where(Analyte.category == category)
    return [q.analyte_out(a) for a in db.scalars(stmt)]


@router.get("/categories", response_model=list[CategoryOut], summary="Body-system categories")
def list_categories(db: Session = Depends(get_db)):
    counts = dict(db.execute(select(Analyte.category, func.count()).group_by(Analyte.category)).all())
    return [
        CategoryOut(code=code, label=label, analyte_count=counts[code])
        for code, label in CATEGORIES.items()
        if counts.get(code)
    ]


@router.get("/measurements", response_model=Page, summary="Query measurements (paginated)")
def list_measurements(
    crew_id: str | None = Query(None, alias="crewId"),
    timepoint: str | None = None,
    phase: str | None = Query(None, description="preflight | inflight | postflight"),
    panel: str | None = None,
    category: str | None = None,
    analyte: str | None = None,
    status: str | None = Query(None, description="Comma-separated: out_of_range,watch,normal,no_data"),
    flag: str | None = Query(None, description="Comma-separated: low,high,normal,no_reference,missing"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = q.filtered_query(
        crew_id.upper() if crew_id else None, timepoint, phase, panel, category, analyte, status, flag,
        include_research=True,
    )
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = q.fetch_rows(db, stmt.limit(limit).offset(offset))
    return Page(total=total, limit=limit, offset=offset, items=[q.measurement_out(r) for r in rows])


EXPORT_FIELDS = [
    "crew_id", "timepoint", "phase", "mission_day", "panel", "category", "analyte_code",
    "analyte_name", "unit", "value", "ref_min", "ref_max", "flag", "relative_percent",
    "baseline_mean", "delta_abs", "delta_pct", "z_score", "status",
]


@router.get(
    "/export/measurements.csv",
    summary="Tidy CSV of every measurement (for notebooks and BI tools)",
    response_class=StreamingResponse,
)
def export_measurements(db: Session = Depends(get_db)):
    rows = q.fetch_rows(db, q.filtered_query(include_research=True))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(q.measurement_out(row).model_dump(include=set(EXPORT_FIELDS)))
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="crew_health_measurements.csv"'},
    )
