"""Response models for the synthetic mission (daily vital signs)."""

from __future__ import annotations

from datetime import date, datetime

from app.schemas import APIModel, StatusCounts


class MetricOut(APIModel):
    code: str
    column: str
    label: str
    unit: str
    required: bool
    cadence: str
    decimals: int
    thresholds: dict[str, float]
    description: str


class MissionOut(APIModel):
    id: str
    name: str
    description: str
    launch_date: date
    first_day: int
    last_day: int
    synthetic: bool
    seed: int | None
    generated_at: datetime
    astronaut_count: int


class AstronautOut(APIModel):
    id: str
    mission_id: str
    display_name: str
    role: str


class DailyRecordOut(APIModel):
    astronaut_id: str
    mission_id: str
    mission_day: int
    date: date
    phase: str
    values: dict[str, float | None]
    metric_status: dict[str, str]
    status: str


class BaselineOut(APIModel):
    metric: str
    n: int
    mean: float | None
    sd: float | None
    sd_effective: float | None


class EpisodeOut(APIModel):
    astronaut_id: str
    metric: str
    label: str
    unit: str
    status: str
    start_day: int
    end_day: int
    measurements: int
    peak_value: float
    peak_day: int


class MetricComparison(APIModel):
    metric: str
    label: str
    unit: str
    value: float | None
    status: str
    baseline_mean: float | None
    delta_pct: float | None
    z_score: float | None
    last_measured_day: int | None


class AstronautDay(APIModel):
    astronaut: AstronautOut
    mission_day: int
    date: date
    phase: str
    status: str
    metrics: list[MetricComparison]
    active_episodes: list[EpisodeOut]


class CrewMemberDay(APIModel):
    astronaut: AstronautOut
    status: str
    values: dict[str, float | None]
    metric_status: dict[str, str]
    active_episodes: int
    days_with_alerts_last_30: int


class MissionDashboard(APIModel):
    mission: MissionOut
    mission_day: int
    date: date
    counts: StatusCounts
    crew: list[CrewMemberDay]
    status_by_day: list[dict]


class SeriesPoint(APIModel):
    mission_day: int
    value: float | None
    status: str


class AstronautSeries(APIModel):
    astronaut_id: str
    baseline_mean: float | None
    points: list[SeriesPoint]


class CrewMeanPoint(APIModel):
    mission_day: int
    n: int
    mean: float | None


class MetricSeries(APIModel):
    metric: MetricOut
    from_day: int
    to_day: int
    series: list[AstronautSeries]
    crew_mean: list[CrewMeanPoint]


class MetricSummary(APIModel):
    label: str
    unit: str
    n: int
    mean: float | None
    min: float | None
    max: float | None
    last: float | None
    last_day: int | None
    baseline_mean: float | None
    delta_vs_baseline_pct: float | None
    z_vs_baseline: float | None
    trend_per_30_days: float | None
    days_watch: int
    days_out_of_range: int


class WeekSummary(APIModel):
    week_start_day: int
    week_end_day: int
    means: dict[str, float | None]
    status: str


class AgentWindow(APIModel):
    from_day: int
    to_day: int
    days: int


class AgentContext(APIModel):
    data_notice: str
    mission: MissionOut
    astronaut: AstronautOut
    window: AgentWindow
    metrics: list[MetricOut]
    baseline: list[BaselineOut]
    summary: dict[str, MetricSummary]
    weekly: list[WeekSummary]
    episodes: list[EpisodeOut]
    highlights: list[str]
    daily: list[DailyRecordOut] | None = None


class EventOut(APIModel):
    astronaut_id: str
    kind: str
    start_day: int
    end_day: int
    description: str
    affected_metrics: list[str]
