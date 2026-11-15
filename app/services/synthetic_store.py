"""Load the synthetic mission into the database and read it back as plain dicts."""

from __future__ import annotations

from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Astronaut, CheckIn, DailyRecord, Mission, SyntheticEvent, VitalBaseline
from app.synthetic.assess import Baseline, assess_record, compute_baselines, worst
from app.synthetic.generator import generate
from app.synthetic.metrics import METRICS

METRIC_CODES = [m.code for m in METRICS]


def load_synthetic(session: Session, seed: int = 42) -> Mission:
    """(Re)generate the synthetic mission and replace whatever was stored before."""
    ds = generate(seed)
    mission_id = ds.mission["id"]

    for model in (SyntheticEvent, VitalBaseline, DailyRecord):
        if model is VitalBaseline:
            session.execute(delete(model).where(
                model.astronaut_id.in_(select(Astronaut.id).where(Astronaut.mission_id == mission_id))
            ))
        else:
            session.execute(delete(model).where(model.mission_id == mission_id))
    session.execute(delete(CheckIn).where(CheckIn.mission_id == mission_id, CheckIn.source == "synthetic"))
    session.execute(delete(Astronaut).where(Astronaut.mission_id == mission_id))
    session.execute(delete(Mission).where(Mission.id == mission_id))
    session.flush()

    mission = Mission(**{k: ds.mission[k] for k in (
        "id", "name", "description", "launch_date", "first_day", "last_day", "synthetic", "seed"
    )})
    session.add(mission)
    session.flush()  # the mission row must exist before the astronauts that reference it
    session.add_all(
        Astronaut(id=a.id, mission_id=mission_id, display_name=a.display_name, role=a.role)
        for a in ds.astronauts
    )
    session.flush()

    for a in ds.astronauts:
        records = [r for r in ds.records if r["astronaut_id"] == a.id]
        baselines = compute_baselines(records)
        session.add_all(
            VitalBaseline(astronaut_id=a.id, metric=b.metric, n=b.n, mean=b.mean, sd=b.sd, sd_effective=b.sd_effective)
            for b in baselines.values()
        )
        for r in records:
            statuses = assess_record(r, baselines)
            session.add(
                DailyRecord(
                    astronaut_id=a.id,
                    mission_id=mission_id,
                    mission_day=r["mission_day"],
                    date=date.fromisoformat(r["date"]),
                    phase=r["phase"],
                    metric_status=statuses,
                    status=worst(statuses.values()),
                    **{code: r[code] for code in METRIC_CODES},
                )
            )

    # Synthetic check-ins never overwrite one an astronaut sent from the app.
    taken = set(session.execute(select(CheckIn.astronaut_id, CheckIn.mission_day).where(CheckIn.mission_id == mission_id)).all())
    session.add_all(
        CheckIn(**c, source="synthetic")
        for c in ds.checkins
        if (c["astronaut_id"], c["mission_day"]) not in taken
    )

    session.add_all(
        SyntheticEvent(
            mission_id=mission_id,
            astronaut_id=e.astronaut_id,
            kind=e.kind,
            start_day=e.start_day,
            end_day=e.end_day,
            description=e.description,
            affected_metrics=sorted(set(e.effects) | ({"bone_density"} if e.extra_bone_loss_pct_per_30d else set())),
        )
        for e in ds.events
    )
    session.commit()
    return mission


def synthetic_is_empty(session: Session) -> bool:
    return (session.scalar(select(func.count()).select_from(DailyRecord)) or 0) == 0


def record_dict(r: DailyRecord) -> dict:
    return {
        "astronaut_id": r.astronaut_id,
        "mission_id": r.mission_id,
        "mission_day": r.mission_day,
        "date": r.date.isoformat(),
        "phase": r.phase,
        **{code: getattr(r, code) for code in METRIC_CODES},
    }


def get_records(
    session: Session,
    mission_id: str,
    astronaut_id: str | None = None,
    from_day: int | None = None,
    to_day: int | None = None,
) -> list[DailyRecord]:
    stmt = select(DailyRecord).where(DailyRecord.mission_id == mission_id)
    if astronaut_id:
        stmt = stmt.where(DailyRecord.astronaut_id == astronaut_id)
    if from_day is not None:
        stmt = stmt.where(DailyRecord.mission_day >= from_day)
    if to_day is not None:
        stmt = stmt.where(DailyRecord.mission_day <= to_day)
    return list(session.scalars(stmt.order_by(DailyRecord.astronaut_id, DailyRecord.mission_day)))


def get_baselines(session: Session, astronaut_id: str) -> dict[str, Baseline]:
    rows = session.scalars(select(VitalBaseline).where(VitalBaseline.astronaut_id == astronaut_id))
    return {b.metric: Baseline(b.metric, b.n, b.mean, b.sd, b.sd_effective) for b in rows}
