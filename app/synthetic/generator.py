"""Synthetic daily health history for one long-duration mission with four astronauts.

OSDR has no daily vital signs for any crew, so this module creates them. The
signal for each astronaut and metric is:

    personal baseline + mission adaptation + labeled event + autocorrelated noise

Mission adaptations follow effects reported for ~6-month ISS stays (orders of
magnitude, not exact values):
* fluid shift: blood pressure a few mmHg lower in the first week;
* shorter sleep in flight (~6 h) and a little before launch;
* body temperature drifting up by ~0.5 °C over the first months ("space fever");
* mild insulin resistance: fasting glucose creeping up;
* hip bone density falling ~1–1.5 % per month;
* eating below requirement (~85 % of pre-flight intake);
* a mood dip in the third quarter of the mission.

On top of that, each astronaut gets one labeled event (ground truth for
evaluating the AI agent). Generation is deterministic for a given seed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.synthetic.checkins import generate_checkins
from app.synthetic.metrics import METRICS

MISSION_ID = "SYN-LDM-01"
MISSION_NAME = "Synthetic Long-Duration Mission 01"
LAUNCH_DATE = date(2027, 3, 1)
FIRST_DAY = -30
LAST_DAY = 180

BONE_SCAN_DAYS = {-30, -2, 30, 60, 90, 120, 150, 180}
WBC_DAYS = {d for d in range(FIRST_DAY, LAST_DAY + 1) if d % 7 == 0}


@dataclass(frozen=True)
class AstronautProfile:
    id: str
    display_name: str
    role: str
    baseline: dict[str, float]


ASTRONAUTS = [
    AstronautProfile("A001", "Astronaut 1", "Commander", {
        "systolic_bp": 118, "diastolic_bp": 76, "heart_rate": 62, "blood_glucose": 88,
        "body_temperature": 36.6, "sleep_hours": 7.2, "mood": 4.2, "bone_density": 1.020,
        "calorie_intake": 2700, "protein_intake": 110, "white_blood_cells": 6.2,
    }),
    AstronautProfile("A002", "Astronaut 2", "Pilot", {
        "systolic_bp": 114, "diastolic_bp": 75, "heart_rate": 58, "blood_glucose": 84,
        "body_temperature": 36.5, "sleep_hours": 7.4, "mood": 4.0, "bone_density": 0.950,
        "calorie_intake": 2300, "protein_intake": 90, "white_blood_cells": 5.6,
    }),
    AstronautProfile("A003", "Astronaut 3", "Mission Specialist 1", {
        "systolic_bp": 124, "diastolic_bp": 80, "heart_rate": 66, "blood_glucose": 90,
        "body_temperature": 36.7, "sleep_hours": 7.1, "mood": 4.4, "bone_density": 1.080,
        "calorie_intake": 2900, "protein_intake": 120, "white_blood_cells": 7.0,
    }),
    AstronautProfile("A004", "Astronaut 4", "Mission Specialist 2", {
        "systolic_bp": 120, "diastolic_bp": 78, "heart_rate": 64, "blood_glucose": 89,
        "body_temperature": 36.6, "sleep_hours": 7.1, "mood": 3.9, "bone_density": 0.980,
        "calorie_intake": 2500, "protein_intake": 100, "white_blood_cells": 6.5,
    }),
]

NOISE_SD = {
    "systolic_bp": 5.0, "diastolic_bp": 4.0, "heart_rate": 3.0, "blood_glucose": 4.0,
    "body_temperature": 0.15, "sleep_hours": 0.4, "mood": 0.45, "bone_density": 0.005,
    "calorie_intake": 150.0, "protein_intake": 7.0, "white_blood_cells": 0.5,
}
AR_PHI = 0.6  # day-to-day autocorrelation of the noise


@dataclass(frozen=True)
class EventSpec:
    astronaut_id: str
    kind: str
    start_day: int
    end_day: int
    description: str
    # metric -> (delta, mode). mode: "bump" (rises and falls back), "drift" (ramps to
    # delta at end_day and stays), "relative_bump" (delta is a fraction of the baseline).
    effects: dict[str, tuple[float, str]] = field(default_factory=dict)
    ramp_days: int = 2
    # Extra hip bone loss in % per 30 days while the event lasts (cumulative).
    extra_bone_loss_pct_per_30d: float = 0.0


EVENTS = [
    EventSpec(
        "A002", "respiratory_infection", 47, 53,
        "Upper respiratory infection: fever, higher resting HR, more white cells, "
        "poor sleep and appetite.",
        {
            "body_temperature": (1.6, "bump"), "heart_rate": (14, "bump"), "sleep_hours": (-1.0, "bump"),
            "mood": (-1.2, "bump"), "white_blood_cells": (6.0, "bump"),
            "calorie_intake": (-0.30, "relative_bump"), "protein_intake": (-0.30, "relative_bump"),
        },
        ramp_days=1,
    ),
    EventSpec(
        "A003", "hypertension_trend", 60, 180,
        "Progressive rise in blood pressure over the second half of the mission.",
        {"systolic_bp": (18, "drift"), "diastolic_bp": (10, "drift")},
    ),
    EventSpec(
        "A004", "sleep_disruption", 100, 128,
        "Circadian misalignment during a heavy work period: short sleep, low mood, "
        "slightly higher HR and BP.",
        {"sleep_hours": (-1.8, "bump"), "mood": (-1.3, "bump"), "heart_rate": (4, "bump"), "systolic_bp": (4, "bump")},
        ramp_days=4,
    ),
    EventSpec(
        "A001", "exercise_countermeasure_interruption", 120, 150,
        "Treadmill and resistance device unavailable: faster bone loss, higher fasting "
        "glucose and resting HR.",
        {"blood_glucose": (12, "bump"), "heart_rate": (5, "bump"), "mood": (-0.4, "bump")},
        ramp_days=7,
        extra_bone_loss_pct_per_30d=2.5,
    ),
]


@dataclass
class SyntheticDataset:
    mission: dict
    astronauts: list[AstronautProfile]
    records: list[dict]
    events: list[EventSpec]
    seed: int
    checkins: list[dict] = field(default_factory=list)


def ramp(d: float, start: float, length: float) -> float:
    if length <= 0:
        return 1.0 if d >= start else 0.0
    return min(max((d - start) / length, 0.0), 1.0)


def bump(d: float, start: float, end: float, ramp_days: float) -> float:
    """Trapezoid: 0 before start, ramps to 1, holds, ramps back to 0 after end."""
    if d < start - ramp_days or d > end + ramp_days:
        return 0.0
    if d < start:
        return 1.0 - (start - d) / (ramp_days + 1)
    if d > end:
        return 1.0 - (d - end) / (ramp_days + 1)
    return 1.0


def mission_effect(metric: str, d: int, base: float) -> float:
    inflight = d >= 0
    if metric == "systolic_bp":
        return -5 * ramp(d, 0, 7) if inflight else 0.0
    if metric == "diastolic_bp":
        return -4 * ramp(d, 0, 7) if inflight else 0.0
    if metric == "heart_rate":
        return -3 * ramp(d, 0, 14) if inflight else 0.0
    if metric == "blood_glucose":
        return 4 * ramp(d, 0, 120)
    if metric == "body_temperature":
        return 0.5 * ramp(d, 0, 75)
    if metric == "sleep_hours":
        if inflight:
            return -0.6 * ramp(d, 0, 10)
        return -0.3 if d >= -7 else 0.0
    if metric == "mood":
        return 0.2 * bump(d, 0, 20, 5) - 0.5 * bump(d, 95, 130, 15)
    if metric in ("calorie_intake", "protein_intake"):
        return -0.15 * base * ramp(d, 0, 14)
    if metric == "white_blood_cells":
        return 0.4 * bump(d, 0, 14, 3)
    return 0.0


def bone_loss_fraction(profile_id: str, d: int, events: list[EventSpec]) -> float:
    """Cumulative fractional hip BMD loss at day d."""
    loss = 0.012 * max(d, 0) / 30
    for ev in events:
        if ev.astronaut_id == profile_id and ev.extra_bone_loss_pct_per_30d:
            days_in = min(max(d - ev.start_day, 0), ev.end_day - ev.start_day)
            loss += ev.extra_bone_loss_pct_per_30d / 100 * days_in / 30
    return loss


def event_effect(metric: str, d: int, base: float, events: list[EventSpec]) -> float:
    total = 0.0
    for ev in events:
        if metric not in ev.effects:
            continue
        delta, mode = ev.effects[metric]
        if mode == "bump":
            total += delta * bump(d, ev.start_day, ev.end_day, ev.ramp_days)
        elif mode == "relative_bump":
            total += delta * base * bump(d, ev.start_day, ev.end_day, ev.ramp_days)
        elif mode == "drift":
            total += delta * ramp(d, ev.start_day, ev.end_day - ev.start_day)
    return total


def _measured(metric: str, d: int) -> bool:
    if metric == "bone_density":
        return d in BONE_SCAN_DAYS
    if metric == "white_blood_cells":
        return d in WBC_DAYS
    return True


def generate(seed: int = 42) -> SyntheticDataset:
    rng = random.Random(seed)
    records: list[dict] = []
    decimals = {m.code: m.decimals for m in METRICS}

    for profile in ASTRONAUTS:
        own_events = [e for e in EVENTS if e.astronaut_id == profile.id]
        noise = {m: 0.0 for m in NOISE_SD}
        for d in range(FIRST_DAY, LAST_DAY + 1):
            row = {
                "astronaut_id": profile.id,
                "mission_id": MISSION_ID,
                "mission_day": d,
                "date": (LAUNCH_DATE + timedelta(days=d)).isoformat(),
                "phase": "preflight" if d < 0 else "inflight",
            }
            for metric, sd in NOISE_SD.items():
                noise[metric] = AR_PHI * noise[metric] + math.sqrt(1 - AR_PHI**2) * rng.gauss(0, sd)
                base = profile.baseline[metric]
                if not _measured(metric, d):
                    row[metric] = None
                    continue
                if metric == "bone_density":
                    value = base * (1 - bone_loss_fraction(profile.id, d, own_events)) + rng.gauss(0, sd)
                else:
                    value = (
                        base
                        + mission_effect(metric, d, base)
                        + event_effect(metric, d, base, own_events)
                        + noise[metric]
                    )
                if metric == "mood":
                    value = min(max(round(value), 1), 5)
                elif metric == "sleep_hours":
                    value = min(max(value, 2.0), 10.0)
                row[metric] = round(value, decimals[metric]) if decimals[metric] else int(round(value))
            records.append(row)

    mission = {
        "id": MISSION_ID,
        "name": MISSION_NAME,
        "launch_date": LAUNCH_DATE,
        "first_day": FIRST_DAY,
        "last_day": LAST_DAY,
        "synthetic": True,
        "seed": seed,
        "description": (
            "Synthetic daily health history: 30-day pre-flight baseline and 180 days in flight "
            "for four astronauts. Generated because OSDR has no daily vital signs for any crew."
        ),
    }
    return SyntheticDataset(
        mission=mission, astronauts=list(ASTRONAUTS), records=records, events=list(EVENTS), seed=seed,
        checkins=generate_checkins(records, seed),
    )
