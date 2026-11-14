"""Synthetic long-duration mission: daily vital signs for the app, the dashboard and the AI agent."""

from __future__ import annotations

import csv
import io
import statistics
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth.deps import assert_can_access, get_current_user, require_care_team, require_roles
from app.config import get_settings
from app.db import get_db
from app.models import Astronaut, DailyRecord, Mission, PatternDetection, ProactiveMessage, SyntheticEvent, User
from app.schemas import StatusCounts
from app.schemas_missions import (
    AgentContext,
    AgentWindow,
    AstronautDay,
    AstronautOut,
    AstronautSeries,
    BaselineOut,
    CrewMeanPoint,
    CrewMemberDay,
    DailyRecordOut,
    EpisodeOut,
    EventOut,
    MetricComparison,
    MetricOut,
    MetricSeries,
    MetricSummary,
    MissionDashboard,
    MissionOut,
    SeriesPoint,
    WeekSummary,
)
from app.services.monitoring import backfill_if_empty
from app.services.synthetic_store import get_baselines, get_records, load_synthetic, record_dict
from app.synthetic.assess import (
    find_episodes,
    highlights,
    pct_vs_baseline,
    summarize_window,
    worst,
    z_vs_baseline,
)
from app.synthetic.metrics import METRICS, METRICS_BY_CODE

router = APIRouter(
    prefix="/api/v1/missions",
    tags=["missions (synthetic daily vitals)"],
    dependencies=[Depends(get_current_user)],
)

DATA_NOTICE = (
    "SYNTHETIC DATA. Generated to resemble a ~6-month long-duration mission; not real astronaut "
    "data and not medical advice. Thresholds are generic screening values."
)


# --- helpers ----------------------------------------------------------------


def metric_out(m) -> MetricOut:
    return MetricOut(
        code=m.code, column=m.column, label=m.label, unit=m.unit, required=m.required,
        cadence=m.cadence, decimals=m.decimals, thresholds=m.thresholds(), description=m.description,
    )


def mission_out(db: Session, mission: Mission) -> MissionOut:
    count = db.scalar(select(func.count()).select_from(Astronaut).where(Astronaut.mission_id == mission.id)) or 0
    return MissionOut(
        id=mission.id, name=mission.name, description=mission.description, launch_date=mission.launch_date,
        first_day=mission.first_day, last_day=mission.last_day, synthetic=mission.synthetic,
        seed=mission.seed, generated_at=mission.generated_at, astronaut_count=count,
    )


def record_out(r: DailyRecord) -> DailyRecordOut:
    return DailyRecordOut(
        astronaut_id=r.astronaut_id, mission_id=r.mission_id, mission_day=r.mission_day, date=r.date,
        phase=r.phase, values={m.code: getattr(r, m.code) for m in METRICS},
        metric_status=r.metric_status, status=r.status,
    )


def get_mission_or_404(db: Session, mission_id: str) -> Mission:
    mission = db.get(Mission, mission_id.upper())
    if mission is None:
        raise HTTPException(404, f"Unknown mission {mission_id!r}")
    return mission


def get_astronaut_or_404(db: Session, mission: Mission, astronaut_id: str) -> Astronaut:
    astronaut = db.get(Astronaut, astronaut_id.upper())
    if astronaut is None or astronaut.mission_id != mission.id:
        raise HTTPException(404, f"Unknown astronaut {astronaut_id!r} in mission {mission.id}")
    return astronaut


def check_day(mission: Mission, day: int | None, name: str = "day") -> int:
    if day is None:
        return mission.last_day
    if not mission.first_day <= day <= mission.last_day:
        raise HTTPException(422, f"{name} must be between {mission.first_day} and {mission.last_day}")
    return day


def episodes_for(records: list[DailyRecord]) -> list[EpisodeOut]:
    by_astronaut = defaultdict(list)
    for r in records:
        by_astronaut[r.astronaut_id].append(r)
    episodes = []
    for items in by_astronaut.values():
        episodes += find_episodes([record_dict(r) for r in items], [r.metric_status for r in items])
    episodes.sort(key=lambda e: (e["start_day"], e["astronaut_id"]))
    return [EpisodeOut(**e) for e in episodes]


def status_counts(statuses) -> StatusCounts:
    statuses = list(statuses)
    return StatusCounts(
        out_of_range=statuses.count("out_of_range"), watch=statuses.count("watch"),
        normal=statuses.count("normal"), no_data=statuses.count("no_data"), total=len(statuses),
    )


# --- mission and catalog ------------------------------------------------------


@router.get("", response_model=list[MissionOut], summary="Missions (one synthetic mission)")
def list_missions(db: Session = Depends(get_db)):
    return [mission_out(db, m) for m in db.scalars(select(Mission).order_by(Mission.id))]


@router.get("/{mission_id}", response_model=MissionOut, summary="Mission details")
def get_mission(mission_id: str, db: Session = Depends(get_db)):
    return mission_out(db, get_mission_or_404(db, mission_id))


@router.get("/{mission_id}/metrics", response_model=list[MetricOut], summary="Metric definitions and thresholds")
def list_metrics(mission_id: str, db: Session = Depends(get_db)):
    get_mission_or_404(db, mission_id)
    return [metric_out(m) for m in METRICS]


@router.get("/{mission_id}/astronauts", response_model=list[AstronautOut], summary="Crew of the mission")
def list_astronauts(mission_id: str, db: Session = Depends(get_db)):
    mission = get_mission_or_404(db, mission_id)
    return db.scalars(select(Astronaut).where(Astronaut.mission_id == mission.id).order_by(Astronaut.id)).all()


# --- crew app ----------------------------------------------------------------


@router.get("/{mission_id}/astronauts/{astronaut_id}/daily", response_model=list[DailyRecordOut], summary="Daily records")
def astronaut_daily(
    mission_id: str,
    astronaut_id: str,
    from_day: int | None = Query(None, alias="fromDay"),
    to_day: int | None = Query(None, alias="toDay"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mission = get_mission_or_404(db, mission_id)
    astronaut = get_astronaut_or_404(db, mission, astronaut_id)
    assert_can_access(user, astronaut.id)
    return [record_out(r) for r in get_records(db, mission.id, astronaut.id, from_day, to_day)]


@router.get("/{mission_id}/astronauts/{astronaut_id}/today", response_model=AstronautDay, summary="One day vs the personal baseline")
def astronaut_today(
    mission_id: str,
    astronaut_id: str,
    day: int | None = Query(None, description="Mission day; default: last day of the dataset"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mission = get_mission_or_404(db, mission_id)
    astronaut = get_astronaut_or_404(db, mission, astronaut_id)
    assert_can_access(user, astronaut.id)
    day = check_day(mission, day)
    records = get_records(db, mission.id, astronaut.id, mission.first_day, day)
    today = records[-1]
    baselines = get_baselines(db, astronaut.id)

    metrics = []
    for m in METRICS:
        # Sparse metrics (bone, WBC) show their latest measurement.
        last = next((r for r in reversed(records) if getattr(r, m.code) is not None), None)
        value = getattr(last, m.code) if last else None
        b = baselines.get(m.code)
        metrics.append(
            MetricComparison(
                metric=m.code, label=m.label, unit=m.unit, value=value,
                status=last.metric_status[m.code] if last else "no_data",
                baseline_mean=b.mean if b else None, delta_pct=pct_vs_baseline(value, b),
                z_score=z_vs_baseline(value, b), last_measured_day=last.mission_day if last else None,
            )
        )
    active = [e for e in episodes_for(records) if e.end_day >= day - METRICS_BY_CODE[e.metric].max_gap_days]
    return AstronautDay(
        astronaut=AstronautOut.model_validate(astronaut), mission_day=day, date=today.date, phase=today.phase,
        status=worst(m.status for m in metrics), metrics=metrics, active_episodes=active,
    )


@router.get("/{mission_id}/astronauts/{astronaut_id}/baseline", response_model=list[BaselineOut], summary="30-day pre-flight baseline")
def astronaut_baseline(
    mission_id: str, astronaut_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    mission = get_mission_or_404(db, mission_id)
    astronaut = get_astronaut_or_404(db, mission, astronaut_id)
    assert_can_access(user, astronaut.id)
    return [BaselineOut(**b.as_dict()) for b in get_baselines(db, astronaut.id).values()]


@router.get("/{mission_id}/astronauts/{astronaut_id}/episodes", response_model=list[EpisodeOut], summary="Watch / out-of-range episodes")
def astronaut_episodes(
    mission_id: str,
    astronaut_id: str,
    from_day: int | None = Query(None, alias="fromDay"),
    to_day: int | None = Query(None, alias="toDay"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mission = get_mission_or_404(db, mission_id)
    astronaut = get_astronaut_or_404(db, mission, astronaut_id)
    assert_can_access(user, astronaut.id)
    return episodes_for(get_records(db, mission.id, astronaut.id, from_day, to_day))


# --- AI agent ------------------------------------------------------------------


@router.get(
    "/{mission_id}/astronauts/{astronaut_id}/agent-context",
    response_model=AgentContext,
    summary="Everything an AI agent needs about one astronaut over a time window",
    description=(
        "Baseline, per-metric summary with trends, weekly means, episodes, plain-language highlights "
        "and (optionally) the daily records for the last `windowDays` days up to `untilDay`. "
        "Ground-truth events are NOT included; use /events to evaluate the agent."
    ),
)
def agent_context(
    mission_id: str,
    astronaut_id: str,
    window_days: int = Query(90, alias="windowDays", ge=7, le=211),
    until_day: int | None = Query(None, alias="untilDay"),
    include_daily: bool = Query(True, alias="includeDaily"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mission = get_mission_or_404(db, mission_id)
    astronaut = get_astronaut_or_404(db, mission, astronaut_id)
    assert_can_access(user, astronaut.id)
    to_day = check_day(mission, until_day, "untilDay")
    from_day = max(mission.first_day, to_day - window_days + 1)

    records = get_records(db, mission.id, astronaut.id, from_day, to_day)
    dicts = [record_dict(r) for r in records]
    statuses = [r.metric_status for r in records]
    baselines = get_baselines(db, astronaut.id)
    summary = summarize_window(dicts, statuses, baselines)
    episodes = find_episodes(dicts, statuses)

    weekly = []
    for start in range(from_day, to_day + 1, 7):
        chunk = [(d, s) for d, s in zip(dicts, statuses) if start <= d["mission_day"] < start + 7]
        means = {}
        for m in METRICS:
            values = [d[m.code] for d, _ in chunk if d[m.code] is not None]
            means[m.code] = round(statistics.fmean(values), m.decimals + 1) if values else None
        weekly.append(
            WeekSummary(
                week_start_day=start, week_end_day=min(start + 6, to_day), means=means,
                status=worst(st for _, s in chunk for st in s.values()),
            )
        )

    return AgentContext(
        data_notice=DATA_NOTICE,
        mission=mission_out(db, mission),
        astronaut=AstronautOut.model_validate(astronaut),
        window=AgentWindow(from_day=from_day, to_day=to_day, days=len(records)),
        metrics=[metric_out(m) for m in METRICS],
        baseline=[BaselineOut(**b.as_dict()) for b in baselines.values()],
        summary={code: MetricSummary(**s) for code, s in summary.items()},
        weekly=weekly,
        episodes=[EpisodeOut(**e) for e in episodes],
        highlights=highlights(summary, episodes),
        daily=[record_out(r) for r in records] if include_daily else None,
    )


# --- dashboard -----------------------------------------------------------------


@router.get("/{mission_id}/dashboard", response_model=MissionDashboard, summary="Crew status on one mission day", dependencies=[Depends(require_care_team)])
def mission_dashboard(
    mission_id: str,
    day: int | None = Query(None, description="Mission day; default: last day of the dataset"),
    db: Session = Depends(get_db),
):
    mission = get_mission_or_404(db, mission_id)
    day = check_day(mission, day)
    astronauts = db.scalars(select(Astronaut).where(Astronaut.mission_id == mission.id).order_by(Astronaut.id)).all()
    records = get_records(db, mission.id, None, mission.first_day, day)
    by_astronaut = defaultdict(list)
    for r in records:
        by_astronaut[r.astronaut_id].append(r)

    crew, day_statuses = [], []
    for a in astronauts:
        items = by_astronaut[a.id]
        today = items[-1]
        day_statuses.append(today.status)
        episodes = episodes_for(items)
        crew.append(
            CrewMemberDay(
                astronaut=AstronautOut.model_validate(a),
                status=today.status,
                values={m.code: getattr(today, m.code) for m in METRICS},
                metric_status=today.metric_status,
                active_episodes=sum(1 for e in episodes if e.end_day >= day - METRICS_BY_CODE[e.metric].max_gap_days),
                days_with_alerts_last_30=sum(
                    1 for r in items if r.mission_day > day - 30 and r.status in ("watch", "out_of_range")
                ),
            )
        )

    per_day = defaultdict(list)
    for r in records:
        per_day[r.mission_day].append(r.status)
    status_by_day = [
        {"missionDay": d, **status_counts(s).model_dump(by_alias=True)} for d, s in sorted(per_day.items())
    ]
    return MissionDashboard(
        mission=mission_out(db, mission), mission_day=day, date=by_astronaut[astronauts[0].id][-1].date,
        counts=status_counts(day_statuses), crew=crew, status_by_day=status_by_day,
    )


@router.get("/{mission_id}/metrics/{metric}/series", response_model=MetricSeries, summary="One metric for the whole crew", dependencies=[Depends(require_care_team)])
def metric_series(
    mission_id: str,
    metric: str,
    from_day: int | None = Query(None, alias="fromDay"),
    to_day: int | None = Query(None, alias="toDay"),
    db: Session = Depends(get_db),
):
    mission = get_mission_or_404(db, mission_id)
    m = METRICS_BY_CODE.get(metric)
    if m is None:
        raise HTTPException(404, f"Unknown metric {metric!r}. See /metrics")
    from_day = mission.first_day if from_day is None else from_day
    to_day = mission.last_day if to_day is None else to_day
    records = get_records(db, mission.id, None, from_day, to_day)

    series = defaultdict(list)
    per_day = defaultdict(list)
    for r in records:
        value = getattr(r, m.code)
        if value is None:
            continue
        series[r.astronaut_id].append(SeriesPoint(mission_day=r.mission_day, value=value, status=r.metric_status[m.code]))
        per_day[r.mission_day].append(value)

    return MetricSeries(
        metric=metric_out(m),
        from_day=from_day,
        to_day=to_day,
        series=[
            AstronautSeries(
                astronaut_id=aid,
                baseline_mean=get_baselines(db, aid)[m.code].mean,
                points=pts,
            )
            for aid, pts in sorted(series.items())
        ],
        crew_mean=[
            CrewMeanPoint(mission_day=d, n=len(v), mean=round(statistics.fmean(v), m.decimals + 1))
            for d, v in sorted(per_day.items())
        ],
    )


@router.get("/{mission_id}/episodes", response_model=list[EpisodeOut], summary="Episodes across the crew", dependencies=[Depends(require_care_team)])
def mission_episodes(
    mission_id: str,
    from_day: int | None = Query(None, alias="fromDay"),
    to_day: int | None = Query(None, alias="toDay"),
    status: str | None = Query(None, description="watch | out_of_range"),
    min_measurements: int = Query(1, alias="minMeasurements", ge=1, description="Hide shorter episodes"),
    db: Session = Depends(get_db),
):
    mission = get_mission_or_404(db, mission_id)
    episodes = episodes_for(get_records(db, mission.id, None, from_day, to_day))
    return [
        e for e in episodes
        if (status is None or e.status == status) and e.measurements >= min_measurements
    ]


@router.get(
    "/{mission_id}/events",
    response_model=list[EventOut],
    summary="Ground-truth events injected by the generator (for evaluating the AI agent)",
    dependencies=[Depends(require_care_team)],
)
def mission_events(mission_id: str, db: Session = Depends(get_db)):
    mission = get_mission_or_404(db, mission_id)
    return db.scalars(
        select(SyntheticEvent).where(SyntheticEvent.mission_id == mission.id).order_by(SyntheticEvent.start_day)
    ).all()


@router.get(
    "/{mission_id}/export.csv", summary="Daily records as CSV", response_class=StreamingResponse,
    dependencies=[Depends(require_care_team)],
)
def export_csv(mission_id: str, db: Session = Depends(get_db)):
    mission = get_mission_or_404(db, mission_id)
    columns = ["astronaut_id", "mission_id", "mission_day", "date", "phase"] + [m.column for m in METRICS] + ["status"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for r in get_records(db, mission.id):
        writer.writerow(
            [r.astronaut_id, r.mission_id, r.mission_day, r.date.isoformat(), r.phase]
            + ["" if getattr(r, m.code) is None else getattr(r, m.code) for m in METRICS]
            + [r.status]
        )
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{mission.id}_daily_vitals.csv"'},
    )


@router.post(
    "/regenerate",
    response_model=MissionOut,
    dependencies=[Depends(require_roles("admin"))],
    summary="Regenerate the synthetic mission with another seed (admin)",
)
def regenerate(seed: int = Query(None, description="Default: SYNTHETIC_SEED"), db: Session = Depends(get_db)):
    mission = load_synthetic(db, seed if seed is not None else get_settings().synthetic_seed)
    # Patterns and proactive messages were derived from the old data: replay them.
    db.execute(delete(PatternDetection).where(PatternDetection.mission_id == mission.id))
    db.execute(delete(ProactiveMessage).where(ProactiveMessage.mission_id == mission.id))
    db.commit()
    backfill_if_empty(db)
    return mission_out(db, mission)
