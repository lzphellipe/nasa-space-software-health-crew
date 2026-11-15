
"""Evidence-based, reusable analyses for the Orbi health briefing API.

All calculations use stored synthetic daily measurements.
Screening flags are illustrative and must not be interpreted as diagnoses.
"""

from __future__ import annotations

import statistics

from sqlalchemy.orm import Session

from app.models import Astronaut, Mission
from app.services.synthetic_store import get_baselines, get_records
from app.synthetic.metrics import METRICS, METRICS_BY_CODE


PAGE_METRICS = {
    "sleep": ("sleep_hours",),
    "mood": ("mood",),
    "body": (
        "systolic_bp",
        "diastolic_bp",
        "heart_rate",
        "blood_glucose",
        "body_temperature",
        "bone_density",
        "white_blood_cells",
    ),
    "meals": ("calorie_intake", "protein_intake"),
    "exercise": (),
    "radiation": (),
    "home": tuple(metric.code for metric in METRICS),
}


def _calculate_trend(measurements, metric):
    """Estimate the direction of recent measurements.

    Parameters
    ----------
    measurements : list[tuple[int, float]]
        Available measurements containing mission day and value.
    metric : Metric
        Metric metadata, including its variability floor.

    Returns
    -------
    dict
        Trend direction, slope per day and number of observations.
    """
    if len(measurements) < 4:
        return {
            "direction": "insufficient_data",
            "slopePerDay": None,
            "observationCount": len(measurements),
        }

    days = [day for day, _ in measurements]
    values = [value for _, value in measurements]

    mean_day = statistics.fmean(days)
    mean_value = statistics.fmean(values)

    denominator = sum((day - mean_day) ** 2 for day in days)

    if denominator == 0:
        return {
            "direction": "insufficient_data",
            "slopePerDay": None,
            "observationCount": len(measurements),
        }

    slope = sum(
        (day - mean_day) * (value - mean_value)
        for day, value in measurements
    ) / denominator

    duration = max(days) - min(days)

    estimated_change = slope * duration
    threshold = max(
        metric.sd_floor,
        abs(mean_value) * 0.03,
    )

    if estimated_change > threshold:
        direction = "increasing"
    elif estimated_change < -threshold:
        direction = "decreasing"
    else:
        direction = "stable"

    return {
        "direction": direction,
        "slopePerDay": round(slope, metric.decimals + 3),
        "observationCount": len(measurements),
    }


def _calculate_persistence(records, metric, day):
    """Count recent screening flags for a metric.

    Parameters
    ----------
    records : list[DailyRecord]
        Daily records available for the astronaut.
    metric : Metric
        Metric metadata.
    day : int
        Requested mission day.

    Returns
    -------
    dict
        Number of measured observations and screening flags.
    """
    recent_records = [
        record
        for record in records
        if day - 6 <= record.mission_day <= day
        and getattr(record, metric.code) is not None
    ]

    watch_count = 0
    out_of_range_count = 0

    for record in recent_records:
        state = (record.metric_status or {}).get(
            metric.code,
            "no_data",
        )

        if state == "watch":
            watch_count += 1
        elif state == "out_of_range":
            out_of_range_count += 1

    measured_count = len(recent_records)
    flagged_count = watch_count + out_of_range_count

    return {
        "measuredCount": measured_count,
        "flaggedCount": flagged_count,
        "watchCount": watch_count,
        "outOfRangeCount": out_of_range_count,
        "flaggedFraction": (
            round(flagged_count / measured_count, 2)
            if measured_count
            else None
        ),
    }


def _analyze_metric(records, baseline, metric, day):
    """Summarize measurements, trends and screening evidence.

    Parameters
    ----------
    records : list[DailyRecord]
        Records ending on the requested mission day.
    baseline : Baseline or None
        Preflight reference for this astronaut and metric.
    metric : Metric
        Metadata and illustrative screening thresholds.
    day : int
        Requested mission day.

    Returns
    -------
    dict
        Numeric evidence, trend, persistence and attention ranking.
    """
    available = sorted(
        (
            record.mission_day,
            float(getattr(record, metric.code)),
        )
        for record in records
        if getattr(record, metric.code) is not None
    )

    if not available:
        return {
            "metric": metric.code,
            "label": metric.label,
            "unit": metric.unit,
            "status": "no_data",
            "priority": 0,
            "evidence": [],
        }

    recent = [
        (measurement_day, value)
        for measurement_day, value in available
        if day - 6 <= measurement_day <= day
    ]

    previous = [
        (measurement_day, value)
        for measurement_day, value in available
        if day - 13 <= measurement_day <= day - 7
    ]

    last_day, latest = available[-1]
    days_since_measurement = day - last_day
    fresh = days_since_measurement <= metric.max_gap_days

    baseline_mean = (
        float(baseline.mean)
        if baseline is not None and baseline.mean is not None
        else None
    )

    recent_mean = (
        statistics.fmean(value for _, value in recent)
        if recent
        else None
    )

    previous_mean = (
        statistics.fmean(value for _, value in previous)
        if previous
        else None
    )

    trend = _calculate_trend(recent, metric)
    persistence = _calculate_persistence(records, metric, day)

    result = {
        "metric": metric.code,
        "label": metric.label,
        "unit": metric.unit,
        "latest": round(latest, metric.decimals + 1),
        "lastMeasuredDay": last_day,
        "daysSinceMeasurement": days_since_measurement,
        "baselineMean": (
            round(baseline_mean, metric.decimals + 2)
            if baseline_mean is not None
            else None
        ),
        "recentCount": len(recent),
        "previousCount": len(previous),
        "recentMean": (
            round(recent_mean, metric.decimals + 2)
            if recent_mean is not None
            else None
        ),
        "previousMean": (
            round(previous_mean, metric.decimals + 2)
            if previous_mean is not None
            else None
        ),
        "recentRange": (
            round(
                max(value for _, value in recent)
                - min(value for _, value in recent),
                metric.decimals + 2,
            )
            if len(recent) >= 2
            else None
        ),
        "status": (
            "stale" if not fresh else
            "insufficient_data" if len(recent) < 2 else
            "available"
        ),
        "priority": 0,
        "evidence": [],
        "trend": trend,
        "persistence": persistence,
        "baselineChangePct": None,
        "latestBaselineChangePct": None,
        "weekOverWeekChange": None,
        "screeningStatus": "no_data",
    }

    if baseline_mean is not None and baseline_mean != 0:
        result["latestBaselineChangePct"] = round(
            100 * (latest / baseline_mean - 1),
            1,
        )

        if recent_mean is not None:
            result["baselineChangePct"] = round(
                100 * (recent_mean / baseline_mean - 1),
                1,
            )

    if len(recent) >= 3 and len(previous) >= 3:
        difference = recent_mean - previous_mean

        result["weekOverWeekChange"] = round(
            difference,
            metric.decimals + 2,
        )

        threshold = max(
            metric.sd_floor * 2,
            abs(previous_mean) * 0.05,
        )

        if abs(difference) >= threshold:
            direction = (
                "increased"
                if difference > 0
                else "decreased"
            )

            result["evidence"].append(
                f"The recent seven-day mean {direction} "
                "compared with the previous seven-day period."
            )

            result["priority"] += 1

    if fresh:
        latest_record = next(
            (
                record
                for record in reversed(records)
                if record.mission_day == last_day
            ),
            None,
        )

        state = (
            (latest_record.metric_status or {}).get(
                metric.code,
                "no_data",
            )
            if latest_record is not None
            else "no_data"
        )

        result["screeningStatus"] = state

        if state in ("watch", "out_of_range"):
            result["priority"] += (
                2 if state == "watch" else 4
            )

            result["evidence"].append(
                "The latest reading has an illustrative "
                f"{state.replace('_', ' ')} screening flag."
            )

        if persistence["measuredCount"] >= 3:
            flagged_count = persistence["flaggedCount"]
            measured_count = persistence["measuredCount"]

            if flagged_count >= 3:
                result["evidence"].append(
                    f"{flagged_count} of the last "
                    f"{measured_count} measured observations "
                    "have illustrative screening flags."
                )

                result["priority"] += 2

        if (
            trend["direction"] in ("increasing", "decreasing")
            and trend["observationCount"] >= 4
        ):
            result["evidence"].append(
                "Recent measurements show an estimated "
                f"{trend['direction']} trend."
            )

            result["priority"] += 1

    else:
        result["evidence"].append(
            "The latest measurement is not recent enough "
            "for the expected sampling cadence."
        )

        result["priority"] = 0

    if not result["evidence"]:
        result["evidence"].append(
            "No notable change was identified by "
            "the current screening rules."
        )

    return result


def build_insight(
    session: Session,
    mission: Mission,
    astronaut: Astronaut,
    page: str,
    day: int,
) -> dict:
    """Build a page-specific briefing and traceable numeric evidence.

    Parameters
    ----------
    session : Session
        Active database session.
    mission : Mission
        Mission containing the requested astronaut.
    astronaut : Astronaut
        Authenticated user's permitted astronaut.
    page : str
        Frontend page identifier.
    day : int
        Requested mission day.

    Returns
    -------
    dict
        Page briefing and metric analyses for the frontend.
    """
    if page not in PAGE_METRICS:
        raise ValueError("Unknown insight page")

    codes = PAGE_METRICS[page]

    if not codes:
        return {
            "page": page,
            "missionId": mission.id,
            "astronautId": astronaut.id,
            "missionDay": day,
            "status": "disabled",
            "message": (
                "No verified measurements are available "
                "for this section yet."
            ),
            "metrics": [],
            "dataSource": "SIMULATED_TELEMETRY",
            "analysisType": "RULE_DERIVED",
            "limitations": [
                "This section has no matching daily metrics "
                "in the synthetic mission database."
            ],
        }

    records = get_records(
        session,
        mission.id,
        astronaut.id,
        max(mission.first_day, day - 30),
        day,
    )

    records = sorted(
        records,
        key=lambda record: record.mission_day,
    )

    baselines = get_baselines(session, astronaut.id)

    analyses = [
        _analyze_metric(
            records,
            baselines.get(code),
            METRICS_BY_CODE[code],
            day,
        )
        for code in codes
    ]

    ranked = sorted(
        (
            analysis
            for analysis in analyses
            if analysis["priority"] > 0
        ),
        key=lambda analysis: (
            -analysis["priority"],
            analysis["metric"],
        ),
    )

    if ranked:
        top_score = ranked[0]["priority"]

        tied = [
            analysis
            for analysis in ranked
            if analysis["priority"] == top_score
        ]

        lead = tied[day % len(tied)]
        detail = lead["evidence"][0]

        message = f"{lead['label']}: {detail}"

        if page == "home" and len(ranked) > 1:
            other = next(
                analysis
                for analysis in ranked
                if analysis["metric"] != lead["metric"]
            )

            message += (
                f" Also worth reviewing: "
                f"{other['label'].lower()}."
            )

    else:
        available = [
            analysis
            for analysis in analyses
            if analysis["status"] in ("available", "insufficient_data")
        ]

        if available:
            lead = available[day % len(available)]

            message = (
                "Today's review includes "
                f"{lead['label'].lower()}. "
                "No notable change was identified "
                "by the current screening rules."
            )

        else:
            message = (
                "There are not enough recent measurements "
                "to provide a reliable briefing."
            )

    metric_statuses = {analysis["status"] for analysis in analyses}
    if "available" in metric_statuses:
        page_status = "available"
    elif "insufficient_data" in metric_statuses:
        page_status = "insufficient_data"
    elif "stale" in metric_statuses:
        page_status = "stale"
    else:
        page_status = "no_data"

    return {
        "page": page,
        "missionId": mission.id,
        "astronautId": astronaut.id,
        "missionDay": day,
        "status": page_status,
        "message": message,
        "metrics": analyses,
        "dataSource": "SIMULATED_TELEMETRY",
        "analysisType": "RULE_DERIVED",
        "limitations": [
            "Synthetic mission measurements "
            "are not clinical validation.",
            "Screening flags are illustrative, "
            "not diagnostic criteria.",
            "Statistical summaries are deterministic; "
            "AI-generated narratives are produced separately.",
        ],
    }
