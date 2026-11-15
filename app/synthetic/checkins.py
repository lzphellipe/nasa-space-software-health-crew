"""Check-in catalog (stress, fatigue, symptoms) and the synthetic check-in history.

The synthetic history uses its own random stream, so the daily vitals of a
given seed do not change. It follows the vitals and the labeled events:
* first days in flight: space adaptation (nausea, headache, congestion, back pain);
* fatigue tracks short sleep;
* A002 infection: congestion, sore throat, cough, headache, high fatigue;
* A004 sleep disruption: high stress and fatigue, trouble sleeping;
* A001 exercise device unavailable: more fatigue, muscle and back pain;
* A003 hypertension trend: occasional headaches late in the mission.
About 10 % of days have no check-in.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# code -> (label, urgent). Urgent symptoms open an urgent request to the care team.
SYMPTOMS: dict[str, tuple[str, bool]] = {
    "headache": ("Headache", False),
    "nasal_congestion": ("Nasal congestion", False),
    "sore_throat": ("Sore throat", False),
    "cough": ("Cough", False),
    "nausea": ("Nausea", False),
    "back_pain": ("Back pain", False),
    "muscle_pain": ("Muscle or joint pain", False),
    "eye_irritation": ("Eye irritation", False),
    "vision_change": ("Change in vision", False),
    "skin_irritation": ("Skin irritation or rash", False),
    "insomnia": ("Trouble sleeping", False),
    "dizziness": ("Dizziness", False),
    "anxiety": ("Anxiety", False),
    "low_appetite": ("Low appetite", False),
    "chest_pain": ("Chest pain", True),
    "shortness_of_breath": ("Shortness of breath", True),
    "fainting": ("Fainting or near-fainting", True),
    "severe_headache": ("Sudden severe headache", True),
}
URGENT_SYMPTOMS = {code for code, (_, urgent) in SYMPTOMS.items() if urgent}

SCALE_LABELS = {1: "very low", 2: "low", 3: "moderate", 4: "high", 5: "very high"}

STRESS_BASE = {"A001": 2.2, "A002": 2.0, "A003": 2.4, "A004": 2.3}
FATIGUE_BASE = {"A001": 2.1, "A002": 2.0, "A003": 2.2, "A004": 2.2}


@dataclass(frozen=True)
class SymptomRule:
    astronaut_id: str | None  # None = everyone
    start_day: int
    end_day: int
    probabilities: dict[str, float]


SYMPTOM_RULES = [
    SymptomRule(None, 0, 4, {"nausea": 0.45, "headache": 0.4, "nasal_congestion": 0.5}),
    SymptomRule(None, 0, 20, {"back_pain": 0.3}),
    SymptomRule("A002", 46, 55, {"nasal_congestion": 0.85, "sore_throat": 0.7, "cough": 0.6, "headache": 0.5, "low_appetite": 0.6}),
    SymptomRule("A004", 100, 128, {"insomnia": 0.75, "anxiety": 0.35, "headache": 0.3}),
    SymptomRule("A001", 120, 150, {"muscle_pain": 0.55, "back_pain": 0.45}),
    SymptomRule("A003", 140, 180, {"headache": 0.3}),
]
BACKGROUND = {"headache": 0.03, "eye_irritation": 0.02, "skin_irritation": 0.02, "nasal_congestion": 0.04, "muscle_pain": 0.03}


def _bump(d: int, start: int, end: int, ramp: int) -> float:
    if d < start - ramp or d > end + ramp:
        return 0.0
    if d < start:
        return 1 - (start - d) / (ramp + 1)
    if d > end:
        return 1 - (d - end) / (ramp + 1)
    return 1.0


def _scale(x: float) -> int:
    return int(min(max(round(x), 1), 5))


def generate_checkins(records: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed * 7919 + 1)
    pre_sleep: dict[str, list[float]] = {}
    for r in records:
        if r["mission_day"] < 0:
            pre_sleep.setdefault(r["astronaut_id"], []).append(r["sleep_hours"])
    baseline_sleep = {k: sum(v) / len(v) for k, v in pre_sleep.items()}

    checkins = []
    for r in records:
        aid, d = r["astronaut_id"], r["mission_day"]
        if rng.random() < 0.10:
            continue
        sleep_gap = baseline_sleep[aid] - r["sleep_hours"]
        stress = (
            STRESS_BASE[aid]
            + 0.3 * _bump(d, 0, 10, 3)            # launch and adaptation
            + 0.4 * _bump(d, 95, 130, 15)         # third-quarter dip
            + 0.25 * max(sleep_gap, 0)
            + rng.gauss(0, 0.45)
        )
        fatigue = FATIGUE_BASE[aid] + 0.6 * max(sleep_gap, 0) + 0.3 * _bump(d, 0, 10, 3) + rng.gauss(0, 0.45)
        if aid == "A002":
            fatigue += 2.0 * _bump(d, 47, 53, 2)
            stress += 0.6 * _bump(d, 47, 53, 2)
        if aid == "A004":
            stress += 1.6 * _bump(d, 100, 128, 4)
            fatigue += 1.2 * _bump(d, 100, 128, 4)
        if aid == "A001":
            fatigue += 0.8 * _bump(d, 120, 150, 5)
            stress += 0.4 * _bump(d, 120, 150, 5)

        probabilities = dict(BACKGROUND)
        for rule in SYMPTOM_RULES:
            if (rule.astronaut_id in (None, aid)) and rule.start_day <= d <= rule.end_day:
                for code, p in rule.probabilities.items():
                    probabilities[code] = max(probabilities.get(code, 0.0), p)
        symptoms = sorted(code for code, p in probabilities.items() if rng.random() < p)

        checkins.append({
            "astronaut_id": aid,
            "mission_id": r["mission_id"],
            "mission_day": d,
            "stress": _scale(stress),
            "fatigue": _scale(fatigue),
            "symptoms": symptoms,
        })
    return checkins
