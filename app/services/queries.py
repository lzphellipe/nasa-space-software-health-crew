"""Read-side queries shared by the crew (mobile) and dashboard routers."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.osdr.datasets import RESEARCH_PANELS
from app.models import Analyte, CrewMember, Measurement, Timepoint
from app.osdr.analytes import CATEGORIES
from app.schemas import (
    Alert,
    AnalyteOut,
    MeasurementOut,
    StatusCounts,
    TimepointOut,
)
from app.services.analytics import STATUS_ORDER, worst_status

Row = tuple[Measurement, Analyte, Timepoint]


def analyte_out(a: Analyte) -> AnalyteOut:
    return AnalyteOut(
        code=a.code,
        panel=a.panel,
        panel_name=a.panel_name,
        display_name=a.display_name,
        category=a.category,
        category_label=CATEGORIES.get(a.category, a.category),
        unit=a.unit,
        unit_note=a.unit_note,
        description=a.description,
        has_reference_range=a.has_reference_range,
    )


def measurement_out(row: Row) -> MeasurementOut:
    m, a, t = row
    return MeasurementOut(
        crew_id=m.crew_id,
        timepoint=t.code,
        phase=t.phase,
        mission_day=t.mission_day,
        analyte_code=a.code,
        analyte_name=a.display_name,
        panel=a.panel,
        category=a.category,
        unit=a.unit,
        value=m.value,
        ref_min=m.ref_min,
        ref_max=m.ref_max,
        flag=m.flag,
        relative_percent=m.relative_percent,
        baseline_mean=m.baseline_mean,
        delta_abs=m.delta_abs,
        delta_pct=m.delta_pct,
        z_score=m.z_score,
        status=m.status,
    )


def base_query() -> Select:
    return (
        select(Measurement, Analyte, Timepoint)
        .join(Analyte, Measurement.analyte_code == Analyte.code)
        .join(Timepoint, Measurement.timepoint_code == Timepoint.code)
    )


def filtered_query(
    crew_id: str | None = None,
    timepoint: str | None = None,
    phase: str | None = None,
    panel: str | None = None,
    category: str | None = None,
    analyte: str | None = None,
    status: str | None = None,
    flag: str | None = None,
    include_research: bool = False,
) -> Select:
    """Research panels (immune cytokine arrays) are left out unless asked for by panel or analyte,
    or with include_research=True."""
    q = base_query()
    if not (include_research or panel or analyte):
        q = q.where(Analyte.panel.not_in(RESEARCH_PANELS))
    if crew_id:
        q = q.where(Measurement.crew_id == crew_id)
    if timepoint:
        q = q.where(Measurement.timepoint_code == timepoint)
    if phase:
        q = q.where(Timepoint.phase == phase)
    if panel:
        q = q.where(Analyte.panel == panel)
    if category:
        q = q.where(Analyte.category == category)
    if analyte:
        q = q.where(Measurement.analyte_code == analyte)
    if status:
        q = q.where(Measurement.status.in_(status.split(",")))
    if flag:
        q = q.where(Measurement.flag.in_(flag.split(",")))
    return q.order_by(Measurement.crew_id, Timepoint.mission_day, Analyte.panel, Analyte.code)


def fetch_rows(session: Session, query: Select) -> list[Row]:
    return [tuple(r) for r in session.execute(query).all()]


def count_statuses(statuses: Iterable[str]) -> StatusCounts:
    counter = Counter(statuses)
    return StatusCounts(
        out_of_range=counter.get("out_of_range", 0),
        watch=counter.get("watch", 0),
        normal=counter.get("normal", 0),
        no_data=counter.get("no_data", 0),
        total=sum(counter.values()),
    )


def timepoints(session: Session) -> list[Timepoint]:
    return list(session.scalars(select(Timepoint).order_by(Timepoint.mission_day)))


def default_timepoint(session: Session) -> Timepoint:
    """First post-flight collection (R+1): the clearest view of flight effects."""
    items = timepoints(session)
    if not items:
        raise HTTPException(503, "No data ingested yet. POST /api/v1/ingest first.")
    return next((t for t in items if t.phase == "postflight"), items[-1])


def resolve_timepoint(session: Session, code: str | None) -> Timepoint:
    if code is None or code == "default":
        return default_timepoint(session)
    if code == "latest":
        return timepoints(session)[-1]
    tp = session.get(Timepoint, code)
    if tp is None:
        raise HTTPException(404, f"Unknown timepoint {code!r}")
    return tp


def get_crew_or_404(session: Session, crew_id: str) -> CrewMember:
    crew = session.get(CrewMember, crew_id.upper())
    if crew is None:
        raise HTTPException(404, f"Unknown crew member {crew_id!r}")
    return crew


def get_analyte_or_404(session: Session, code: str) -> Analyte:
    analyte = session.get(Analyte, code)
    if analyte is None:
        raise HTTPException(404, f"Unknown analyte {code!r}")
    return analyte


def status_by_crew(session: Session, timepoint: str) -> dict[str, list[str]]:
    """Statuses of the clinical panels only (research panels do not set a crew member's status)."""
    result: dict[str, list[str]] = defaultdict(list)
    q = (select(Measurement.crew_id, Measurement.status)
         .join(Analyte, Measurement.analyte_code == Analyte.code)
         .where(Measurement.timepoint_code == timepoint, Analyte.panel.not_in(RESEARCH_PANELS)))
    for crew_id, status in session.execute(q):
        result[crew_id].append(status)
    return result


def _fmt(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.4g}" if abs(x) < 1e5 else f"{x:,.0f}"


def build_alert(row: Row) -> Alert | None:
    m, a, t = row
    out = measurement_out(row)
    if m.status == "out_of_range":
        bound = "below" if m.flag == "low" else "above"
        rng = f"{_fmt(m.ref_min)}–{_fmt(m.ref_max)}" if m.ref_max is not None else f"≥ {_fmt(m.ref_min)}"
        unit = "" if a.unit == "ratio" else f" {a.unit}"
        message = f"{a.display_name} {_fmt(m.value)}{unit} is {bound} the reference range ({rng})."
        severity = "high"
    elif m.status == "watch":
        direction = "above" if (m.delta_pct or 0) > 0 else "below"
        message = (
            f"{a.display_name} is {abs(m.delta_pct or 0):.0f}% {direction} this crew member's "
            f"pre-flight baseline (z = {m.z_score:+.1f})."
        )
        severity = "medium"
    else:
        return None
    return Alert(
        severity=severity,
        crew_id=m.crew_id,
        timepoint=t.code,
        analyte_code=a.code,
        analyte_name=a.display_name,
        category=a.category,
        message=message,
        measurement=out,
    )


def alerts_for(rows: Iterable[Row]) -> list[Alert]:
    alerts = [a for a in (build_alert(r) for r in rows) if a is not None]
    alerts.sort(
        key=lambda a: (
            -STATUS_ORDER[a.measurement.status],
            -abs(a.measurement.z_score or 0),
        )
    )
    return alerts


def overall(statuses: Iterable[str]) -> str:
    return worst_status(statuses)


def measurement_count(session: Session, research: bool = False) -> int:
    """Clinical measurements by default; research=True counts the research panels instead."""
    panels = Analyte.panel.in_(RESEARCH_PANELS) if research else Analyte.panel.not_in(RESEARCH_PANELS)
    q = select(func.count()).select_from(Measurement).join(Analyte, Measurement.analyte_code == Analyte.code).where(panels)
    return session.scalar(q) or 0


def timepoint_out(t: Timepoint) -> TimepointOut:
    return TimepointOut.model_validate(t)
