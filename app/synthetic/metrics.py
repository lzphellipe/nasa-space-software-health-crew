"""Definitions of the daily health metrics in the synthetic mission dataset.

Thresholds are generic adult screening values, used to color the apps and to
seed the AI agent. They are illustrative, not clinical criteria.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Metric:
    code: str
    column: str  # column name in the CSV export
    label: str
    unit: str
    required: bool
    cadence: str  # daily | weekly | monthly
    decimals: int
    sd_floor: float  # minimum SD used for personal-baseline z-scores
    # Absolute thresholds: high uses ">=", low uses "<".
    out_high: float | None = None
    out_low: float | None = None
    watch_high: float | None = None
    watch_low: float | None = None
    # Thresholds relative to the personal pre-flight baseline, in percent.
    out_rel_low: float | None = None
    watch_rel_low: float | None = None
    description: str = ""

    @property
    def max_gap_days(self) -> int:
        return {"daily": 2, "weekly": 7, "monthly": 31}[self.cadence]

    def thresholds(self) -> dict:
        keys = ("out_high", "out_low", "watch_high", "watch_low", "out_rel_low", "watch_rel_low")
        return {k: v for k, v in asdict(self).items() if k in keys and v is not None}


METRICS: list[Metric] = [
    Metric(
        "systolic_bp", "systolic_bp_mmhg", "Systolic blood pressure", "mmHg", True, "daily", 0, 3.0,
        out_high=140, out_low=90, watch_high=130,
        description="Morning seated measurement.",
    ),
    Metric(
        "diastolic_bp", "diastolic_bp_mmhg", "Diastolic blood pressure", "mmHg", True, "daily", 0, 2.0,
        out_high=90, out_low=60, watch_high=85,
    ),
    Metric(
        "heart_rate", "heart_rate_bpm", "Resting heart rate", "bpm", True, "daily", 0, 2.0,
        out_high=101, out_low=45, watch_high=90,
    ),
    Metric(
        "blood_glucose", "blood_glucose_mg_dl", "Fasting blood glucose", "mg/dL", True, "daily", 0, 3.0,
        out_high=126, out_low=70, watch_high=100,
    ),
    Metric(
        "body_temperature", "body_temperature_c", "Body temperature", "°C", True, "daily", 2, 0.1,
        out_high=38.0, out_low=35.5, watch_high=37.5,
    ),
    Metric(
        "sleep_hours", "sleep_hours", "Sleep", "h", True, "daily", 1, 0.3,
        out_low=5.0, watch_low=6.0, description="Total sleep in the last 24 h.",
    ),
    Metric(
        "mood", "mood_score", "Mood", "/5", True, "daily", 0, 0.3,
        out_low=1.5, watch_low=2.5, description="Self-reported, 1 = very bad, 5 = very good.",
    ),
    Metric(
        "bone_density", "bone_density_g_cm2", "Bone mineral density (hip)", "g/cm²", False, "monthly", 3, 0.005,
        out_rel_low=-10, watch_rel_low=-5,
        description="Two pre-flight scans, then one every 30 days.",
    ),
    Metric(
        "calorie_intake", "calorie_intake_kcal", "Calorie intake", "kcal", False, "daily", 0, 100.0,
        out_rel_low=-40, watch_rel_low=-25,
    ),
    Metric(
        "protein_intake", "protein_intake_g", "Protein intake", "g", False, "daily", 0, 8.0,
        out_rel_low=-40, watch_rel_low=-30,
    ),
    Metric(
        "white_blood_cells", "wbc_10e3_per_ul", "White blood cells", "×10³/µL", False, "weekly", 1, 0.4,
        out_high=11.0, out_low=4.0, description="Weekly finger-prick count (immune system).",
    ),
]

METRICS_BY_CODE = {m.code: m for m in METRICS}
REQUIRED_METRICS = [m.code for m in METRICS if m.required]
