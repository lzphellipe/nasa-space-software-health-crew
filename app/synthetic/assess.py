"""Assessment of the daily records: personal baselines, statuses, episodes, trends.

Pure functions over plain dicts, so the same logic serves the database loader,
the API and offline notebooks.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass

from app.synthetic.metrics import METRICS, METRICS_BY_CODE, Metric

STATUS_ORDER = {"no_data": 0, "normal": 1, "watch": 2, "out_of_range": 3}


@dataclass
class Baseline:
    metric: str
    n: int
    mean: float | None
    sd: float | None
    sd_effective: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def compute_baselines(records: list[dict]) -> dict[str, Baseline]:
    """Baseline per metric from the pre-flight records of one astronaut."""
    out: dict[str, Baseline] = {}
    pre = [r for r in records if r["mission_day"] < 0]
    for m in METRICS:
        values = [r[m.code] for r in pre if r.get(m.code) is not None]
        n = len(values)
        mean = statistics.fmean(values) if n else None
        sd = statistics.stdev(values) if n >= 2 else None
        sd_eff = max(sd or 0.0, m.sd_floor) if mean is not None else None
        out[m.code] = Baseline(m.code, n, mean, sd, sd_eff)
    return out


def metric_status(metric: Metric, value: float | None, baseline: Baseline | None) -> str:
    if value is None:
        return "no_data"
    if metric.out_high is not None and value >= metric.out_high:
        return "out_of_range"
    if metric.out_low is not None and value < metric.out_low:
        return "out_of_range"
    pct = pct_vs_baseline(value, baseline)
    if pct is not None and metric.out_rel_low is not None and pct <= metric.out_rel_low:
        return "out_of_range"
    if metric.watch_high is not None and value >= metric.watch_high:
        return "watch"
    if metric.watch_low is not None and value < metric.watch_low:
        return "watch"
    if pct is not None and metric.watch_rel_low is not None and pct <= metric.watch_rel_low:
        return "watch"
    return "normal"


def pct_vs_baseline(value: float | None, baseline: Baseline | None) -> float | None:
    if value is None or baseline is None or not baseline.mean:
        return None
    return (value / baseline.mean - 1) * 100


def z_vs_baseline(value: float | None, baseline: Baseline | None) -> float | None:
    if value is None or baseline is None or baseline.mean is None or not baseline.sd_effective:
        return None
    return (value - baseline.mean) / baseline.sd_effective


def worst(statuses) -> str:
    result = "no_data"
    for s in statuses:
        if STATUS_ORDER[s] > STATUS_ORDER[result]:
            result = s
    return result


def assess_record(record: dict, baselines: dict[str, Baseline]) -> dict[str, str]:
    """Status per metric for one daily record."""
    return {m.code: metric_status(m, record.get(m.code), baselines.get(m.code)) for m in METRICS}


def find_episodes(records: list[dict], statuses: list[dict[str, str]]) -> list[dict]:
    """Group consecutive non-normal measurements of the same metric into episodes."""
    episodes: list[dict] = []
    for m in METRICS:
        current: dict | None = None
        last_measured_day: int | None = None
        for rec, st in zip(records, statuses):
            value = rec.get(m.code)
            if value is None:
                continue
            day = rec["mission_day"]
            status = st[m.code]
            abnormal = status in ("watch", "out_of_range")
            if abnormal:
                if (
                    current is not None
                    and last_measured_day is not None
                    and day - current["end_day"] <= m.max_gap_days
                ):
                    current["end_day"] = day
                    current["measurements"] += 1
                    current["status"] = worst([current["status"], status])
                    if _more_extreme(m, value, current["peak_value"]):
                        current["peak_value"], current["peak_day"] = value, day
                else:
                    current = {
                        "astronaut_id": rec["astronaut_id"],
                        "metric": m.code,
                        "label": m.label,
                        "unit": m.unit,
                        "status": status,
                        "start_day": day,
                        "end_day": day,
                        "measurements": 1,
                        "peak_value": value,
                        "peak_day": day,
                    }
                    episodes.append(current)
            elif current is not None:
                current = None
            last_measured_day = day
    episodes.sort(key=lambda e: (e["start_day"], -STATUS_ORDER[e["status"]]))
    return episodes


def _more_extreme(metric: Metric, new: float, old: float) -> bool:
    high_side = metric.out_high is not None or metric.watch_high is not None
    return new > old if high_side else new < old


def linear_slope(points: list[tuple[int, float]]) -> float | None:
    """Least-squares slope (units per day)."""
    if len(points) < 3:
        return None
    xs, ys = zip(*points)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def summarize_window(records: list[dict], statuses: list[dict[str, str]], baselines: dict[str, Baseline]) -> dict:
    summary = {}
    for m in METRICS:
        pts = [(r["mission_day"], r[m.code]) for r in records if r.get(m.code) is not None]
        values = [v for _, v in pts]
        sts = [s[m.code] for s, r in zip(statuses, records) if r.get(m.code) is not None]
        b = baselines.get(m.code)
        mean = statistics.fmean(values) if values else None
        slope = linear_slope(pts)
        summary[m.code] = {
            "label": m.label,
            "unit": m.unit,
            "n": len(values),
            "mean": mean,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "last": values[-1] if values else None,
            "last_day": pts[-1][0] if pts else None,
            "baseline_mean": b.mean if b else None,
            "delta_vs_baseline_pct": pct_vs_baseline(mean, b),
            "z_vs_baseline": z_vs_baseline(mean, b),
            "trend_per_30_days": slope * 30 if slope is not None else None,
            "days_watch": sts.count("watch"),
            "days_out_of_range": sts.count("out_of_range"),
        }
    return summary


def _fmt(value: float | None, decimals: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{decimals}f}"


def with_unit(value: float | None, metric: Metric, decimals: int | None = None) -> str:
    text = _fmt(value, metric.decimals if decimals is None else decimals)
    return f"{text}{metric.unit}" if metric.unit.startswith("/") else f"{text} {metric.unit}"


def highlights(summary: dict, episodes: list[dict]) -> list[str]:
    """Short factual sentences an LLM agent can quote or reason over.

    Every out-of-range episode is listed; watch episodes only when they span
    more than one measurement, so single noisy days do not crowd the context.
    """
    lines = []
    relevant = [
        e for e in episodes
        if e["status"] == "out_of_range" or e["measurements"] >= 2
    ]
    for e in sorted(relevant, key=lambda e: (-STATUS_ORDER[e["status"]], e["start_day"])):
        m = METRICS_BY_CODE[e["metric"]]
        when = f"day {e['start_day']}" if e["start_day"] == e["end_day"] else f"days {e['start_day']}–{e['end_day']}"
        lines.append(
            f"{m.label}: {e['status'].replace('_', ' ')} on {when}; most extreme "
            f"{with_unit(e['peak_value'], m)} on day {e['peak_day']}."
        )
    for code, s in summary.items():
        m = METRICS_BY_CODE[code]
        trend, b = s["trend_per_30_days"], s["baseline_mean"]
        if trend is None or b is None or m.cadence == "weekly":
            continue
        # Report trends larger than 2 % of the baseline per 30 days.
        if abs(trend) >= 0.02 * abs(b):
            d = max(m.decimals, 1)
            lines.append(
                f"{m.label} trend: {with_unit(trend, m, d)} per 30 days "
                f"(window mean {with_unit(s['mean'], m, d)} vs pre-flight {with_unit(b, m, d)})."
            )
    return lines
