# Crew Health API

FastAPI service that pulls astronaut blood panels from the **NASA Open Science Data Repository (OSDR)**, cleans and normalizes them into a relational database, and serves them to the React Native crew app and to the crew-wide monitoring dashboard.

> **Not a medical device.** Reference ranges come from the labs that ran the tests; the deviation rules are illustrative.

## Data sources

All data comes from the SpaceX **Inspiration4** mission (3 days in low Earth orbit, September 2021, 4 crew members, de-identified as `C001`–`C004` in OSDR). Samples were collected at **L-92, L-44, L-3** (days before launch) and **R+1, R+45, R+82, R+194** (days after return).

| Dataset key | Study | File | Content |
|---|---|---|---|
| `cbc` | [OSD-569](https://osdr.nasa.gov/bio/repo/data/studies/OSD-569) | `LSDS-7_Complete_Blood_Count_CBC_TRANSFORMED.csv` | Complete blood count, 20 analytes, whole blood |
| `cmp` | [OSD-575](https://osdr.nasa.gov/bio/repo/data/studies/OSD-575) | `LSDS-8_Comprehensive_Metabolic_Panel_CMP_TRANSFORMED.csv` | Metabolic panel, 19 analytes, serum |
| `cardio` | [OSD-575](https://osdr.nasa.gov/bio/repo/data/studies/OSD-575) | `LSDS-8_Multiplex_serum_cardiovascular_EvePanel_TRANSFORMED.csv` | Multiplex cardiovascular panel, 9 analytes, serum |

Total: 84 samples, 48 analytes, 1,344 measurements.

The download URL is resolved through the study file listing (`/osdr/data/osd/files/<n>`), with the documented `geode-py` URL as fallback; the download redirects to a signed S3 URL. A copy of each file ships in `app/seed/`, so the API also starts offline (`INGEST_SOURCE=auto` falls back to it automatically).

## Quick start

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload            # http://localhost:8000/docs
pytest                                   # 28 tests
```

On first start the database is empty, so the API ingests the three files (from OSDR, or the bundled copy if OSDR is unreachable). Data lands in `data/crew_health.db` (SQLite).

With PostgreSQL:

```bash
docker compose up --build                # API on :8000, Postgres on the internal network
```

To run the tests against PostgreSQL: `TEST_DATABASE_URL=postgresql+psycopg://user@host:5432/empty_db pytest`.

## How the data is processed

1. **Parse.** The OSDR files are wide (one row per sample, 2–3 columns per analyte). They become one `Measurement` per sample and analyte. The parser handles the quirks in the published files:
   - missing values written as `""`, `NA` or `N/A` (eGFR has no upper limit, so `ref_max` is null);
   - an inconsistent column name (`alt_max_units_per_liter` has no `range_`);
   - analytes whose name is a prefix of another (`basophils` vs `absolute_basophils`);
   - hemoglobin labeled `percent` in the column name although values and range are **g/dL** (corrected, with a `unitNote`);
   - the same serum sample name in two panels (unique key is sample name + panel).
2. **Catalog.** Each analyte gets a display name, a readable unit and a body-system category (`immune`, `oxygen_transport`, `coagulation`, `kidney`, `liver`, `electrolytes`, `metabolism`, `inflammation`, `vascular`). Unknown columns in future file versions are still ingested under `other`.
3. **Flag against the reference range:** `low`, `normal`, `high`, `no_reference` (multiplex panel), `missing`.
4. **Personal baseline.** For each crew member and analyte, the mean and SD of the three pre-flight samples. Each value gets `deltaAbs`, `deltaPct` and `zScore` against it. With n = 3 the SD is unstable, so it never goes below 5% of the mean.
5. **Status** (what the apps should color by):

| Status | Rule |
|---|---|
| `out_of_range` | outside the lab reference range |
| `watch` | post-flight, inside the range (or no range), but ≥ 20% **and** ≥ 2 SD from the personal pre-flight mean |
| `normal` | everything else with a value |
| `no_data` | value missing in the source file |

Thresholds are configurable (`WATCH_PCT_CHANGE`, `WATCH_Z_SCORE`, `BASELINE_SD_FLOOR_FRACTION`). Pre-flight samples are never `watch`, because they are the baseline.

6. **Idempotent re-ingestion.** Each file is stored with its SHA-256; an unchanged file is skipped, a changed one replaces its samples in a single transaction. Every run is logged in `ingest_runs`.

## Data model

```
studies ─< source_files ─< samples ─< measurements >─ analytes
                              │              │
                         crew_members    timepoints
baseline_stats (crew_member × analyte)   ingest_runs
```

## Endpoints

JSON keys are **camelCase**. Interactive docs at `/docs`, OpenAPI schema at `/openapi.json`.
`timepoint` parameters accept a code (`R+1`, URL-encoded as `R%2B1`), `default` (first post-flight, R+1) or `latest`.

### Crew app (React Native)

| Method | Path | Use |
|---|---|---|
| GET | `/api/v1/crew?timepoint=` | crew list with overall status and counts |
| GET | `/api/v1/crew/{crewId}/summary?timepoint=&highlights=3` | home screen: status per body system, top measurements, alerts |
| GET | `/api/v1/crew/{crewId}/timeline` | status at every collection, for a timeline strip |
| GET | `/api/v1/crew/{crewId}/measurements?timepoint=&panel=&category=&status=` | detail list |
| GET | `/api/v1/crew/{crewId}/alerts?timepoint=` | alerts, most severe first |
| GET | `/api/v1/crew/{crewId}/analytes/{code}/series` | chart of one analyte: points, reference range, personal baseline |

### Dashboard

| Method | Path | Use |
|---|---|---|
| GET | `/api/v1/dashboard/overview?timepoint=` | totals, status per timepoint, crew cards, last ingestion |
| GET | `/api/v1/dashboard/alerts?timepoint=&severity=high\|medium` | alerts across the crew |
| GET | `/api/v1/dashboard/heatmap?timepoint=&metric=delta_pct\|z_score\|value&panel=&category=` | crew × analyte matrix |
| GET | `/api/v1/dashboard/analytes/{code}/trend` | one analyte for every crew member, plus crew mean/min/max |

### Catalog, query and export

| Method | Path | Use |
|---|---|---|
| GET | `/api/v1/studies` | OSDR studies and the ingested files (origin, URL, SHA-256) |
| GET | `/api/v1/timepoints` | collection timepoints with phase and mission day |
| GET | `/api/v1/analytes?panel=&category=` | analyte catalog |
| GET | `/api/v1/categories` | body-system categories |
| GET | `/api/v1/measurements?crewId=&timepoint=&phase=&panel=&category=&analyte=&status=&flag=&limit=&offset=` | generic paginated query |
| GET | `/api/v1/export/measurements.csv` | tidy CSV for notebooks or BI tools |
| POST | `/api/v1/ingest?source=auto\|remote\|bundled&force=false` | pull from OSDR again (needs `X-API-Key` if `ADMIN_API_KEY` is set) |
| GET | `/api/v1/ingest/runs` | ingestion history |
| GET | `/health` | liveness and `dataReady` |

### Example: `GET /api/v1/crew/C004/summary` (trimmed)

```json
{
  "crewId": "C004",
  "displayName": "Crew Member 4",
  "timepoint": { "code": "R+1", "phase": "postflight", "offsetDays": 1, "missionDay": 4, "label": "1 day after return" },
  "overallStatus": "out_of_range",
  "counts": { "outOfRange": 4, "watch": 2, "normal": 42, "noData": 0, "total": 48 },
  "categories": [
    {
      "category": "liver",
      "label": "Liver & proteins",
      "status": "out_of_range",
      "counts": { "outOfRange": 1, "watch": 1, "normal": 6, "noData": 0, "total": 8 },
      "highlights": [
        { "analyteCode": "ast", "analyteName": "Aspartate aminotransferase (AST)", "value": 48, "unit": "U/L",
          "refMin": 10, "refMax": 40, "flag": "high", "deltaPct": 21.0, "zScore": 2.1, "status": "out_of_range" }
      ]
    }
  ],
  "alerts": [
    { "severity": "high", "analyteCode": "ast",
      "message": "Aspartate aminotransferase (AST) 48 U/L is above the reference range (10–40)." }
  ]
}
```

### Consuming from React Native

```ts
const API_URL = "http://192.168.0.10:8000"; // the machine running the API, reachable from the phone

export async function getCrewSummary(crewId: string, timepoint = "default") {
  const res = await fetch(`${API_URL}/api/v1/crew/${crewId}/summary?timepoint=${encodeURIComponent(timepoint)}`);
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}
```

TypeScript types can be generated from the schema: `npx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.ts`.

On a physical device `localhost` is the phone itself; use the computer's LAN IP (or `10.0.2.2` on the Android emulator).

## Configuration

See `.env.example`. Main variables: `DATABASE_URL`, `INGEST_SOURCE`, `INGEST_ON_STARTUP`, `CORS_ORIGINS`, `ADMIN_API_KEY`, `WATCH_PCT_CHANGE`, `WATCH_Z_SCORE`.

## Limitations

- **Four people, seven timepoints.** The baseline uses three samples per person; z-scores are indicative, not statistical tests.
- **Multiplex panel.** Research-assay concentrations with no clinical reference range; the `relativePercent` column is passed through as reported. These analytes only ever get `watch` or `normal`, and vary a lot between pre-flight samples (e.g. CRP).
- **eGFR** is reported with the race-based equations used by the lab at the time; current practice (CKD-EPI 2021) no longer uses race.
- Tables are created with `create_all`; add Alembic migrations before changing the schema in a shared deployment.

## Project layout

```
app/
  main.py            FastAPI app, CORS, startup ingestion
  config.py          settings from environment variables
  db.py, models.py   SQLAlchemy engine and tables
  schemas.py         response models (camelCase)
  osdr/              datasets, OSDR client, CSV parser, analyte catalog
  services/          ingestion, analytics (flags, baselines), read queries
  routers/           crew, dashboard, catalog, ingest
  seed/              bundled copy of the three OSDR files
tests/               parser, OSDR client (mocked), API end-to-end
```
