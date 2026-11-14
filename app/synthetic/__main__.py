"""Write the synthetic mission to CSV without starting the API.

    python -m app.synthetic --out samples/synthetic_daily_vitals.csv --seed 42
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from app.synthetic.assess import assess_record, compute_baselines, worst
from app.synthetic.generator import generate
from app.synthetic.metrics import METRICS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="samples/synthetic_daily_vitals.csv")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    ds = generate(args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    columns = ["astronaut_id", "mission_id", "mission_day", "date", "phase"] + [m.column for m in METRICS] + ["status"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for a in ds.astronauts:
            records = [r for r in ds.records if r["astronaut_id"] == a.id]
            baselines = compute_baselines(records)
            for r in records:
                status = worst(assess_record(r, baselines).values())
                writer.writerow(
                    [r["astronaut_id"], r["mission_id"], r["mission_day"], r["date"], r["phase"]]
                    + ["" if r[m.code] is None else r[m.code] for m in METRICS]
                    + [status]
                )
    events = out.with_name(out.stem + "_events.json")
    events.write_text(
        json.dumps(
            [
                {
                    "astronaut_id": e.astronaut_id, "kind": e.kind, "start_day": e.start_day,
                    "end_day": e.end_day, "description": e.description,
                }
                for e in ds.events
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(ds.records)} rows to {out} and the ground truth to {events}")


if __name__ == "__main__":
    main()
