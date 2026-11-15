"""Pattern detection over a rolling 7-day window.

For each astronaut and day D, the detectors look at days D-6 … D of daily vitals
and check-ins and report patterns with a severity (low, medium, high). The rules
are deliberately simple and explainable; every finding carries its evidence.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CheckIn, DailyRecord, PatternDetection, VitalBaseline
from app.synthetic.checkins import SYMPTOMS, URGENT_SYMPTOMS

WINDOW_DAYS = 7
MIN_DAYS = 4  # minimum days with data in the window for a daily-metric rule
SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}

PATTERN_LABELS = {
    "sleep_debt": "Short sleep",
    "elevated_stress": "Elevated stress",
    "fatigue": "Persistent fatigue",
    "low_mood": "Low mood",
    "wellbeing_cluster": "Short sleep, stress and low mood together",
    "fever": "Fever",
    "rising_blood_pressure": "Raised blood pressure",
    "elevated_glucose": "Raised fasting glucose",
    "elevated_heart_rate": "Resting heart rate above personal baseline",
    "low_intake": "Low food intake",
    "recurring_symptom": "Recurring symptom",
    "urgent_symptom": "Urgent symptom reported",
    "missed_checkins": "Few check-ins",
}


@dataclass
class Finding:
    pattern: str
    severity: str
    summary: str
    evidence: dict = field(default_factory=dict)
    detail: str = ""


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _slope(points: list[tuple[int, float]]) -> float | None:
    if len(points) < 3:
        return None
    xs, ys = zip(*points)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else None


def detect_window(
    day: int,
    records: list[dict],
    checkins: list[dict],
    baselines: dict[str, dict],
) -> list[Finding]:
    """Run every rule on the 7 days ending at `day`. Inputs are already filtered to that window."""
    findings: list[Finding] = []
    days = WINDOW_DAYS

    def values(metric: str) -> list[float]:
        return [r[metric] for r in records if r.get(metric) is not None]

    def base(metric: str) -> float | None:
        return (baselines.get(metric) or {}).get("mean")

    # --- sleep ---------------------------------------------------------------
    sleep = values("sleep_hours")
    if len(sleep) >= MIN_DAYS:
        mean, short = _mean(sleep), sum(1 for v in sleep if v < 6)
        if mean < 5 or short >= 6:
            sev = "high"
        elif mean < 6 or short >= 4:
            sev = "medium"
        else:
            sev = None
        if sev:
            findings.append(Finding("sleep_debt", sev,
                f"Sleep averaged {mean:.1f} h over the last {days} days; {short} nights under 6 h.",
                {"meanHours": round(mean, 2), "nightsUnder6h": short, "baselineHours": base("sleep_hours")}))

    # --- check-in scores -----------------------------------------------------
    stress = [(c["mission_day"], c["stress"]) for c in checkins]
    if len(stress) >= 3:
        mean = _mean([v for _, v in stress])
        slope = _slope(stress)
        if mean >= 4.2:
            sev = "high"
        elif mean >= 3.5:
            sev = "medium"
        elif mean >= 3.0 and slope is not None and slope >= 0.2:
            sev = "low"
        else:
            sev = None
        if sev:
            findings.append(Finding("elevated_stress", sev,
                f"Self-reported stress averaged {mean:.1f}/5 over the last {days} days.",
                {"meanScore": round(mean, 2), "trendPerDay": round(slope, 2) if slope is not None else None,
                 "checkins": len(stress)}))

    fatigue = [c["fatigue"] for c in checkins]
    if len(fatigue) >= 3:
        mean = _mean(fatigue)
        sev = "high" if mean >= 4.2 else "medium" if mean >= 3.5 else None
        if sev:
            findings.append(Finding("fatigue", sev,
                f"Self-reported fatigue averaged {mean:.1f}/5 over the last {days} days.",
                {"meanScore": round(mean, 2), "checkins": len(fatigue)}))

    mood = values("mood")
    if len(mood) >= MIN_DAYS:
        mean = _mean(mood)
        sev = "high" if mean <= 1.8 else "medium" if mean <= 2.5 else None
        if sev:
            findings.append(Finding("low_mood", sev,
                f"Mood averaged {mean:.1f}/5 over the last {days} days.",
                {"meanScore": round(mean, 2), "baselineScore": base("mood")}))

    behavioral = {f.pattern for f in findings if f.severity in ("medium", "high")}
    if {"sleep_debt", "elevated_stress", "low_mood"} <= behavioral:
        findings.append(Finding("wellbeing_cluster", "high",
            "Short sleep, high stress and low mood at the same time over the last 7 days.",
            {"patterns": sorted(behavioral)}))

    # --- vital signs -----------------------------------------------------------
    temps = values("body_temperature")
    if temps:
        fever_days = sum(1 for v in temps if v >= 38.0)
        warm_days = sum(1 for v in temps if v >= 37.5)
        sev = "high" if fever_days else "medium" if warm_days >= 2 else None
        if sev:
            findings.append(Finding("fever", sev,
                f"Body temperature peaked at {max(temps):.1f} °C; {fever_days} day(s) at 38 °C or more.",
                {"maxCelsius": max(temps), "daysAtOrAbove38": fever_days, "daysAtOrAbove37_5": warm_days}))

    sbp, dbp = values("systolic_bp"), values("diastolic_bp")
    if len(sbp) >= MIN_DAYS:
        high_days = sum(1 for s, d in zip(sbp, dbp) if s >= 140 or d >= 90)
        raised_days = sum(1 for s, d in zip(sbp, dbp) if s >= 135 or d >= 87)
        sev = "high" if high_days >= 3 else "medium" if raised_days >= 3 else None
        if sev:
            findings.append(Finding("rising_blood_pressure", sev,
                f"Blood pressure averaged {_mean(sbp):.0f}/{_mean(dbp):.0f} mmHg; "
                f"{high_days} day(s) at 140/90 or above.",
                {"meanSystolic": round(_mean(sbp), 1), "meanDiastolic": round(_mean(dbp), 1),
                 "daysAtOrAbove140_90": high_days, "daysAtOrAbove135_87": raised_days,
                 "baselineSystolic": base("systolic_bp")}))

    glucose = values("blood_glucose")
    if len(glucose) >= MIN_DAYS:
        mean = _mean(glucose)
        sev = "high" if max(glucose) >= 126 else "medium" if mean >= 110 else "low" if mean >= 100 else None
        if sev:
            findings.append(Finding("elevated_glucose", sev,
                f"Fasting glucose averaged {mean:.0f} mg/dL over the last {days} days.",
                {"meanMgDl": round(mean, 1), "max": max(glucose), "baselineMgDl": base("blood_glucose")}))

    hr = values("heart_rate")
    hr_base = baselines.get("heart_rate") or {}
    if len(hr) >= MIN_DAYS and hr_base.get("mean") is not None:
        mean = _mean(hr)
        z = (mean - hr_base["mean"]) / (hr_base.get("sd_effective") or 2.0)
        if mean - hr_base["mean"] >= 8 and z >= 2:
            findings.append(Finding("elevated_heart_rate", "medium",
                f"Resting heart rate averaged {mean:.0f} bpm, {mean - hr_base['mean']:.0f} bpm above pre-flight.",
                {"meanBpm": round(mean, 1), "baselineBpm": round(hr_base["mean"], 1), "z": round(z, 1)}))

    kcal = values("calorie_intake")
    if len(kcal) >= MIN_DAYS and base("calorie_intake"):
        pct = (_mean(kcal) / base("calorie_intake") - 1) * 100
        sev = "high" if pct <= -40 else "medium" if pct <= -25 else None
        if sev:
            findings.append(Finding("low_intake", sev,
                f"Calorie intake was {abs(pct):.0f}% below pre-flight over the last {days} days.",
                {"meanKcal": round(_mean(kcal)), "baselineKcal": round(base("calorie_intake")),
                 "pctVsBaseline": round(pct, 1)}))

    # --- symptoms and engagement -----------------------------------------------
    counts: dict[str, int] = {}
    for c in checkins:
        for s in c["symptoms"]:
            counts[s] = counts.get(s, 0) + 1
    for code, n in sorted(counts.items()):
        label = SYMPTOMS.get(code, (code, False))[0]
        if code in URGENT_SYMPTOMS:
            findings.append(Finding("urgent_symptom", "high",
                f"{label} reported {n} time(s) in the last {days} days.", {"symptom": code, "days": n}, detail=code))
        elif n >= 3:
            findings.append(Finding("recurring_symptom", "medium" if n >= 5 else "low",
                f"{label} reported on {n} of the last {days} days.", {"symptom": code, "days": n}, detail=code))

    if len(checkins) < 3:
        findings.append(Finding("missed_checkins", "low",
            f"Only {len(checkins)} check-in(s) in the last {days} days.", {"checkins": len(checkins)}))

    return findings


def run_detection(session: Session, mission_id: str, days: list[int]) -> list[PatternDetection]:
    """Detect patterns for every astronaut of the mission on each day in `days`; replaces earlier results."""
    if not days:
        return []
    first, last = min(days) - WINDOW_DAYS + 1, max(days)
    records_by: dict[str, list[dict]] = {}
    for r in session.scalars(
        select(DailyRecord).where(
            DailyRecord.mission_id == mission_id, DailyRecord.mission_day.between(first, last)
        )
    ):
        records_by.setdefault(r.astronaut_id, []).append({
            "mission_day": r.mission_day, "sleep_hours": r.sleep_hours, "mood": r.mood,
            "body_temperature": r.body_temperature, "systolic_bp": r.systolic_bp,
            "diastolic_bp": r.diastolic_bp, "blood_glucose": r.blood_glucose,
            "heart_rate": r.heart_rate, "calorie_intake": r.calorie_intake,
        })
    checkins_by: dict[str, list[dict]] = {}
    for c in session.scalars(
        select(CheckIn).where(CheckIn.mission_id == mission_id, CheckIn.mission_day.between(first, last))
    ):
        checkins_by.setdefault(c.astronaut_id, []).append(
            {"mission_day": c.mission_day, "stress": c.stress, "fatigue": c.fatigue, "symptoms": c.symptoms or []}
        )
    baselines_by: dict[str, dict[str, dict]] = {}
    for b in session.scalars(select(VitalBaseline)):
        baselines_by.setdefault(b.astronaut_id, {})[b.metric] = {"mean": b.mean, "sd_effective": b.sd_effective}

    astronauts = sorted(set(records_by) | set(checkins_by))
    session.execute(
        delete(PatternDetection).where(
            PatternDetection.mission_id == mission_id,
            PatternDetection.astronaut_id.in_(astronauts),
            PatternDetection.as_of_day.in_(days),
        )
    )
    stored = []
    for aid in astronauts:
        recs, chks = records_by.get(aid, []), checkins_by.get(aid, [])
        for day in sorted(days):
            lo = day - WINDOW_DAYS + 1
            window_records = [r for r in recs if lo <= r["mission_day"] <= day]
            if not window_records:
                continue
            window_checkins = [c for c in chks if lo <= c["mission_day"] <= day]
            for f in detect_window(day, window_records, window_checkins, baselines_by.get(aid, {})):
                det = PatternDetection(
                    astronaut_id=aid, mission_id=mission_id, as_of_day=day, window_start=lo, window_end=day,
                    pattern=f.pattern, detail=f.detail, severity=f.severity, summary=f.summary, evidence=f.evidence,
                )
                session.add(det)
                stored.append(det)
    session.flush()
    return stored
