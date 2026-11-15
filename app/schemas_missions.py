"""Response models for the synthetic mission (daily vital signs)."""

from __future__ import annotations

from datetime import date, datetime

from app.provenance import DERIVED, SIMULATED
from pydantic import Field, field_validator

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
    provenance: str = SIMULATED



class AstronautOut(APIModel):
    id: str
    mission_id: str
    display_name: str
    role: str


class DailyRecordOut(APIModel):
    # Day, status and the one-line summary come first so generic list views show them.
    mission_day: int
    status: str
    summary: str = Field(description="The day's vitals in one line, e.g. 'BP 118/76 mmHg · HR 62 bpm · sleep 6.1 h · mood 3/5'")
    date: date
    phase: str
    astronaut_id: str
    mission_id: str
    values: dict[str, float | None]
    metric_status: dict[str, str]
    provenance: str = SIMULATED



class BaselineOut(APIModel):
    metric: str
    n: int
    mean: float | None
    sd: float | None
    sd_effective: float | None

    @field_validator("mean", "sd", "sd_effective")
    @classmethod
    def _round(cls, v: float | None) -> float | None:
        return None if v is None else round(v, 3)


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
    provenance: str = DERIVED



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

    @field_validator("baseline_mean", "delta_pct", "z_score")
    @classmethod
    def _round(cls, v: float | None) -> float | None:
        return None if v is None else round(v, 2)


class AstronautDay(APIModel):
    astronaut: AstronautOut
    mission_day: int
    date: date
    phase: str
    status: str
    summary: str
    metrics: list[MetricComparison]
    values: dict[str, MetricComparison] = Field(description="The same comparisons keyed by metric code")
    active_episodes: list[EpisodeOut]
    provenance: str = SIMULATED


class BaselineSummary(APIModel):
    astronaut_id: str
    mission_id: str
    from_day: int
    to_day: int
    summary: str
    values: dict[str, float | None] = Field(description="Baseline mean keyed by metric code (3 decimals)")

    @field_validator("values")
    @classmethod
    def _round_values(cls, v: dict[str, float | None]) -> dict[str, float | None]:
        return {k: None if x is None else round(x, 3) for k, x in v.items()}
    metrics: list[BaselineOut]
    provenance: str = SIMULATED



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
    provenance: str = SIMULATED



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
    provenance: str = SIMULATED



class EventOut(APIModel):
    astronaut_id: str
    kind: str
    start_day: int
    end_day: int
    description: str
    affected_metrics: list[str]
