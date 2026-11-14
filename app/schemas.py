"""Response models. JSON keys are camelCase for the React Native and dashboard clients."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.provenance import REAL


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


# --- reference data -------------------------------------------------------


class StudyOut(APIModel):
    id: str
    title: str
    mission: str
    url: str


class SourceFileOut(APIModel):
    dataset_key: str
    study_id: str
    file_name: str
    url: str
    origin: str
    sha256: str
    size_bytes: int
    row_count: int
    measurement_count: int
    ingested_at: datetime


class StudyDetail(StudyOut):
    files: list[SourceFileOut]


class TimepointOut(APIModel):
    code: str
    phase: str
    offset_days: int
    mission_day: int
    label: str


class AnalyteOut(APIModel):
    code: str
    panel: str
    panel_name: str
    display_name: str
    category: str
    category_label: str
    unit: str
    unit_note: str | None = None
    description: str | None = None
    has_reference_range: bool


class CategoryOut(APIModel):
    code: str
    label: str
    analyte_count: int


# --- measurements ---------------------------------------------------------


class MeasurementOut(APIModel):
    crew_id: str
    timepoint: str
    phase: str
    mission_day: int
    analyte_code: str
    analyte_name: str
    panel: str
    category: str
    unit: str
    value: float | None
    ref_min: float | None
    ref_max: float | None
    flag: str
    relative_percent: float | None
    baseline_mean: float | None
    delta_abs: float | None
    delta_pct: float | None
    z_score: float | None
    status: str
    provenance: str = REAL


class Page(APIModel):
    total: int
    limit: int
    offset: int
    items: list[MeasurementOut]


class BaselineOut(APIModel):
    n: int
    mean: float | None
    sd: float | None
    sd_effective: float | None
    min: float | None
    max: float | None


class SeriesPoint(APIModel):
    timepoint: str
    phase: str
    mission_day: int
    value: float | None
    ref_min: float | None
    ref_max: float | None
    delta_pct: float | None
    z_score: float | None
    flag: str
    status: str


class AnalyteSeries(APIModel):
    crew_id: str
    analyte: AnalyteOut
    baseline: BaselineOut | None
    points: list[SeriesPoint]


# --- crew -----------------------------------------------------------------


class StatusCounts(APIModel):
    out_of_range: int = 0
    watch: int = 0
    normal: int = 0
    no_data: int = 0
    total: int = 0


class CrewMemberOut(APIModel):
    id: str
    display_name: str
    mission: str
    timepoint: str
    overall_status: str
    counts: StatusCounts


class CategorySummary(APIModel):
    category: str
    label: str
    status: str
    counts: StatusCounts
    highlights: list[MeasurementOut]


class Alert(APIModel):
    severity: str  # high | medium
    crew_id: str
    timepoint: str
    analyte_code: str
    analyte_name: str
    category: str
    message: str
    measurement: MeasurementOut


class CrewSummary(APIModel):
    crew_id: str
    display_name: str
    mission: str
    timepoint: TimepointOut
    overall_status: str
    counts: StatusCounts
    categories: list[CategorySummary]
    alerts: list[Alert]
    provenance: str = REAL


class TimelineEntry(APIModel):
    timepoint: TimepointOut
    overall_status: str
    counts: StatusCounts


# --- dashboard ------------------------------------------------------------


class TimepointStatus(APIModel):
    timepoint: TimepointOut
    counts: StatusCounts


class IngestRunOut(APIModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    requested_source: str
    status: str
    files: list[dict]
    warnings: list[str]
    error: str | None


class Overview(APIModel):
    mission: str
    studies: list[StudyOut]
    crew_count: int
    sample_count: int
    measurement_count: int
    analyte_count: int
    default_timepoint: str
    timepoints: list[TimepointStatus]
    crew: list[CrewMemberOut]
    last_ingest: IngestRunOut | None
    provenance: str = REAL


class HeatmapCell(APIModel):
    crew_id: str
    analyte_code: str
    value: float | None
    status: str


class Heatmap(APIModel):
    timepoint: str
    metric: str
    crew: list[str]
    analytes: list[AnalyteOut]
    cells: list[HeatmapCell]


class TrendPoint(APIModel):
    timepoint: str
    mission_day: int
    value: float | None
    status: str


class CrewTrend(APIModel):
    crew_id: str
    points: list[TrendPoint]


class CrewAggregate(APIModel):
    timepoint: str
    mission_day: int
    n: int
    mean: float | None
    min: float | None
    max: float | None
    out_of_range: int


class AnalyteTrend(APIModel):
    analyte: AnalyteOut
    timepoints: list[TimepointOut]
    series: list[CrewTrend]
    crew_aggregate: list[CrewAggregate]
