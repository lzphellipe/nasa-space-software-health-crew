"""Builders for the CosmoMedic screens: home, status, log overviews, profile, avatar."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cosmo import logic
from app.cosmo.models import AppAlert, AppAstronaut, AppGroundMessage, AppJournalEntry, AppLog, AppMission, AppShopItem
from app.cosmo.schemas import AlertOut, DailySummaryOut, GroundMessageOut, LogOut
from app.cosmo.service import alert_provenance, astronaut_logs, evaluate, log_provenance, message_status
from app.models import ProactiveMessage
from app.provenance import SIMULATED, USER

MODULES = {
    "mood": {"title": "Mood check-in", "types": ["mood"], "route": "/log/mood"},
    "sleep": {"title": "Sleep", "types": ["sleep"], "route": "/log/sleep"},
    "radiation": {"title": "Radiation", "types": ["radiation"], "route": "/log/radiation"},
    "meals": {"title": "Meals", "types": ["meal"], "route": "/log/meals"},
    "exercise": {"title": "Exercise", "types": ["exercise"], "route": "/log/exercise"},
    "body": {"title": "Body", "types": ["measurement", "injury"], "route": "/log/body"},
    "relax": {"title": "Relax", "types": ["meditation"], "route": "/log/relax", "comingSoon": True},
}
WEEKLY_METRICS = ["bone_density", "muscle_mass", "strength", "aerobic"]


def log_out(l: AppLog) -> LogOut:
    return LogOut(id=l.id, astronaut_id=l.astronaut_id, type=l.type, timestamp=l.timestamp, mission_day=l.mission_day,
                  environment_id=l.environment_id, note=l.note, coins_earned=l.coins_earned, data=l.data,
                  provenance=log_provenance(l))


def alert_out(a: AppAlert) -> AlertOut:
    return AlertOut(id=a.id, astronaut_id=a.astronaut_id, type=a.type, level=a.level, text=a.text,
                    evidence=a.evidence, timestamp=a.timestamp, mission_day=a.mission_day,
                    sent_to_ground=a.sent_to_ground, status=a.status, provenance=alert_provenance(a))


def message_out(m: AppGroundMessage) -> GroundMessageOut:
    return GroundMessageOut(id=m.id, astronaut_id=m.astronaut_id, sender=m.sender, text=m.text, urgent=m.urgent,
                            sent_at=m.sent_at, estimated_arrival_at=m.estimated_arrival_at,
                            status=message_status(m), attachments=m.attachments,
                            provenance=SIMULATED if m.source == "seed" else USER)


def summary_out(ev: logic.DayEvaluation) -> DailySummaryOut:
    return DailySummaryOut(
        astronaut_id=ev.astronaut_id, mission_day=ev.day, logs_done=ev.logs_done, logs_done_count=len(ev.logs_done),
        logs_total=len(logic.COUNTED_TYPES), overall_score=ev.score, status_label=ev.label, areas=ev.areas,
        domains={k: v.level for k, v in ev.domains.items()},
    )


def mission_out(m: AppMission, day: int) -> dict:
    return {"id": m.id, "name": m.name, "destination": m.destination, "startDate": m.start_date,
            "lengthDays": m.length_days, "today": day, "commDelaySec": m.comm_delay_sec, "goals": m.goals,
            "environments": m.environments, "provenance": SIMULATED}


def iso(dt) -> str | None:
    """UTC timestamp with a trailing Z, like the rest of the API."""
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def fmt_minutes(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _day_logs(logs: list[AppLog], day: int, types: list[str]) -> list[AppLog]:
    return sorted((l for l in logs if l.mission_day == day and l.type in types), key=lambda l: l.timestamp)


def streak(logs: list[AppLog], day: int) -> int:
    days = {l.mission_day for l in logs if l.type == "mood"}
    count, d = 0, day
    if d not in days:
        d -= 1  # today's check-in may still be pending
    while d in days:
        count += 1
        d -= 1
    return count


def measurements_state(astronaut: AppAstronaut, logs: list[AppLog], day: int) -> list[dict]:
    rows = []
    for metric in ["blood_pressure", *WEEKLY_METRICS]:
        items = sorted((l for l in logs if l.type == "measurement" and l.data.get("metric") == metric and l.mission_day <= day),
                       key=lambda l: (l.mission_day, l.timestamp))
        last = items[-1] if items else None
        cadence = "daily" if metric == "blood_pressure" else "weekly"
        if last is None:
            state = "due_today" if cadence == "daily" else "not_logged"
        elif last.mission_day == day:
            state = "logged_today"
        elif cadence == "daily" or day - last.mission_day >= 7:
            state = "due_today"
        else:
            state = "up_to_date"
        rows.append({
            "metric": metric, "cadence": cadence, "state": state,
            "lastLoggedDay": last.mission_day if last else None,
            "latest": last.data["values"] if last else None,
            "deltaVsBaselinePct": last.data.get("deltaVsBaselinePct") if last else None,
            "trend": [{"missionDay": l.mission_day, "deltaVsBaselinePct": l.data.get("deltaVsBaselinePct"),
                       "values": l.data["values"]} for l in items[-8:]],
        })
    return rows


def module_detail(module: str, astronaut: AppAstronaut, logs: list[AppLog], day: int, goals: dict) -> str:
    today_logs = _day_logs(logs, day, MODULES[module]["types"])
    if module == "mood" and today_logs:
        last = today_logs[-1].data
        return " · ".join([last["label"].capitalize(), *[t.capitalize() for t in last.get("tags", [])[:2]]])
    if module == "sleep" and today_logs:
        last = today_logs[-1].data
        return f"{fmt_minutes(last['durationMin'])} · quality {last['quality']}/5"
    if module == "radiation" and today_logs:
        total = sum(l.data["doseUsv"] for l in today_logs)
        return f"{total:g} μSv · {'personal dosimeter' if today_logs[-1].data['source'] == 'personal' else 'area monitor'}"
    if module == "meals":
        kcal = sum(l.data["kcal"] for l in today_logs)
        return f"{kcal:,.0f} of {goals.get('kcalPerDay', 2500):,} kcal"
    if module == "exercise":
        minutes = sum(l.data["durationMin"] for l in today_logs)
        return f"{minutes} of {goals.get('exerciseMinPerDay', 90)} min today"
    if module == "body":
        current = sum(1 for r in measurements_state(astronaut, logs, day) if r["state"] in ("logged_today", "up_to_date"))
        return f"{current} of 5 measurements current"
    if module == "relax":
        return "Breathing · Meditation · Stretch"
    return "Not logged today"


def module_done(module: str, logs: list[AppLog], day: int) -> bool:
    types = [t for t in MODULES[module]["types"] if t != "injury"]
    return bool(_day_logs(logs, day, types))


def button_label(module: str, done: bool) -> str:
    if module == "relax":
        return "Coming soon"
    if not done:
        return f"Log today +{logic.COINS_PER_LOG} 🪙"
    return "✓ Logged today · Add" if module in ("meals", "exercise", "body") else "✓ Logged today · Edit"


def latest_orbi_note(session: Session, astronaut_id: str) -> ProactiveMessage | None:
    return session.scalar(select(ProactiveMessage).where(
        ProactiveMessage.astronaut_id == astronaut_id, ProactiveMessage.status == "unread")
        .order_by(ProactiveMessage.mission_day.desc(), ProactiveMessage.id.desc()).limit(1))


def home(session: Session, astronaut: AppAstronaut, m: AppMission, day: int) -> dict:
    logs = astronaut_logs(session, astronaut.id)
    ev = evaluate(session, astronaut, day, m, logs)
    note = latest_orbi_note(session, astronaut.id)
    unread = [g for g in session.scalars(select(AppGroundMessage).where(
        AppGroundMessage.astronaut_id == astronaut.id, AppGroundMessage.sender == "ground",
        AppGroundMessage.read_at.is_(None))) if message_status(g) == "delivered"]
    items = []
    for key, cfg in MODULES.items():
        done = module_done(key, logs, day)
        items.append({"module": key, "title": cfg["title"], "route": cfg["route"], "done": done,
                      "status": "Explore" if key == "relax" else "Done" if done else "Log now",
                      "detail": module_detail(key, astronaut, logs, day, m.goals),
                      "comingSoon": cfg.get("comingSoon", False)})
    return {
        "astronaut": {"id": astronaut.id, "name": astronaut.name, "firstName": astronaut.name.split()[0],
                      "crewId": astronaut.crew_id},
        "mission": {"id": m.id, "name": m.name, "missionDay": day, "lengthDays": m.length_days,
                    "progressPct": round(day / m.length_days * 100)},
        "coins": astronaut.avatar.get("coins", 0),
        "logs": items,
        "logsDoneCount": len(ev.logs_done),
        "logsTotal": len(logic.COUNTED_TYPES),
        "overallScore": ev.score,
        "statusLabel": ev.label,
        "avatarState": logic.avatar_state(ev),
        "orbi": ({"id": note.id, "title": note.title, "text": note.body.split("\n\n")[0], "types": note.patterns}
                 if note else None),
        "unreadGroundMessages": len(unread),
        "provenance": "RULE_DERIVED",
    }


def status(session: Session, astronaut: AppAstronaut, m: AppMission, day: int) -> dict:
    logs = astronaut_logs(session, astronaut.id)
    ev = evaluate(session, astronaut, day, m, logs)
    days = [summary_out(evaluate(session, astronaut, d, m, logs)).model_dump(mode="json", by_alias=True)
            for d in range(max(1, day - 6), day + 1)]
    dom = ev.domains
    upto = [l for l in logs if l.mission_day <= day]

    def latest(t, pred=lambda l: True):
        items = [l for l in upto if l.type == t and pred(l)]
        return max(items, key=lambda l: (l.mission_day, l.timestamp)) if items else None

    mood = latest("mood")
    stress_tags = logic.STRESS_TAGS & set(mood.data.get("tags", [])) if mood else set()
    bp = latest("measurement", lambda l: l.data.get("metric") == "blood_pressure")
    sleep = latest("sleep")
    rad_today = sum(l.data["doseUsv"] for l in upto if l.type == "radiation" and l.mission_day == day
                    and l.data.get("source") == "personal")
    bone = latest("measurement", lambda l: l.data.get("metric") == "bone_density")
    muscle = latest("measurement", lambda l: l.data.get("metric") in ("strength", "muscle_mass"))
    ex_today = sum(l.data["durationMin"] for l in upto if l.type == "exercise" and l.mission_day == day)
    goal = m.goals.get("exerciseMinPerDay", 90)
    pct = lambda v: f"{v:+.1f}%".replace("-", "−") if v is not None else "—"

    callouts = [
        {"key": "mood", "title": "Mood", "area": "head", "level": dom["mood"].level, "route": "/log/mood",
         "logged": bool(mood and mood.mission_day == day), "value": mood.data["label"].capitalize() if mood else "+ Log today"},
        {"key": "stress", "title": "Stress", "area": "head", "level": "warn" if stress_tags else "ok", "route": "/orbi",
         "logged": bool(mood), "value": "High" if stress_tags else "Low" if mood else "—"},
        {"key": "blood_pressure", "title": "Blood pressure", "area": "torso", "level": dom["blood_pressure"].level,
         "route": "/log/body", "logged": bool(bp and bp.mission_day == day),
         "value": f"{bp.data['values']['systolic']} / {bp.data['values']['diastolic']}" if bp and bp.mission_day == day else "+ Log today"},
        {"key": "radiation", "title": "Radiation*", "area": "armR", "level": dom["radiation"].level, "route": "/log/radiation",
         "logged": rad_today > 0, "value": f"{rad_today:g} μSv" if rad_today else "+ Log today", "illustrative": True},
        {"key": "muscle", "title": "Muscle", "area": "armL", "level": dom["muscle"].level, "route": "/log/body",
         "logged": bool(muscle), "value": pct(muscle.data.get("deltaVsBaselinePct")) if muscle else "—"},
        {"key": "sleep", "title": "Sleep · wrist band", "area": "wrist", "level": dom["sleep"].level, "route": "/log/sleep",
         "logged": bool(sleep and sleep.mission_day == day),
         "value": fmt_minutes(sleep.data["durationMin"]) if sleep else "+ Log today"},
        {"key": "exercise", "title": "Exercise", "area": "legs", "level": dom["fitness"].level, "route": "/log/exercise",
         "logged": ex_today > 0, "value": f"{ex_today} / {goal} min"},
        {"key": "bone_density", "title": "Bone density", "area": "legs", "level": dom["bone"].level, "route": "/log/body",
         "logged": bool(bone), "value": pct(bone.data.get("deltaVsBaselinePct")) if bone else "—"},
    ]
    return {
        "astronautId": astronaut.id, "missionDay": day, "overallScore": ev.score, "statusLabel": ev.label,
        "logsDoneCount": len(ev.logs_done), "logsTotal": len(logic.COUNTED_TYPES),
        "streak": streak(logs, day), "areas": ev.areas, "domains": {k: v.level for k, v in dom.items()},
        "callouts": callouts, "days": days, "coins": astronaut.avatar.get("coins", 0),
        "provenance": "RULE_DERIVED",
    }


def overview(session: Session, astronaut: AppAstronaut, m: AppMission, day: int, module: str) -> dict:
    cfg = MODULES[module]
    logs = astronaut_logs(session, astronaut.id)
    ev = evaluate(session, astronaut, day, m, logs)
    types = cfg["types"]
    today_logs = _day_logs(logs, day, types)
    week_days = list(range(max(1, day - 6), day + 1))
    done = module_done(module, logs, day)
    out = {"module": module, "title": cfg["title"], "missionDay": day, "loggedToday": done,
           "buttonLabel": button_label(module, done), "comingSoon": cfg.get("comingSoon", False),
           "today": [log_out(l).model_dump(mode="json", by_alias=True) for l in today_logs]}
    goals = m.goals

    if module == "mood":
        week = []
        for d in week_days:
            items = _day_logs(logs, d, ["mood"])
            week.append({"missionDay": d, "level": items[-1].data["level"] if items else None,
                         "label": items[-1].data["label"] if items else None})
        levels = [w["level"] for w in week if w["level"]]
        best = max((w for w in week if w["level"]), key=lambda w: w["level"], default=None)
        journal = session.scalars(select(AppJournalEntry).where(AppJournalEntry.astronaut_id == astronaut.id)
                                  .order_by(AppJournalEntry.timestamp.desc())).all()
        out["stats"] = {"week": week, "average": round(statistics.fmean(levels), 2) if levels else None,
                        "bestDay": best["missionDay"] if best else None, "checkIns": len(levels),
                        "streak": streak(logs, day), "level": ev.domains["mood"].level,
                        "sharedWithOrbi": [{"id": j.id, "timestamp": iso(j.timestamp), "summary": j.summary,
                                            "moodLevel": j.mood_level} for j in journal]}
    elif module == "sleep":
        last = [l for l in logs if l.type == "sleep" and l.mission_day <= day]
        last = max(last, key=lambda l: (l.mission_day, l.timestamp)) if last else None
        week = [{"missionDay": d, "durationMin": sum(l.data["durationMin"] for l in _day_logs(logs, d, ["sleep"])) or None}
                for d in week_days]
        mins = [w["durationMin"] for w in week if w["durationMin"]]
        out["stats"] = {"lastNight": last.data | {"missionDay": last.mission_day} if last else None,
                        "week": week, "goalHours": goals.get("sleepHours", 7),
                        "averageHours": round(statistics.fmean(mins) / 60, 2) if mins else None,
                        "level": ev.domains["sleep"].level,
                        "tip": "The ISS sees about 16 sunrises a day: keep a fixed sleep window and dim lights before bed."}
    elif module == "radiation":
        rads = [l for l in logs if l.type == "radiation" and l.mission_day <= day]
        personal = [l for l in rads if l.data.get("source") == "personal"]
        week = [{"missionDay": d, "doseUsv": sum(l.data["doseUsv"] for l in personal if l.mission_day == d) or None}
                for d in week_days]
        area = [l for l in rads if l.data.get("source") == "area"]
        out["stats"] = {"missionTotalUsv": round(sum(l.data["doseUsv"] for l in personal), 1), "week": week,
                        "todayUsv": sum(l.data["doseUsv"] for l in personal if l.mission_day == day),
                        "areaMonitor": log_out(max(area, key=lambda l: l.timestamp)).model_dump(mode="json", by_alias=True) if area else None,
                        "particleMix": dict(Counter(l.data.get("particle", "total") for l in rads)),
                        "recent": [log_out(l).model_dump(mode="json", by_alias=True)
                                   for l in sorted(rads, key=lambda l: l.timestamp, reverse=True)[:5]],
                        "illustrative": True,
                        "note": "Illustrative values: NASA sets no fixed daily limit for long missions."}
    elif module == "meals":
        kcal_today = sum(l.data["kcal"] for l in today_logs)
        macros = {k: round(sum(l.data.get(k, 0) for l in today_logs), 1) for k in ("proteinG", "carbsG", "fatG")}
        week = [{"missionDay": d, "kcal": sum(l.data["kcal"] for l in _day_logs(logs, d, ["meal"])) or None}
                for d in week_days]
        nut = ev.domains["nutrition"].detail
        out["stats"] = {"kcalToday": kcal_today, "goalKcal": goals.get("kcalPerDay", 2500), "macrosToday": macros,
                        "week": week, "averageTaste": nut.get("avgTaste"), "menuFatigue": nut.get("menuFatigue", False),
                        "mostRepeatedMeal": nut.get("mostRepeatedMeal"), "level": ev.domains["nutrition"].level}
    elif module == "exercise":
        goal = goals.get("exerciseMinPerDay", 90)
        week = []
        for d in week_days:
            minutes = sum(l.data["durationMin"] for l in _day_logs(logs, d, ["exercise"]))
            week.append({"missionDay": d, "minutes": minutes,
                         "state": "today" if d == day else "done" if minutes >= 60 else "missed"})
        out["stats"] = {"minutesToday": sum(l.data["durationMin"] for l in today_logs), "goalMinPerDay": goal,
                        "goalDaysPerWeek": goals.get("exerciseDaysPerWeek", 6), "week": week,
                        "weekAverageMin": round(statistics.fmean(w["minutes"] for w in week)),
                        "level": ev.domains["fitness"].level}
    elif module == "body":
        injuries = sorted((l for l in logs if l.type == "injury" and l.mission_day <= day),
                          key=lambda l: l.timestamp, reverse=True)
        out["stats"] = {"measurements": measurements_state(astronaut, logs, day),
                        "baseline": astronaut.baseline, "lossReferencePct": -10,
                        "injuries": [log_out(l).model_dump(mode="json", by_alias=True) for l in injuries]}
    elif module == "relax":
        out["stats"] = {"comingSoon": True, "sessions": [log_out(l).model_dump(mode="json", by_alias=True)
                                                         for l in logs if l.type == "meditation"]}
    out["provenance"] = "RULE_DERIVED"
    return out


def leaderboard(session: Session, by: str, me: str | None) -> list[dict]:
    astronauts = session.scalars(select(AppAstronaut)).all()
    counts = Counter(l.astronaut_id for l in session.scalars(select(AppLog)))
    value = (lambda a: counts.get(a.id, 0)) if by == "logs" else (lambda a: a.avatar.get("coins", 0))
    ranked = sorted(astronauts, key=lambda a: (-value(a), a.name))
    return [{"rank": i + 1, "astronautId": a.id, "name": a.name, "value": value(a), "by": by,
             "level": a.avatar.get("level"), "equipped": a.avatar.get("equipped"), "isMe": a.id == me}
            for i, a in enumerate(ranked)]


def avatar_view(session: Session, astronaut: AppAstronaut, m: AppMission, day: int) -> dict:
    ev = evaluate(session, astronaut, day, m)
    owned = set(astronaut.avatar.get("owned", []))
    items = defaultdict(list)
    equipped = astronaut.avatar.get("equipped", {})
    room = equipped.get("room", {})
    for item in session.scalars(select(AppShopItem).order_by(AppShopItem.category, AppShopItem.price)):
        is_equipped = (equipped.get(item.category) == item.id) or (room.get(item.category) == item.id)
        items[item.category].append({"id": item.id, "name": item.name, "price": item.price, "asset": item.asset,
                                     "owned": item.id in owned or item.price == 0, "equipped": is_equipped})
    return {"avatar": astronaut.avatar | {"state": logic.avatar_state(ev)}, "shop": items}
