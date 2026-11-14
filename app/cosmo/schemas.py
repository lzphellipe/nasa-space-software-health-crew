"""Request and response models for the CosmoMedic app layer.

Field names, enums and nested objects follow docs/data/data-model.json of the
frontend (camelCase JSON, ISO-8601 UTC timestamps).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import Field, field_validator, model_validator

from app.schemas import APIModel

MOOD_LABELS = {5: "great", 4: "good", 3: "okay", 2: "tired", 1: "stressed"}
MEASUREMENT_UNITS = {"bone_density": "g/cm²", "muscle_mass": "kg", "strength": "score", "aerobic": "score"}
MEASUREMENT_CADENCE = {"blood_pressure": "daily", "bone_density": "weekly", "muscle_mass": "weekly",
                       "strength": "weekly", "aerobic": "weekly"}
LOG_TYPES = ("mood", "sleep", "radiation", "measurement", "injury", "meal", "exercise", "meditation")


# --- log data per type (input) ----------------------------------------------------


class MoodData(APIModel):
    level: int = Field(ge=1, le=5, description="5 great, 4 good, 3 okay, 2 tired, 1 stressed")
    label: Literal["great", "good", "okay", "tired", "stressed"] | None = None
    tags: list[Annotated[str, Field(max_length=30)]] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def set_label(self):
        self.label = MOOD_LABELS[self.level]
        self.tags = sorted({t.strip().lower() for t in self.tags if t.strip()})
        return self


class SleepBand(APIModel):
    movement_events: int | None = Field(None, ge=0)
    light_exposure_lux: float | None = Field(None, ge=0)
    avg_hr: float | None = Field(None, ge=20, le=220)
    resp_rate: float | None = Field(None, ge=4, le=60)
    noise_db: float | None = Field(None, ge=0, le=140)


class SleepData(APIModel):
    bedtime: datetime
    wake_time: datetime
    duration_min: int | None = Field(None, ge=0, le=16 * 60, description="Computed from bedtime and wakeTime when omitted")
    quality: int = Field(ge=1, le=5)
    wake_ups: Literal["none", "1-2", "3+"] = "none"
    source: Literal["archer_band", "manual"] = "manual"
    band: SleepBand | None = None

    @model_validator(mode="after")
    def check_times(self):
        minutes = (self.wake_time - self.bedtime).total_seconds() / 60
        if minutes <= 0 or minutes > 16 * 60:
            raise ValueError("wakeTime must be after bedtime and at most 16 h later")
        if self.duration_min is None:
            self.duration_min = round(minutes)
        return self


class RadiationData(APIModel):
    value: float = Field(gt=0, le=1_000_000)
    unit: Literal["uSv", "mSv"] = "uSv"
    dose_usv: float | None = Field(None, description="Computed: the value in µSv")
    source: Literal["personal", "area"] = "personal"
    badge_id: str | None = Field(None, max_length=40)
    particle: Literal["total", "neutron"] = "total"
    illustrative: bool = True

    @model_validator(mode="after")
    def normalize(self):
        self.dose_usv = round(self.value * (1000 if self.unit == "mSv" else 1), 3)
        self.illustrative = True
        return self


class MeasurementData(APIModel):
    metric: Literal["blood_pressure", "bone_density", "muscle_mass", "strength", "aerobic"]
    cadence: Literal["daily", "weekly"] | None = None
    values: dict[str, Any] = Field(description="blood_pressure: {systolic, diastolic, pulse}; others: {value, unit}")
    delta_vs_baseline_pct: float | None = Field(None, description="Computed by the API from the astronaut's baseline")

    @model_validator(mode="after")
    def check_values(self):
        self.cadence = MEASUREMENT_CADENCE[self.metric]
        v = self.values
        if self.metric == "blood_pressure":
            try:
                sys_, dia = float(v["systolic"]), float(v["diastolic"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("blood_pressure needs numeric systolic and diastolic") from None
            pulse = v.get("pulse")
            if not (60 <= sys_ <= 250 and 30 <= dia <= 150 and sys_ > dia):
                raise ValueError("blood pressure out of possible range or systolic ≤ diastolic")
            if pulse is not None and not 30 <= float(pulse) <= 220:
                raise ValueError("pulse out of possible range")
            self.values = {"systolic": int(round(sys_)), "diastolic": int(round(dia)),
                           "pulse": int(round(float(pulse))) if pulse is not None else None}
        else:
            try:
                value = float(v["value"])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"{self.metric} needs a numeric value") from None
            if value <= 0:
                raise ValueError("value must be positive")
            self.values = {"value": value, "unit": MEASUREMENT_UNITS[self.metric]}
        return self


class InjuryData(APIModel):
    body_part: Literal["head", "neck", "back", "arm", "hand", "leg", "knee", "foot"]
    side: Literal["left", "right", "center"] = "center"
    kind: Literal["bruise", "cut", "strain", "burn", "pain"]
    severity: Literal["minor", "moderate", "severe"]
    pain: int = Field(ge=1, le=5)
    description: str = Field(min_length=1, max_length=2000)
    photo_url: str | None = Field(None, max_length=500)
    status: Literal["active", "healing", "resolved"] = "active"
    flagged_to_ground: bool | None = Field(None, description="Set by the API: true when severe")

    @model_validator(mode="after")
    def flag(self):
        self.flagged_to_ground = self.severity == "severe"
        return self


class MealData(APIModel):
    name: str = Field(min_length=1, max_length=120)
    items: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    kcal: float = Field(ge=0, le=5000)
    protein_g: float = Field(0, ge=0, le=500)
    carbs_g: float = Field(0, ge=0, le=1000)
    fat_g: float = Field(0, ge=0, le=500)
    taste_rating: int = Field(ge=1, le=5, description="Menu fatigue signal")


class ExerciseData(APIModel):
    modality: Literal["resistive", "aerobic"]
    equipment: Literal["resistive_device", "treadmill", "cycle"]
    duration_min: int = Field(ge=1, le=300)
    effort: int = Field(ge=1, le=5)


class MeditationData(APIModel):
    session: str = Field(min_length=1, max_length=120)
    duration_min: int = Field(ge=1, le=180)
    mood_before: int = Field(ge=1, le=5)
    mood_after: int = Field(ge=1, le=5)


class _LogBase(APIModel):
    note: str | None = Field(None, max_length=2000)
    environment_id: str | None = Field(None, max_length=20)
    timestamp: datetime | None = Field(None, description="Default: now (UTC)")


class MoodLogIn(_LogBase):
    type: Literal["mood"]
    data: MoodData


class SleepLogIn(_LogBase):
    type: Literal["sleep"]
    data: SleepData


class RadiationLogIn(_LogBase):
    type: Literal["radiation"]
    data: RadiationData


class MeasurementLogIn(_LogBase):
    type: Literal["measurement"]
    data: MeasurementData


class InjuryLogIn(_LogBase):
    type: Literal["injury"]
    data: InjuryData


class MealLogIn(_LogBase):
    type: Literal["meal"]
    data: MealData


class ExerciseLogIn(_LogBase):
    type: Literal["exercise"]
    data: ExerciseData


class MeditationLogIn(_LogBase):
    type: Literal["meditation"]
    data: MeditationData


LogIn = Annotated[
    Union[MoodLogIn, SleepLogIn, RadiationLogIn, MeasurementLogIn, InjuryLogIn, MealLogIn, ExerciseLogIn, MeditationLogIn],
    Field(discriminator="type"),
]


# --- responses ----------------------------------------------------------------------


class LogOut(APIModel):
    id: str
    astronaut_id: str
    type: str
    timestamp: datetime
    mission_day: int
    environment_id: str | None
    note: str | None
    coins_earned: int
    data: dict
    provenance: str


class LogSaved(APIModel):
    log: LogOut
    coins: int
    coins_earned: int
    logged_today: bool
    alerts: list[AlertOut]
    support_request_id: int | None = None
    message: str


class AlertOut(APIModel):
    id: str
    astronaut_id: str
    type: str
    level: str
    text: str
    evidence: list[str]
    timestamp: datetime
    mission_day: int
    sent_to_ground: bool
    status: str
    provenance: str


class GroundMessageOut(APIModel):
    id: str
    astronaut_id: str
    sender: str = Field(serialization_alias="from")
    text: str
    urgent: bool
    sent_at: datetime
    estimated_arrival_at: datetime
    status: str  # sending | in_transit | delivered | read
    attachments: list[str]
    provenance: str


class GroundMessageIn(APIModel):
    text: str = Field(min_length=1, max_length=4000)
    urgent: bool = False
    attachments: list[str] = Field(default_factory=list, max_length=10, description="Log ids")


class GroundReplyIn(GroundMessageIn):
    astronaut_id: str


class DailySummaryOut(APIModel):
    astronaut_id: str
    mission_day: int
    logs_done: list[str]
    logs_done_count: int
    logs_total: int
    overall_score: int
    status_label: str
    areas: dict[str, str]
    domains: dict[str, str]
    provenance: str = "RULE_DERIVED"


LogSaved.model_rebuild()
