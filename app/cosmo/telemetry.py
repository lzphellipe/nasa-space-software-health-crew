"""Device telemetry of the CosmoMedic app: wearables, cuff, dosimeter and EVA suit.

The phone syncs readings in batches (Health Connect on Android, HealthKit on iPhone, or
Bluetooth Low Energy for sensors without a health platform); the EVA suit sends its own
telemetry. In this prototype there is no hardware, so a simulator makes the same readings,
labelled SIMULATED_TELEMETRY. Readings from real devices are DEVICE_MEASURED.

Readings never become logs by themselves. Orbi asks first:
* a reading that matches a daily log (sleep session, blood pressure, dosimeter dose)
  becomes a "log" confirmation: one tap saves it as the log (or the astronaut corrects it);
* a reading outside its usual range (low SpO2, high temperature, high suit CO2...)
  becomes a "check": a quick question on how the astronaut feels. "Not OK" alerts the ground.

Thresholds are illustrative screening values, not medical criteria.
"""

from __future__ import annotations

import hashlib
import math
import operator
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.cosmo.models import AppAstronaut, AppConfirmation, AppDeviceReading, AppLog
from app.provenance import DEVICE, SIMULATED

SOURCES = ("health_connect", "healthkit", "ble", "eva_suit", "simulator")
DEVICES = ("watch", "wrist_band", "bp_cuff", "dosimeter", "eva_suit", "thermometer", "pulse_oximeter")


@dataclass(frozen=True)
class Metric:
    code: str
    unit: str
    label: str
    lo: float | None = None  # plausible range of a scalar value
    hi: float | None = None
    log_type: str | None = None  # becomes this log when confirmed
    eva: bool = False


METRICS: dict[str, Metric] = {m.code: m for m in [
    Metric("heart_rate", "bpm", "Heart rate", 25, 230),
    Metric("resting_heart_rate", "bpm", "Resting heart rate", 25, 150),
    Metric("hrv", "ms", "Heart rate variability (RMSSD)", 1, 300),
    Metric("spo2", "%", "Blood oxygen (SpO2)", 50, 100),
    Metric("body_temperature", "°C", "Body temperature", 33, 43),
    Metric("respiratory_rate", "/min", "Respiratory rate", 4, 60),
    Metric("sleep_session", "session", "Sleep session", log_type="sleep"),
    Metric("blood_pressure", "mmHg", "Blood pressure", log_type="measurement"),
    Metric("radiation_dose", "µSv", "Dosimeter daily dose", 0, 100_000, log_type="radiation"),
    Metric("eva_heart_rate", "bpm", "EVA heart rate", 25, 230, eva=True),
    Metric("eva_suit_pressure", "kPa", "EVA suit pressure", 0, 60, eva=True),
    Metric("eva_co2", "mmHg", "EVA suit CO2 (partial pressure)", 0, 40, eva=True),
    Metric("eva_metabolic_rate", "W", "EVA metabolic rate", 50, 1500, eva=True),
]}

# Quick checks: (comparison, threshold, question, urgent). One pending check per metric per mission day.
# Declarative so the app can run the same checks on the device while there is no link (offline bundle).
CHECK_RULES = {
    "spo2": ("<", 92, "Your blood oxygen read {v:g}% at {t}. How are you feeling? Any shortness of breath?", True),
    "body_temperature": (">=", 37.8, "Your temperature read {v:.1f} °C at {t}. Do you feel feverish or unwell?", False),
    "respiratory_rate": (">", 24, "Your breathing rate read {v:.0f}/min at {t}, higher than usual. Are you OK?", False),
    "heart_rate": (">=", 120, "Your heart rate was {v:.0f} bpm at rest at {t}. Any chest discomfort, palpitations or dizziness?", False),
    "eva_heart_rate": (">=", 160, "Your heart rate reached {v:.0f} bpm during the EVA at {t}. How do you feel now?", True),
    "eva_co2": (">=", 8, "Suit CO2 reached {v:.1f} mmHg at {t}. Any headache, dizziness or shortness of breath?", True),
    "eva_suit_pressure": ("<", 27.5, "Suit pressure dropped to {v:.1f} kPa at {t}. Are you OK? Tell the crew and Mission Control.", True),
}
_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


def _test(op: str, threshold: float):
    return lambda v: _OPS[op](v, threshold)


# (test, question, urgent)
CHECKS = {metric: (_test(op, threshold), text, urgent) for metric, (op, threshold, text, urgent) in CHECK_RULES.items()}

SYMPTOMS = ("shortness_of_breath", "chest_pain", "headache", "dizziness", "nausea", "fever", "fatigue",
            "palpitations", "pain", "other")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M UTC")


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- validation ----------------------------------------------------------------------------------


def _parse_time(raw, field: str) -> datetime:
    if isinstance(raw, datetime):
        return _as_utc(raw)
    try:
        return _as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except ValueError:
        raise ValueError(f"{field} must be an ISO date-time") from None


def normalize(metric: str, value: float | None, values: dict | None) -> dict:
    """Validate one reading and return its stored `values`. Raises ValueError with a readable message."""
    spec = METRICS.get(metric)
    if spec is None:
        raise ValueError(f"unknown metric {metric!r}; one of {', '.join(METRICS)}")
    values = dict(values or {})
    if metric == "sleep_session":
        bed, wake = _parse_time(values.get("bedtime"), "values.bedtime"), _parse_time(values.get("wakeTime"), "values.wakeTime")
        minutes = (wake - bed).total_seconds() / 60
        if not 60 <= minutes <= 16 * 60:
            raise ValueError("a sleep session must last between 1 and 16 hours")
        out = {"bedtime": _iso(bed), "wakeTime": _iso(wake), "durationMin": round(minutes)}
        for key, lo, hi in (("avgHr", 20, 220), ("respRate", 4, 60), ("movementEvents", 0, 500), ("awakenings", 0, 50)):
            if values.get(key) is not None:
                v = float(values[key])
                if not lo <= v <= hi:
                    raise ValueError(f"values.{key} out of range")
                out[key] = int(v) if key in ("movementEvents", "awakenings") else v
        return out
    if metric == "blood_pressure":
        try:
            sys_, dia = float(values["systolic"]), float(values["diastolic"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("blood_pressure needs values.systolic and values.diastolic") from None
        if not (60 <= sys_ <= 260 and 30 <= dia <= 160 and sys_ > dia):
            raise ValueError("blood pressure out of range (systolic 60-260, diastolic 30-160, systolic > diastolic)")
        out = {"systolic": round(sys_), "diastolic": round(dia)}
        if values.get("pulse") is not None:
            out["pulse"] = round(float(values["pulse"]))
        return out
    if value is None:
        value = values.get("value")
    if value is None:
        raise ValueError(f"{metric} needs value")
    value = float(value)
    if metric == "radiation_dose" and values.get("unit") == "mSv":
        value *= 1000
    if not (spec.lo <= value <= spec.hi):
        raise ValueError(f"{metric} {value} outside the plausible range {spec.lo}-{spec.hi} {spec.unit}")
    out = {"value": round(value, 2)}
    if values.get("context") in ("rest", "exercise", "sleep", "eva"):
        out["context"] = values["context"]
    return out


# --- proposals -----------------------------------------------------------------------------------


def _sleep_quality(v: dict) -> tuple[int, str]:
    """Estimate quality 1-5 and wake-ups from the band, for the astronaut to confirm or change."""
    hours = v["durationMin"] / 60
    awakenings = v.get("awakenings", 0)
    movement = v.get("movementEvents", 0)
    quality = 5 if hours >= 7 else 4 if hours >= 6.5 else 3 if hours >= 5.5 else 2
    if awakenings >= 3 or movement > 12:
        quality = max(1, quality - 1)
    wake_ups = "none" if awakenings == 0 else "1-2" if awakenings <= 2 else "3+"
    return quality, wake_ups


def _fmt_minutes(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min"


def propose_log(reading: AppDeviceReading, badge_id: str | None) -> tuple[dict, str]:
    """The log data (frontend format) a reading would become, and Orbi's question."""
    v = reading.values
    if reading.metric == "sleep_session":
        quality, wake_ups = _sleep_quality(v)
        band = {k: v[k] for k in ("movementEvents", "avgHr", "respRate") if k in v}
        data = {"bedtime": v["bedtime"], "wakeTime": v["wakeTime"], "quality": quality, "wakeUps": wake_ups,
                "source": "archer_band", **({"band": band} if band else {})}
        bed, wake = _parse_time(v["bedtime"], "bedtime"), _parse_time(v["wakeTime"], "wakeTime")
        return data, (f"Your wrist band recorded {_fmt_minutes(v['durationMin'])} of sleep ({_hhmm(bed)} to "
                      f"{_hhmm(wake)}). Is that right? Quality looks like {quality}/5: change it if it felt different.")
    if reading.metric == "blood_pressure":
        data = {"metric": "blood_pressure", "values": dict(v)}
        pulse = f", pulse {v['pulse']}" if "pulse" in v else ""
        return data, f"The cuff measured {v['systolic']}/{v['diastolic']} mmHg{pulse} at {_hhmm(reading.measured_at)}. Save it?"
    if reading.metric == "radiation_dose":
        data = {"value": v["value"], "unit": "uSv", "source": "personal", **({"badgeId": badge_id} if badge_id else {})}
        return data, f"Your dosimeter reports {v['value']:g} µSv for today. Save it to your radiation log?"
    raise ValueError(reading.metric)


def _log_exists(session: Session, astronaut_id: str, day: int, log_type: str, metric: str) -> bool:
    logs = session.scalars(select(AppLog).where(AppLog.astronaut_id == astronaut_id, AppLog.mission_day == day,
                                                AppLog.type == log_type))
    for log in logs:
        if log_type != "measurement" or log.data.get("metric") == "blood_pressure":
            if log_type != "radiation" or log.data.get("source") == "personal":
                return True
    return False


def _pending(session: Session, astronaut_id: str, day: int, metric: str, kind: str) -> AppConfirmation | None:
    return session.scalar(select(AppConfirmation).where(
        AppConfirmation.astronaut_id == astronaut_id, AppConfirmation.mission_day == day,
        AppConfirmation.metric == metric, AppConfirmation.kind == kind,
        AppConfirmation.status.in_(("pending", "answered", "confirmed", "corrected"))))


# --- ingestion -----------------------------------------------------------------------------------


@dataclass
class IngestResult:
    batch_id: str
    stored: list[AppDeviceReading]
    duplicates: int
    confirmations: list[AppConfirmation]


def ingest(session: Session, astronaut: AppAstronaut, day: int, source: str, device: str,
           readings: list[dict], simulated: bool = False) -> IngestResult:
    """Store validated readings (dict: metric, measuredAt, values) and open Orbi's confirmations.

    Idempotent: a reading with the same metric, device and time is not stored twice, so the
    phone can resend a batch after a failed sync.
    """
    batch = new_id("sync")
    now = datetime.now(timezone.utc)
    stored, dupes, confirmations = [], 0, []
    for r in readings:
        metric, measured_at = r["metric"], _as_utc(r["measuredAt"])
        exists = session.scalar(select(AppDeviceReading.id).where(
            AppDeviceReading.astronaut_id == astronaut.id, AppDeviceReading.metric == metric,
            AppDeviceReading.device == device, AppDeviceReading.measured_at == measured_at))
        if exists:
            dupes += 1
            continue
        reading = AppDeviceReading(
            id=new_id("rd"), astronaut_id=astronaut.id, source=source, device=device, metric=metric,
            values=r["values"], unit=METRICS[metric].unit, measured_at=measured_at, received_at=now,
            mission_day=day, simulated=simulated, batch_id=batch)
        session.add(reading)
        stored.append(reading)
    session.flush()

    badge = (astronaut.devices or {}).get("dosimeterBadgeId")
    for reading in sorted(stored, key=lambda x: x.measured_at):
        spec = METRICS[reading.metric]
        conf = None
        if spec.log_type:
            # Already logged today, or already proposed: nothing to ask.
            if _log_exists(session, astronaut.id, day, spec.log_type, reading.metric) or \
                    _pending(session, astronaut.id, day, reading.metric, "log"):
                continue
            data, question = propose_log(reading, badge)
            conf = AppConfirmation(id=new_id("cnf"), astronaut_id=astronaut.id, kind="log", metric=reading.metric,
                                   reading_ids=[reading.id], log_type=spec.log_type, proposed=data, question=question,
                                   mission_day=day, simulated=simulated)
        elif reading.metric in CHECKS:
            test, text, _ = CHECKS[reading.metric]
            value = reading.values["value"]
            if reading.metric == "heart_rate" and reading.values.get("context") in ("exercise", "eva"):
                continue
            if test(value) and not _pending(session, astronaut.id, day, reading.metric, "check"):
                conf = AppConfirmation(id=new_id("cnf"), astronaut_id=astronaut.id, kind="check", metric=reading.metric,
                                       reading_ids=[reading.id], proposed={"value": value, "unit": spec.unit},
                                       question=text.format(v=value, t=_hhmm(reading.measured_at)),
                                       mission_day=day, simulated=simulated)
        if conf is not None:
            session.add(conf)
            session.flush()
            confirmations.append(conf)
    return IngestResult(batch, stored, dupes, confirmations)


# --- output --------------------------------------------------------------------------------------


def reading_out(r: AppDeviceReading) -> dict:
    return {"id": r.id, "astronautId": r.astronaut_id, "metric": r.metric, "label": METRICS[r.metric].label,
            "values": r.values, "unit": r.unit, "measuredAt": _iso(r.measured_at), "receivedAt": _iso(r.received_at),
            "source": r.source, "device": r.device, "missionDay": r.mission_day,
            "provenance": SIMULATED if r.simulated else DEVICE}


def confirmation_out(c: AppConfirmation) -> dict:
    urgent = c.kind == "check" and CHECKS.get(c.metric, (None, None, False))[2]
    if c.kind == "log":
        actions = ["confirm", "correct", "dismiss"]
    else:
        actions = ["ok", "not_ok", "dismiss"]
    return {"id": c.id, "astronautId": c.astronaut_id, "kind": c.kind, "metric": c.metric,
            "label": METRICS[c.metric].label, "question": c.question, "logType": c.log_type,
            "proposed": c.proposed, "readingIds": c.reading_ids, "status": c.status, "answer": c.answer,
            "logId": c.log_id, "alertId": c.alert_id, "urgent": bool(urgent), "actions": actions,
            "symptomOptions": list(SYMPTOMS) if c.kind == "check" else [],
            "missionDay": c.mission_day, "createdAt": _iso(c.created_at), "resolvedAt": _iso(c.resolved_at),
            "provenance": SIMULATED if c.simulated else DEVICE}


def pending_count(session: Session, astronaut_id: str) -> int:
    return session.scalar(select(func.count()).select_from(AppConfirmation).where(
        AppConfirmation.astronaut_id == astronaut_id, AppConfirmation.status == "pending")) or 0


def last_sync(session: Session, astronaut_id: str) -> datetime | None:
    return session.scalar(select(func.max(AppDeviceReading.received_at)).where(
        AppDeviceReading.astronaut_id == astronaut_id))


def sync_status(session: Session, astronaut: AppAstronaut) -> dict:
    """Last sync per device and source, and what is waiting for Orbi."""
    rows = session.execute(
        select(AppDeviceReading.device, AppDeviceReading.source, func.max(AppDeviceReading.received_at),
               func.max(AppDeviceReading.measured_at), func.count(), func.max(case((AppDeviceReading.simulated, 1), else_=0)))
        .where(AppDeviceReading.astronaut_id == astronaut.id)
        .group_by(AppDeviceReading.device, AppDeviceReading.source)).all()
    devices = [{"device": d, "source": s, "lastSyncAt": _iso(_as_utc(sync)), "lastReadingAt": _iso(_as_utc(meas)),
                "readings": n, "simulated": bool(sim)} for d, s, sync, meas, n, sim in rows]
    last = max((_as_utc(r[2]) for r in rows), default=None)
    minutes = round((datetime.now(timezone.utc) - last).total_seconds() / 60) if last else None
    return {"lastSyncAt": _iso(last), "minutesSinceSync": minutes, "devices": sorted(devices, key=lambda x: x["device"]),
            "paired": astronaut.devices, "pendingConfirmations": pending_count(session, astronaut.id),
            "simulated": any(d["simulated"] for d in devices) if devices else None}


def latest_vitals(session: Session, astronaut_id: str) -> dict:
    """Latest value of each continuous metric, for the Health Overview."""
    out = {}
    for code in ("heart_rate", "resting_heart_rate", "spo2", "body_temperature", "respiratory_rate", "hrv"):
        r = session.scalar(select(AppDeviceReading).where(
            AppDeviceReading.astronaut_id == astronaut_id, AppDeviceReading.metric == code)
            .order_by(AppDeviceReading.measured_at.desc()).limit(1))
        if r is not None:
            out[code] = {"value": r.values["value"], "unit": r.unit, "measuredAt": _iso(r.measured_at),
                         "provenance": SIMULATED if r.simulated else DEVICE}
    return out


def summary(readings: list[AppDeviceReading]) -> dict:
    by_metric: dict[str, list[AppDeviceReading]] = {}
    for r in readings:
        by_metric.setdefault(r.metric, []).append(r)
    out = {}
    for code, items in by_metric.items():
        items.sort(key=lambda r: r.measured_at)
        nums = [r.values["value"] for r in items if "value" in r.values]
        out[code] = {"label": METRICS[code].label, "unit": METRICS[code].unit, "count": len(items),
                     "latest": items[-1].values, "latestAt": _iso(items[-1].measured_at),
                     **({"min": min(nums), "max": max(nums), "avg": round(sum(nums) / len(nums), 2)} if nums else {})}
    return out


# --- simulator -----------------------------------------------------------------------------------

ANOMALIES = ("none", "low_spo2", "fever", "high_heart_rate", "eva_co2", "eva_pressure")


def _rng(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


def simulate(astronaut: AppAstronaut, day: int, now: datetime, hours: int = 24, eva: bool = False,
             anomaly: str = "none") -> dict[str, list[dict]]:
    """Readings a wrist band, cuff, dosimeter (and suit, during an EVA) would sync over the last `hours`.

    Grouped by device. Deterministic for the same astronaut, day and hour, so a repeated call
    produces the same readings (and the ingestion skips them as duplicates).
    """
    now = _as_utc(now).replace(second=0, microsecond=0)
    now = now - timedelta(minutes=now.minute % 15)
    base = astronaut.baseline or {}
    rest_hr = float(base.get("restingHr", 62))
    usual_sleep = float(base.get("usualSleepH", 7.5))
    rng = _rng(astronaut.id, day, now.isoformat(), hours, eva, anomaly)
    out: dict[str, list[dict]] = {"wrist_band": [], "bp_cuff": [], "dosimeter": [], "eva_suit": []}

    # Last night's sleep: ends at 06:30 UTC before now, shorter for crew members whose story is poor sleep.
    wake = now.replace(hour=6, minute=30)
    if wake > now:
        wake -= timedelta(days=1)
    short = {"ast-01": 1.3, "ast-04": 2.0}.get(astronaut.id, 0.4)
    sleep_h = max(3.5, usual_sleep - short + rng.uniform(-0.4, 0.3))
    bed = wake - timedelta(hours=sleep_h)
    awake = 0 if sleep_h > 6.8 else rng.choice((1, 2, 3))

    start = now - timedelta(hours=hours)
    t = start
    while t <= now:
        asleep = bed <= t <= wake
        circadian = math.sin((t.hour + t.minute / 60 - 9) / 24 * 2 * math.pi)
        hr = rest_hr + (-6 if asleep else 8 + 6 * circadian) + rng.gauss(0, 3)
        if anomaly == "high_heart_rate" and now - timedelta(hours=1) < t <= now - timedelta(minutes=30):
            hr = 126 + rng.uniform(0, 6)
        out["wrist_band"].append({"metric": "heart_rate", "measuredAt": t, "values": {
            "value": round(hr), "context": "sleep" if asleep else "rest"}})
        if t.minute == 0:
            spo2 = 97 + rng.gauss(0, 0.8) - (0.6 if asleep else 0)
            if anomaly == "low_spo2" and t >= now - timedelta(hours=1):
                spo2 = 89.5
            out["wrist_band"].append({"metric": "spo2", "measuredAt": t, "values": {"value": round(min(spo2, 100), 1)}})
            out["wrist_band"].append({"metric": "respiratory_rate", "measuredAt": t,
                                      "values": {"value": round(14 + rng.gauss(0, 1.2) - (2 if asleep else 0))}})
        if t.minute == 0 and t.hour % 6 == 0:
            temp = 36.8 + 0.25 * circadian + rng.gauss(0, 0.1)
            if anomaly == "fever" and t >= now - timedelta(hours=7):
                temp = 38.3 + rng.uniform(0, 0.3)
            out["wrist_band"].append({"metric": "body_temperature", "measuredAt": t, "values": {"value": round(temp, 1)}})
        t += timedelta(minutes=15)

    if start <= wake <= now:
        out["wrist_band"].append({"metric": "sleep_session", "measuredAt": wake, "values": {
            "bedtime": bed, "wakeTime": wake, "avgHr": round(rest_hr - 6 + rng.gauss(0, 1)),
            "respRate": round(13 + rng.gauss(0, 0.8), 1), "movementEvents": rng.randint(1, 4) + 4 * awake,
            "awakenings": awake}})
        out["wrist_band"].append({"metric": "resting_heart_rate", "measuredAt": wake,
                                  "values": {"value": round(rest_hr + rng.gauss(0, 2))}})
        out["wrist_band"].append({"metric": "hrv", "measuredAt": wake,
                                  "values": {"value": round(max(15, 55 - 6 * (usual_sleep - sleep_h) + rng.gauss(0, 6)))}})
        bp_time = wake + timedelta(minutes=50)
        if bp_time <= now:
            sys_, dia = (int(x) for x in str(base.get("restingBp", "118/76")).split("/"))
            out["bp_cuff"].append({"metric": "blood_pressure", "measuredAt": bp_time, "values": {
                "systolic": round(sys_ + rng.gauss(0, 4)), "diastolic": round(dia + rng.gauss(0, 3)),
                "pulse": round(rest_hr + rng.gauss(0, 3))}})
    out["dosimeter"].append({"metric": "radiation_dose", "measuredAt": now,
                             "values": {"value": round(40 + rng.gauss(0, 3), 1)}})

    if eva or anomaly in ("eva_co2", "eva_pressure"):
        eva_start = now - timedelta(hours=min(hours, 6))
        t = eva_start
        while t <= now:
            frac = (t - eva_start) / max(now - eva_start, timedelta(minutes=1))
            co2 = 2.5 + 2 * frac + rng.gauss(0, 0.3)
            if anomaly == "eva_co2" and frac > 0.8:
                co2 = 8.6 + rng.uniform(0, 0.8)
            pressure = 29.6 + rng.gauss(0, 0.05)
            if anomaly == "eva_pressure" and frac > 0.9:
                pressure = 26.9
            out["eva_suit"] += [
                {"metric": "eva_heart_rate", "measuredAt": t, "values": {"value": round(105 + 25 * math.sin(frac * math.pi) + rng.gauss(0, 5)), "context": "eva"}},
                {"metric": "eva_co2", "measuredAt": t, "values": {"value": round(max(co2, 0), 2)}},
                {"metric": "eva_suit_pressure", "measuredAt": t, "values": {"value": round(pressure, 2)}},
                {"metric": "eva_metabolic_rate", "measuredAt": t, "values": {"value": round(300 + 120 * math.sin(frac * math.pi) + rng.gauss(0, 20))}},
            ]
            t += timedelta(minutes=10)

    # Normalize to the stored format (datetimes to ISO, validation of ranges).
    for device, items in out.items():
        for item in items:
            item["values"] = normalize(item["metric"], None, {
                k: (_iso(v) if isinstance(v, datetime) else v) for k, v in item["values"].items()})
    return {d: items for d, items in out.items() if items}
