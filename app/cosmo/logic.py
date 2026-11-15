"""Rules of the CosmoMedic app layer: daily summary, body areas, score and alerts.

Everything here is derived from the astronaut's logs with explicit thresholds
(RULE_DERIVED). The thresholds follow docs/data/data-model.json of the
frontend where it gives one (bone −1.5 %, strength/aerobic −10 %) and generic
screening values elsewhere. Illustrative, not medical criteria.

Domains (each ok | warn | alert | unknown) and where they show on the body:

    head  ← mood, sleep, head/neck injuries
    torso ← blood pressure, nutrition, back injuries
    armL  ← muscle (strength, muscle mass), left arm/hand injuries
    armR  ← radiation (illustrative), right arm/hand injuries
    legL  ← bone density, fitness (aerobic, exercise adherence), left leg/knee/foot injuries
    legR  ← same, right side
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.cosmo.models import AppAstronaut, AppLog, AppMission

LEVEL_ORDER = {"unknown": 0, "ok": 1, "warn": 2, "alert": 3}
PENALTY = {"warn": 5, "alert": 10}
COUNTED_TYPES = ["mood", "sleep", "radiation", "measurement", "meal", "exercise", "meditation"]  # injury not counted
REQUIRED_FOR_SCORE = 6  # meditation (Relax) is "coming soon"
COINS_PER_LOG = 10
WINDOW = 7
STRESS_TAGS = {"anxious", "overloaded", "stressed"}


# --- mission clock ---------------------------------------------------------------------


def mission(session: Session) -> AppMission | None:
    return session.scalar(select(AppMission).limit(1))


def today(session: Session, m: AppMission | None = None) -> int:
    """Mission day used as "today": APP_MISSION_DAY, "auto" (from the calendar) or mission.today."""
    m = m or mission(session)
    if m is None:
        return 1
    setting = get_settings().app_mission_day
    if setting == "auto":
        start = date.fromisoformat(m.start_date)
        day = (datetime.now(timezone.utc).date() - start).days + 1
    elif setting:
        day = int(setting)
    else:
        day = m.today
    return max(1, min(day, m.length_days))


def worst(levels) -> str:
    result = "unknown"
    for level in levels:
        if LEVEL_ORDER[level] > LEVEL_ORDER[result]:
            result = level
    return result


# --- evaluation of one day ------------------------------------------------------------


@dataclass
class DomainResult:
    level: str = "unknown"
    detail: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


@dataclass
class DayEvaluation:
    astronaut_id: str
    day: int
    logs_done: list[str]
    domains: dict[str, DomainResult]
    areas: dict[str, str]
    score: int
    label: str


def baseline_delta(astronaut: AppAstronaut, metric: str, value: float) -> float | None:
    key = {"bone_density": "boneDensity", "muscle_mass": "muscleMassKg",
           "strength": "strengthScore", "aerobic": "aerobicScore"}.get(metric)
    base = (astronaut.baseline or {}).get(key) if key else None
    if not base:
        return None
    return round((value / float(base) - 1) * 100, 1)


def _latest_measurement(logs: list[AppLog], metric: str) -> AppLog | None:
    items = [l for l in logs if l.type == "measurement" and l.data.get("metric") == metric]
    return max(items, key=lambda l: (l.mission_day, l.timestamp)) if items else None


def evaluate_day(astronaut: AppAstronaut, all_logs: list[AppLog], day: int, goals: dict) -> DayEvaluation:
    logs = [l for l in all_logs if l.mission_day <= day]
    window = [l for l in logs if l.mission_day > day - WINDOW]
    window_len = min(WINDOW, day)
    by_type = lambda t, src=window: sorted((l for l in src if l.type == t), key=lambda l: (l.mission_day, l.timestamp))
    d: dict[str, DomainResult] = {}

    # Mood: average of the last 3 mood logs of the week.
    moods = by_type("mood")[-3:]
    if moods:
        avg = statistics.fmean(l.data["level"] for l in moods)
        stress_hits = sum(1 for l in by_type("mood") if STRESS_TAGS & set(l.data.get("tags", [])))
        level = "alert" if avg <= 1.7 else "warn" if avg <= 2.6 or stress_hits >= 3 else "ok"
        d["mood"] = DomainResult(level, {"avgLevel": round(avg, 2), "stressDays": stress_hits,
                                         "tags": sorted({t for l in moods for t in l.data.get("tags", [])})},
                                 [l.id for l in moods])
    else:
        d["mood"] = DomainResult()

    # Sleep: last 3 nights of the week.
    sleeps = by_type("sleep")[-3:]
    if sleeps:
        hours = [l.data["durationMin"] / 60 for l in sleeps]
        avg = statistics.fmean(hours)
        short = sum(1 for h in hours if h < 6)
        level = "alert" if avg < 5 else "warn" if avg < 6 or short >= 3 else "ok"
        d["sleep"] = DomainResult(level, {"avgHours": round(avg, 2), "shortNights": short,
                                          "goalHours": goals.get("sleepHours", 7)}, [l.id for l in sleeps])
    else:
        d["sleep"] = DomainResult()

    # Blood pressure: latest reading of the week.
    bp = [l for l in by_type("measurement") if l.data.get("metric") == "blood_pressure"]
    if bp:
        last = bp[-1]
        s, dia = last.data["values"]["systolic"], last.data["values"]["diastolic"]
        level = "alert" if s >= 140 or dia >= 90 else "warn" if s >= 130 or dia >= 85 else "ok"
        d["blood_pressure"] = DomainResult(level, {"systolic": s, "diastolic": dia, "day": last.mission_day}, [last.id])
    else:
        d["blood_pressure"] = DomainResult()

    # Muscle: latest strength or muscle mass change vs baseline (whole mission).
    muscle_logs = [m for m in (_latest_measurement(logs, "strength"), _latest_measurement(logs, "muscle_mass")) if m]
    if muscle_logs:
        deltas = {m.data["metric"]: m.data.get("deltaVsBaselinePct") for m in muscle_logs}
        worst_delta = min(v for v in deltas.values() if v is not None) if any(v is not None for v in deltas.values()) else 0
        level = "alert" if worst_delta <= -10 else "warn" if worst_delta <= -5 else "ok"
        d["muscle"] = DomainResult(level, {"deltaPct": deltas}, [m.id for m in muscle_logs])
    else:
        d["muscle"] = DomainResult()

    # Bone density: latest change vs baseline (data model: alert at −1.5 %).
    bone = _latest_measurement(logs, "bone_density")
    if bone and bone.data.get("deltaVsBaselinePct") is not None:
        delta = bone.data["deltaVsBaselinePct"]
        level = "alert" if delta <= -1.5 else "warn" if delta <= -1.0 else "ok"
        d["bone"] = DomainResult(level, {"deltaPct": delta, "day": bone.mission_day}, [bone.id])
    else:
        d["bone"] = DomainResult()

    # Fitness: aerobic change vs baseline (alert at −10 %) and exercise adherence this week.
    aerobic = _latest_measurement(logs, "aerobic")
    minutes_by_day = Counter()
    for l in by_type("exercise"):
        minutes_by_day[l.mission_day] += l.data["durationMin"]
    active_days = sum(1 for m in minutes_by_day.values() if m >= 60)
    ratio = active_days / window_len if window_len else 1
    adherence = "alert" if ratio < 0.3 else "warn" if ratio < 0.55 else "ok"
    aerobic_level = "unknown"
    detail = {"activeDays": active_days, "windowDays": window_len,
              "goalMinPerDay": goals.get("exerciseMinPerDay", 90)}
    evidence = [l.id for l in by_type("exercise")][-3:]
    if aerobic and aerobic.data.get("deltaVsBaselinePct") is not None:
        delta = aerobic.data["deltaVsBaselinePct"]
        aerobic_level = "alert" if delta <= -10 else "warn" if delta <= -8 else "ok"
        detail["aerobicDeltaPct"] = delta
        evidence = [aerobic.id] + evidence
    d["fitness"] = DomainResult(worst([adherence, aerobic_level]), detail | {"adherence": adherence,
                                                                             "aerobic": aerobic_level}, evidence)

    # Radiation (illustrative): today's personal dose vs the astronaut's own mission average.
    doses = [l for l in logs if l.type == "radiation" and l.data.get("source") == "personal"]
    today_doses = [l for l in doses if l.mission_day == day]
    earlier = [l.data["doseUsv"] for l in doses if l.mission_day < day]
    if today_doses and earlier:
        dose, avg = sum(l.data["doseUsv"] for l in today_doses), statistics.fmean(earlier)
        level = "alert" if dose > 2 * avg else "warn" if dose > 1.5 * avg else "ok"
        d["radiation"] = DomainResult(level, {"todayUsv": dose, "avgDailyUsv": round(avg, 1),
                                              "illustrative": True}, [l.id for l in today_doses])
    else:
        d["radiation"] = DomainResult()

    # Nutrition: intake vs goal and menu fatigue (taste ratings, the same dish again and again).
    meals = by_type("meal")
    if meals:
        kcal_by_day = Counter()
        for l in meals:
            kcal_by_day[l.mission_day] += l.data["kcal"]
        goal = goals.get("kcalPerDay", 2500)
        intake = statistics.fmean(kcal_by_day.values()) / goal
        taste = statistics.fmean(l.data["tasteRating"] for l in meals)
        repeated = Counter(l.data["name"].strip().lower() for l in meals).most_common(1)[0]
        # Menu fatigue: low taste ratings, made worse when the same dish keeps coming back.
        recent_taste = [l.data["tasteRating"] for l in meals[-4:]]
        fatigue = len(meals) >= 4 and (taste <= 2.6 or (repeated[1] >= 4 and statistics.fmean(recent_taste) <= 3))
        level = "alert" if intake < 0.5 else "warn" if intake < 0.65 or fatigue else "ok"
        d["nutrition"] = DomainResult(level, {"avgKcalPerDay": round(intake * goal), "goalKcal": goal,
                                              "intakePct": round(intake * 100), "avgTaste": round(taste, 2),
                                              "menuFatigue": fatigue, "mostRepeatedMeal": repeated[0],
                                              "mostRepeatedCount": repeated[1]}, [l.id for l in meals][-4:])
    else:
        d["nutrition"] = DomainResult()

    # Injuries: latest state of each injury reported (an injury log can be updated with PUT).
    injuries = [l for l in logs if l.type == "injury" and l.data.get("status") != "resolved"]
    injury_levels = []
    for l in injuries:
        sev, status = l.data["severity"], l.data.get("status", "active")
        lvl = "alert" if sev == "severe" else "warn" if sev == "moderate" or status == "active" else "ok"
        injury_levels.append((l, lvl))
    d["injury"] = DomainResult(worst([lvl for _, lvl in injury_levels]) if injury_levels else "unknown",
                               {"open": len(injuries)}, [l.id for l, _ in injury_levels])

    # Body areas for the hologram.
    def injuries_at(parts: set[str], sides: set[str]) -> list[str]:
        return [lvl for l, lvl in injury_levels if l.data["bodyPart"] in parts and l.data.get("side", "center") in sides]

    lv = lambda k: d[k].level
    areas = {
        "head": worst([lv("mood"), lv("sleep"), *injuries_at({"head", "neck"}, {"left", "right", "center"}), "ok"]),
        "torso": worst([lv("blood_pressure"), lv("nutrition"), *injuries_at({"back"}, {"left", "right", "center"}), "ok"]),
        "armL": worst([lv("muscle"), *injuries_at({"arm", "hand"}, {"left", "center"}), "ok"]),
        "armR": worst([lv("radiation"), *injuries_at({"arm", "hand"}, {"right", "center"}), "ok"]),
        "legL": worst([lv("bone"), lv("fitness"), *injuries_at({"leg", "knee", "foot"}, {"left", "center"}), "ok"]),
        "legR": worst([lv("bone"), lv("fitness"), *injuries_at({"leg", "knee", "foot"}, {"right", "center"}), "ok"]),
    }

    done = sorted({l.type for l in logs if l.mission_day == day and l.type in COUNTED_TYPES})
    health = max(0, 100 - sum(PENALTY.get(r.level, 0) for r in d.values()))
    adherence_score = min(1.0, len([t for t in done if t != "meditation"]) / REQUIRED_FOR_SCORE)
    score = round(0.8 * health + 20 * adherence_score)
    label = "stable" if score >= 75 else "attention" if score >= 60 else "alert"
    return DayEvaluation(astronaut.id, day, done, d, areas, score, label)


# --- alerts in the app's format --------------------------------------------------------

ALERT_TEXT = {
    "poor_sleep": lambda x: f"Sleep averaged {x['avgHours']:.1f} h over the last nights (goal {x['goalHours']} h); "
                            f"{x['shortNights']} night(s) under 6 h.",
    "low_mood": lambda x: f"Mood averaged {x['avgLevel']:.1f}/5 over the last check-ins"
                          + (f" ({', '.join(x['tags'])})." if x.get("tags") else "."),
    "bone_loss": lambda x: f"Bone density {x['deltaPct']:+.1f}% vs. baseline (NASA reference: −1 to −1.5% per month).",
    "menu_fatigue": lambda x: (f"Intake {x['intakePct']}% of the {x['goalKcal']} kcal goal; average taste "
                               f"{x['avgTaste']:.1f}/5" + (f"; \"{x['mostRepeatedMeal']}\" {x['mostRepeatedCount']} times."
                                                           if x["mostRepeatedCount"] >= 4 else ".")),
}


def alert_candidates(ev: DayEvaluation, logs: list[AppLog]) -> list[dict]:
    """Alerts the rules raise for this day: type, level, text, evidence."""
    d, out = ev.domains, []
    lv = {"warn": "medium", "alert": "high"}
    if d["sleep"].level in lv:
        out.append({"type": "poor_sleep", "level": lv[d["sleep"].level],
                    "text": ALERT_TEXT["poor_sleep"](d["sleep"].detail), "evidence": d["sleep"].evidence})
    if d["mood"].level in lv:
        out.append({"type": "low_mood", "level": lv[d["mood"].level],
                    "text": ALERT_TEXT["low_mood"](d["mood"].detail), "evidence": d["mood"].evidence})
    if d["bone"].level in ("warn", "alert"):
        out.append({"type": "bone_loss", "level": {"warn": "low", "alert": "medium"}[d["bone"].level],
                    "text": ALERT_TEXT["bone_loss"](d["bone"].detail), "evidence": d["bone"].evidence})
    fitness_level = worst([d["fitness"].detail.get("aerobic", "unknown"), d["muscle"].level])
    if fitness_level in lv:
        parts = []
        if "aerobicDeltaPct" in d["fitness"].detail:
            parts.append(f"aerobic {d['fitness'].detail['aerobicDeltaPct']:+.0f}%")
        for metric, delta in (d["muscle"].detail.get("deltaPct") or {}).items():
            if delta is not None:
                parts.append(f"{metric.replace('_', ' ')} {delta:+.0f}%")
        out.append({"type": "fitness_drop", "level": lv[fitness_level],
                    "text": f"Fitness vs. baseline: {', '.join(parts)} (NASA average loss 10–12%).",
                    "evidence": d["fitness"].evidence + d["muscle"].evidence})
    elif d["fitness"].detail.get("adherence") in lv:
        x = d["fitness"].detail
        out.append({"type": "fitness_drop", "level": "low",
                    "text": f"Exercise of 60 min or more on {x['activeDays']} of the last {x['windowDays']} days "
                            f"(goal {x['goalMinPerDay']} min/day).", "evidence": d["fitness"].evidence})
    if d["nutrition"].level in ("warn", "alert"):
        out.append({"type": "menu_fatigue", "level": {"warn": "low", "alert": "medium"}[d["nutrition"].level],
                    "text": ALERT_TEXT["menu_fatigue"](d["nutrition"].detail), "evidence": d["nutrition"].evidence})
    for log_id in d["injury"].evidence:
        log = next(l for l in logs if l.id == log_id)
        sev = log.data["severity"]
        level = {"severe": "high", "moderate": "medium"}.get(sev, "low" if log.data.get("status") == "active" else None)
        if level:
            where = f"{log.data.get('side', '')} {log.data['bodyPart']}".strip()
            out.append({"type": "injury", "level": level, "detail": log.id,
                        "text": f"{where.capitalize()} {log.data['kind']} ({sev}) still {log.data.get('status', 'active')}.",
                        "evidence": [log.id]})
    return out


def avatar_state(ev: DayEvaluation) -> str:
    """Drives the Bolt animation: idle | walking | happy | tired | sleepy | sad."""
    if ev.domains["mood"].level in ("warn", "alert"):
        return "sad"
    if ev.domains["sleep"].level in ("warn", "alert"):
        return "sleepy"
    if ev.score < 60:
        return "tired"
    if ev.score >= 85:
        return "happy"
    return "idle"
