"""Derived health indicators: reference-range flags and personal baselines.

Every crew member is compared with their own pre-flight samples (L-92, L-44,
L-3). A post-flight value is:

* ``out_of_range`` when it falls outside the lab reference range;
* ``watch`` when it is inside the range (or has none) but moved at least
  ``WATCH_PCT_CHANGE`` percent AND ``WATCH_Z_SCORE`` standard deviations
  from the personal pre-flight mean;
* ``normal`` otherwise, and ``no_data`` when the value is missing.

Pre-flight samples get flags against the reference range but are never
``watch``, because they are the baseline.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BaselineStat, Measurement, Timepoint

STATUS_ORDER = {"no_data": 0, "normal": 1, "watch": 2, "out_of_range": 3}


def reference_flag(value: float | None, ref_min: float | None, ref_max: float | None) -> str:
    if value is None:
        return "missing"
    if ref_min is None and ref_max is None:
        return "no_reference"
    if ref_min is not None and value < ref_min:
        return "low"
    if ref_max is not None and value > ref_max:
        return "high"
    return "normal"


def worst_status(statuses) -> str:
    worst = "no_data"
    for status in statuses:
        if STATUS_ORDER.get(status, 0) > STATUS_ORDER[worst]:
            worst = status
    return worst


def recompute_derived(session: Session, settings: Settings) -> int:
    """Recompute baselines and per-measurement deviation for the whole database."""
    phases = dict(session.execute(select(Timepoint.code, Timepoint.phase)).all())
    measurements = session.scalars(select(Measurement)).all()

    groups: dict[tuple[str, str], list[Measurement]] = defaultdict(list)
    for m in measurements:
        groups[(m.crew_id, m.analyte_code)].append(m)

    session.execute(delete(BaselineStat))
    for (crew_id, analyte_code), items in groups.items():
        baseline_values = [
            m.value for m in items if m.value is not None and phases.get(m.timepoint_code) == "preflight"
        ]
        n = len(baseline_values)
        mean = statistics.fmean(baseline_values) if n else None
        sd = statistics.stdev(baseline_values) if n >= 2 else None
        sd_eff = None
        if mean is not None:
            sd_eff = max(sd or 0.0, abs(mean) * settings.baseline_sd_floor_fraction, 1e-9)

        session.add(
            BaselineStat(
                crew_id=crew_id,
                analyte_code=analyte_code,
                n=n,
                mean=mean,
                sd=sd,
                sd_effective=sd_eff,
                min=min(baseline_values) if n else None,
                max=max(baseline_values) if n else None,
            )
        )

        for m in items:
            m.flag = reference_flag(m.value, m.ref_min, m.ref_max)
            m.baseline_mean = mean
            if m.value is None or mean is None:
                m.delta_abs = m.delta_pct = m.z_score = None
            else:
                m.delta_abs = m.value - mean
                m.delta_pct = (m.delta_abs / mean * 100.0) if mean != 0 else None
                m.z_score = m.delta_abs / sd_eff if sd_eff else None

            if m.value is None:
                m.status = "no_data"
            elif m.flag in ("low", "high"):
                m.status = "out_of_range"
            elif (
                phases.get(m.timepoint_code) != "preflight"
                and m.delta_pct is not None
                and m.z_score is not None
                and abs(m.delta_pct) >= settings.watch_pct_change
                and abs(m.z_score) >= settings.watch_z_score
            ):
                m.status = "watch"
            else:
                m.status = "normal"

    return len(measurements)
