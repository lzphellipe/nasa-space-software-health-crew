# Crew Health API

FastAPI service that serves astronaut health data to the React Native crew app, the crew-wide monitoring dashboard and the AI agent. It has three data sets, kept apart because they describe different people:

1. **Real data from NASA OSDR**: blood panels of the four Inspiration4 crew members, before and after flight (`/api/v1/crew`, `/api/v1/dashboard`).
2. **Real data from the AGBRESA bed rest study** (DLR/ESA/NASA): heart rate, blood pressure, sleepiness, motion sickness and mood of 24 volunteers over 60 days of head-down bed rest, an Earth analog of microgravity (`/api/v1/analog/agbresa`).
3. **Synthetic daily vitals** for a 6-month mission with four astronauts, generated because no open source has daily vital signs for any crew (`/api/v1/missions`).

On top of the synthetic mission, the API runs the crew monitoring itself: login per astronaut, daily check-ins (stress, fatigue, symptoms), pattern detection over a rolling 7-day window, proactive messages (at most one every 7 days, can be turned off), routing to human support, and keyword search in PDF documents. There is no AI agent in this service.

> **Not a medical device.** Reference ranges come from the labs that ran the tests; the deviation rules are illustrative.

## Data sources (OSDR)

The real data comes from the SpaceX **Inspiration4** mission (3 days in low Earth orbit, September 2021, 4 crew members, de-identified as `C001`–`C004` in OSDR). Samples were collected at **L-92, L-44, L-3** (days before launch) and **R+1, R+45, R+82, R+194** (days after return).

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
pytest                                   # 67 tests
```

On first start the database is empty, so the API ingests the three OSDR files and the two AGBRESA files (from NASA and PLOS, or the bundled copies if they are unreachable) and generates the synthetic mission. Data lands in `data/crew_health.db` (SQLite).

On Windows, if port 8000 is blocked (`WinError 10013`), use another one: `uvicorn app.main:app --reload --port 8080`.

With PostgreSQL:

```bash
docker compose up --build                # API on :8000, Postgres on the internal network
```

To run the tests against PostgreSQL: `TEST_DATABASE_URL=postgresql+psycopg://user@host:5432/empty_db pytest`.

## Crew monitoring

### Authentication

Astronauts and the care team log in and send the token as `Authorization: Bearer <token>`.

```http
POST /api/v1/auth/login
{ "username": "astronaut2", "password": "spaceapps2026" }
→ { "accessToken": "...", "tokenType": "bearer", "expiresIn": 43200, "user": { "role": "astronaut", "astronautId": "A002", ... } }
```

| Role | Can see |
|---|---|
| `astronaut` | only their own astronaut: `/api/v1/me/*` and `/api/v1/missions/{id}/astronauts/{own id}/*` (403 for anyone else) |
| `medical` | the whole crew: `/api/v1/care/*`, mission dashboard, series, episodes, events, exports; PDF uploads |
| `admin` | everything `medical` can, plus `POST /api/v1/missions/regenerate` |

Public research data (OSDR `/api/v1/crew`, `/api/v1/dashboard`, catalog; AGBRESA) stays open without a token. In `/docs`, use **Authorize** with a username and password.

Demo accounts (created at startup while `SEED_DEMO_USERS=true`, password `DEMO_PASSWORD`, default `spaceapps2026`):

| Username | Role | Astronaut |
|---|---|---|
| `astronaut1` … `astronaut4` | astronaut | `A001` … `A004` |
| `surgeon` | medical | all |
| `admin` | admin | all |

Passwords are stored as PBKDF2-SHA256 hashes; tokens are HS256 JWTs signed with `JWT_SECRET` (set it to a long random value; without it, a random one is used and tokens stop working after a restart). **Change the demo password or turn the demo accounts off outside a demo.**

### Mission clock

The synthetic mission runs from day −30 to 180. "Today" is `CURRENT_MISSION_DAY` (default: 180, the last day). New check-ins and support requests are stamped with that day.

### Check-ins

`POST /api/v1/me/checkins`, one per mission day (sending again the same day replaces it):

```json
{ "stress": 4, "fatigue": 3, "symptoms": ["headache", "insomnia"], "notes": "Busy EVA prep week" }
```

- `stress`, `fatigue`: 1 very low … 5 very high.
- `symptoms`: codes from `GET /api/v1/me/checkins/options` (labels included). Four are **urgent**: `chest_pain`, `shortness_of_breath`, `fainting`, `severe_headache`. Reporting one opens an urgent support request immediately, and the response tells the astronaut so.
- The synthetic mission ships with ~750 check-ins that follow the vitals and the labeled events (e.g. A002 reports congestion, sore throat and high fatigue during the infection; A004 high stress and trouble sleeping during the sleep disruption).

### Pattern detection (rolling 7-day window)

For each astronaut and day D, rules look at days D−6 … D of vitals and check-ins (`app/services/patterns.py`). Every finding has a severity and its evidence.

| Pattern | Rule (7-day window) |
|---|---|
| `sleep_debt` | mean sleep < 6 h or ≥ 4 nights < 6 h (high: < 5 h or ≥ 6 nights) |
| `elevated_stress` | mean stress ≥ 3.5 (high ≥ 4.2); low: ≥ 3 and rising |
| `fatigue` | mean fatigue ≥ 3.5 (high ≥ 4.2) |
| `low_mood` | mean mood ≤ 2.5 (high ≤ 1.8) |
| `wellbeing_cluster` | sleep debt + elevated stress + low mood together (high) |
| `fever` | any day ≥ 38 °C (high); ≥ 2 days ≥ 37.5 °C (medium) |
| `rising_blood_pressure` | ≥ 3 days ≥ 135/87 (high: ≥ 3 days ≥ 140/90) |
| `elevated_glucose` | mean ≥ 100 (low), ≥ 110 (medium) mg/dL; any ≥ 126 (high) |
| `elevated_heart_rate` | mean ≥ 8 bpm and 2 SD above pre-flight |
| `low_intake` | calories ≥ 25 % (high: 40 %) below pre-flight |
| `recurring_symptom` | same symptom on ≥ 3 days (medium on ≥ 5) |
| `urgent_symptom` | any urgent symptom (high) |
| `missed_checkins` | fewer than 3 check-ins (low) |

**Scheduling.** At startup the whole mission history is replayed day by day; then a background task repeats the cycle for the current mission day every `DETECTION_INTERVAL_MINUTES` (default 60). The care team can run it now with `POST /api/v1/care/patterns/run?day=`. Each cycle: detect → route to the care team → send proactive messages.

On the synthetic mission it finds the four labeled events: A002 fever (days 47–59), A003 rising blood pressure (from day ~107), A004 short sleep + stress + low mood (days 102–133), A001 recurring back pain while the exercise device is out.

### Proactive messages

- Built from templates, no AI: what was seen, one practical suggestion, and the options to ask for support or turn the messages off.
- **At most one message per astronaut every 7 mission days** (`PROACTIVE_MIN_INTERVAL_DAYS`), and only for medium or high findings.
- **Opt-out:** `PUT /api/v1/me/preferences { "proactiveMessages": false }`. Opting out stops messages only; the care team still sees the patterns and urgent symptoms are still routed.
- `GET /api/v1/me/messages`, `POST /api/v1/me/messages/{id}/read` or `/dismiss`.

### Human support

Requests reach the care team queue (`GET /api/v1/care/support`, urgent and open first) from three places:

| Reason | How | Priority |
|---|---|---|
| `astronaut_request` | `POST /api/v1/me/support { "topic": "psychological", "message": "...", "urgent": false }`; topics: medical, psychological, family, operational, other | routine or urgent |
| `urgent_symptom` | urgent symptom in a check-in | urgent |
| `pattern` | high-severity fever, blood pressure, glucose, low mood or wellbeing cluster | routine |

Automatic requests are not duplicated while one for the same reason is still open. The care team answers with `PATCH /api/v1/care/support/{id}` (`status` open → acknowledged → resolved, `assignedTo`, `response` shown to the astronaut, `careNotes` never shown to the astronaut). If `SUPPORT_WEBHOOK_URL` is set (e.g. a Slack or Teams incoming webhook), each new request is also posted there.

### PDF search

- `GET /api/v1/knowledge/search?q=sleep schedule fatigue&limit=5` returns the best passages with document, **page** and a snippet (BM25 keyword ranking; tables of contents are down-weighted). Search in the document's language: the bundled standard is in English.
- `GET /api/v1/knowledge/documents/{id}/pages/{page}` returns the full text of a page.
- Bundled: **NASA-STD-3001 Volume 1, Revision C: Crew Health** (2023, 87 pages, public release).
- The care team can upload more PDFs: `POST /api/v1/knowledge/documents` (multipart `file`, optional `title`, max 20 MB). Scanned PDFs without a text layer are rejected.

### Endpoints

| Method | Path | Who |
|---|---|---|
| POST | `/api/v1/auth/login`, `/api/v1/auth/token` | anyone |
| GET | `/api/v1/auth/me` | logged in |
| GET | `/api/v1/me` | astronaut: profile, mission day, unread messages, open requests |
| GET | `/api/v1/me/checkins/options` | astronaut |
| POST, GET | `/api/v1/me/checkins` | astronaut |
| GET | `/api/v1/me/patterns?day=` | astronaut |
| GET | `/api/v1/me/messages`, POST `/api/v1/me/messages/{id}/read\|dismiss` | astronaut |
| GET, PUT | `/api/v1/me/preferences` | astronaut |
| POST, GET | `/api/v1/me/support` | astronaut |
| GET | `/api/v1/care/support?status=&astronautId=`, PATCH `/api/v1/care/support/{id}` | care team |
| GET | `/api/v1/care/checkins?astronautId=&fromDay=&toDay=` | care team |
| GET | `/api/v1/care/patterns?day=` or `?fromDay=&toDay=`, `&astronautId=&minSeverity=` | care team |
| GET | `/api/v1/care/messages?astronautId=` | care team |
| POST | `/api/v1/care/patterns/run?day=` | care team |
| GET | `/api/v1/knowledge/documents`, `/search`, `/documents/{id}/pages/{page}` | logged in |
| POST | `/api/v1/knowledge/documents` | care team |

## AGBRESA bed rest study (real data, Earth analog)

Supplementary files S1 and S2 of Frett T. et al. (2020), *Tolerability of daily intermittent or continuous short-arm centrifugation during 60-day 6° head down bed rest (AGBRESA study)*, PLoS ONE 15(9): e0239228, [doi:10.1371/journal.pone.0239228](https://doi.org/10.1371/journal.pone.0239228), CC BY 4.0. The API downloads them from PLOS; copies ship in `app/seed/agbresa/`.

**These are healthy volunteers in bed rest on Earth, not astronauts.** Every response carries a `notice` saying so.

| | |
|---|---|
| Volunteers | 24 (16 men, 8 women), IDs `AGB-A` … `AGB-Z` |
| Groups | `intermittent` (6 × 5 min centrifuge daily), `continuous` (30 min daily), `control` (no centrifuge), 8 each |
| Duration | 60 days of 6° head-down bed rest, 2019, DLR :envihab (Cologne) |
| S2, cardiovascular | systolic and diastolic BP, heart rate **before** (`pre`), **2 min into** (`during`) and **after** (`post`) the centrifuge run, on bed rest days 1, 30 and 60 (centrifuge groups) |
| S1, questionnaires | sleepiness (ESS, 0–24), motion sickness (MSAQ, % of max, 5 scores) and mood (PANAS positive/negative, 10–50), before and after the run on days 1, ~29 and ~57; control group: overall MSAQ only, once a week (`single`) |

### What was decoded and cleaned

- **Group codes are not in the paper.** They were decoded from the data: `1 = intermittent`, `2 = continuous`, `3 = control`. With this mapping the per-group means of S2 reproduce Table 1 of the paper (e.g. heart rate during centrifugation, continuous group: 82.4 → 99.1 → 100.5 bpm). Sex: `1 = male`, `2 = female` (matches the 16/8 split). The age-group cut-off is not published, so `ageGroup` is passed through as 1 or 2.
- **Same volunteer, two spellings:** `0A` in S1 is `A` in S2; the files' own numeric IDs differ and are not used.
- **Four impossible values are withheld** (`value: null`, `status: "invalid"`, the original kept in `rawValue` with a `qualityNote`; list at `/quality`): systolic BP 14 mmHg with diastolic 67 (AGB-Y, day 1), ESS 25 (scale max 24), MSAQ peripheral 107 % and PANAS positive 9 (scale min 10). The BP value is in the published Table 1 too: it is why the continuous group's day-1 systolic reads 111.9 ± 41; without it, it is 125.9 ± 11.6.
- **Control group:** its one weekly MSAQ value appears in both the PRE and POST columns; it is stored once, as `single`.
- SPSS artefacts (`filter_$`, `VAR00002`) and the precomputed `Delta_*` columns are ignored; changes are recomputed.

### Status

| Metric | Watch | Out of range | Applies to |
|---|---|---|---|
| Systolic BP | ≥ 130 | ≥ 140 or < 90 | `pre`, `post` |
| Diastolic BP | ≥ 85 | ≥ 90 or < 60 | `pre`, `post` |
| Heart rate | ≥ 90 | ≥ 101 or < 45 | `pre`, `post` |
| Sleepiness (ESS) | ≥ 11 | ≥ 16 | `pre`, `post` |
| MSAQ, PANAS | | | no clinical cut-off: `no_reference` |

Values measured `during` centrifugation are a deliberate cardiovascular load, so resting thresholds do not apply (`no_reference`). Every value also gets `pctVsFirst`: the change from the same volunteer's first session.

### Endpoints

| Method | Path | Use |
|---|---|---|
| GET | `/api/v1/analog/agbresa` | study, design, groups, metrics, source files, invalid-value count |
| GET | `/api/v1/analog/agbresa/subjects?group=` | volunteers with their sessions and latest status |
| GET | `/api/v1/analog/agbresa/subjects/{id}` | history session by session: `values`, `metricStatus` and `pctVsFirst`, keyed by metric then moment |
| GET | `/api/v1/analog/agbresa/subjects/{id}/observations?metric=&moment=` | raw observations |
| GET | `/api/v1/analog/agbresa/groups/compare?metric=heart_rate&moment=during` | mean, SD, min, max per group at the begin, middle and end of bed rest |
| GET | `/api/v1/analog/agbresa/quality` | withheld values with the reason |
| GET | `/api/v1/analog/agbresa/export.csv` | all observations as CSV |

Example, `GET /api/v1/analog/agbresa/subjects/Y` (one session, trimmed):

```json
{
  "notice": "Real data from healthy volunteers in a 60-day head-down-tilt bed rest study on Earth ...",
  "subject": { "id": "AGB-Y", "group": "continuous", "sex": "male", "campaign": "AGBR2", "latestStatus": "watch" },
  "sessions": [
    {
      "session": "HDT1", "phase": "begin", "bedRestDay": 1, "status": "watch",
      "values": { "systolic_bp": { "pre": 119.0, "during": null, "post": 119.0 }, "heart_rate": { "pre": 61.0, "during": 84.0, "post": 58.0 } },
      "metricStatus": { "systolic_bp": { "pre": "normal", "during": "invalid", "post": "normal" } },
      "pctVsFirst": { "heart_rate": { "pre": 0.0, "during": 0.0, "post": 0.0 } }
    }
  ]
}
```

## Synthetic mission: daily vitals for the AI agent

OSDR has no daily blood pressure, heart rate, glucose, temperature, sleep or mood for any crew, so the API generates them (`app/synthetic/`). Every response carries `synthetic: true` or a `dataNotice`.

| | |
|---|---|
| Mission | `SYN-LDM-01`, one mission, launch on 2027-03-01 (illustrative date) |
| Crew | `A001`–`A004` (Commander, Pilot, Mission Specialists 1 and 2) |
| Days | `missionDay` −30 to 180: a 30-day pre-flight baseline, then 180 days in flight |
| Rows | 4 × 211 = 844 daily records |
| Seed | deterministic (`SYNTHETIC_SEED=42`); `POST /api/v1/missions/regenerate?seed=` makes a new one |

### Columns

| Column (CSV) | Metric code (API) | Unit | Required | Cadence |
|---|---|---|---|---|
| `astronaut_id` | | | ✓ | |
| `mission_id` | | | ✓ | |
| `mission_day` | | day | ✓ | −30 … 180 |
| `systolic_bp_mmhg` | `systolic_bp` | mmHg | ✓ | daily |
| `diastolic_bp_mmhg` | `diastolic_bp` | mmHg | ✓ | daily |
| `heart_rate_bpm` | `heart_rate` | bpm | ✓ | daily |
| `blood_glucose_mg_dl` | `blood_glucose` | mg/dL | ✓ | daily |
| `body_temperature_c` | `body_temperature` | °C | ✓ | daily |
| `sleep_hours` | `sleep_hours` | h | ✓ | daily |
| `mood_score` | `mood` | 1–5 | ✓ | daily |
| `bone_density_g_cm2` | `bone_density` | g/cm² (hip) | | 2 pre-flight scans, then every 30 days |
| `calorie_intake_kcal` | `calorie_intake` | kcal | | daily |
| `protein_intake_g` | `protein_intake` | g | | daily |
| `wbc_10e3_per_ul` | `white_blood_cells` | ×10³/µL (immune system) | | weekly |

Blood pressure is split into systolic and diastolic. Optional metrics are `null` on days without a measurement. A ready-made copy is in `samples/synthetic_daily_vitals.csv` (regenerate with `python -m app.synthetic --seed 42`).

### How the values are built

`value = personal baseline + mission adaptation + labeled event + autocorrelated noise`

The in-flight adaptations follow effects reported for ~6-month ISS stays, as orders of magnitude: blood pressure a few mmHg lower in the first week (fluid shift), sleep around 6 h, body temperature drifting up ~0.5 °C, fasting glucose creeping up, hip bone density falling ~1.2 % per month, eating ~85 % of pre-flight intake, and a mood dip in the third quarter of the mission.

Each astronaut gets one **labeled event**, the ground truth for checking whether the AI agent finds it (`GET /api/v1/missions/SYN-LDM-01/events`):

| Astronaut | Event | Days | Signature |
|---|---|---|---|
| A002 | respiratory infection | 47–53 | fever (~38.4 °C), higher HR, white cells up, poor sleep and appetite |
| A003 | hypertension trend | 60–180 | systolic and diastolic rising steadily, above 140/90 near the end |
| A004 | sleep disruption | 100–128 | sleep ~4–5 h, mood 1–2, slightly higher HR |
| A001 | exercise device unavailable | 120–150 | fasting glucose ~110, higher HR, faster bone loss |

### Status rules

Each metric of each day gets `normal`, `watch` or `out_of_range` (thresholds in `GET /metrics`):

| Metric | Watch | Out of range |
|---|---|---|
| Systolic BP | ≥ 130 | ≥ 140 or < 90 |
| Diastolic BP | ≥ 85 | ≥ 90 or < 60 |
| Heart rate | ≥ 90 | ≥ 101 or < 45 |
| Fasting glucose | ≥ 100 | ≥ 126 or < 70 |
| Body temperature | ≥ 37.5 °C | ≥ 38.0 °C or < 35.5 °C |
| Sleep | < 6 h | < 5 h |
| Mood | ≤ 2 | 1 |
| Bone density | ≥ 5 % below own baseline | ≥ 10 % below |
| Calories / protein | ≥ 25 % / 30 % below own baseline | ≥ 40 % below |
| White blood cells | | > 11 or < 4 ×10³/µL |

Consecutive abnormal measurements of a metric are grouped into **episodes** (start, end, peak).

### AI agent context

`GET /api/v1/missions/SYN-LDM-01/astronauts/{id}/agent-context?windowDays=90&untilDay=180&includeDaily=true`

One call returns everything an agent needs about one astronaut over a window (default: the **last 90 days**, i.e. days 91–180):

- `metrics`: definitions, units and thresholds;
- `baseline`: 30-day pre-flight mean and SD per metric;
- `summary`: per metric, window mean/min/max/last, change vs baseline (% and z), **trend per 30 days**, days in watch and out of range;
- `weekly`: weekly means and the worst status of each week;
- `episodes`: abnormal stretches with their peak;
- `highlights`: short factual sentences, ready to put in a prompt;
- `daily`: the raw daily records (omit with `includeDaily=false` to save tokens).

Ground-truth events are deliberately **not** included, so they can be used to score the agent. Move `untilDay` to simulate the agent running on any mission day (e.g. `untilDay=60` during the infection).

### Endpoints

| Method | Path | Use |
|---|---|---|
| GET | `/api/v1/missions` | the mission (flagged `synthetic`) |
| GET | `/api/v1/missions/{id}/metrics` | metric definitions and thresholds |
| GET | `/api/v1/missions/{id}/astronauts` | crew |
| GET | `/api/v1/missions/{id}/astronauts/{aid}/today?day=` | app home: one day vs baseline, latest bone/WBC values, active episodes |
| GET | `/api/v1/missions/{id}/astronauts/{aid}/daily?fromDay=&toDay=` | daily records |
| GET | `/api/v1/missions/{id}/astronauts/{aid}/baseline` | 30-day pre-flight baseline |
| GET | `/api/v1/missions/{id}/astronauts/{aid}/episodes?fromDay=&toDay=` | episodes of one astronaut |
| GET | `/api/v1/missions/{id}/astronauts/{aid}/agent-context?windowDays=90&untilDay=&includeDaily=` | AI agent context |
| GET | `/api/v1/missions/{id}/dashboard?day=` | crew status on a day, plus status counts for every day |
| GET | `/api/v1/missions/{id}/metrics/{metric}/series?fromDay=&toDay=` | one metric for the 4 astronauts, with baselines and crew mean |
| GET | `/api/v1/missions/{id}/episodes?status=&minMeasurements=` | episodes across the crew |
| GET | `/api/v1/missions/{id}/events` | ground-truth events (evaluation only) |
| GET | `/api/v1/missions/{id}/export.csv` | all daily records as CSV |
| POST | `/api/v1/missions/regenerate?seed=` | regenerate and replay pattern detection (admin) |

Daily records come as `values` and `metricStatus` objects keyed by metric code, so the app can render any metric from the `/metrics` list:

```json
{
  "astronautId": "A002", "missionId": "SYN-LDM-01", "missionDay": 49, "date": "2027-04-19", "phase": "inflight",
  "values": { "systolic_bp": 111, "heart_rate": 71, "body_temperature": 38.43, "sleep_hours": 5.8, "mood": 3, "white_blood_cells": 11.3, "...": "..." },
  "metricStatus": { "body_temperature": "out_of_range", "white_blood_cells": "out_of_range", "protein_intake": "out_of_range", "sleep_hours": "watch", "...": "..." },
  "status": "out_of_range"
}
```

## OSDR blood panels

### How the data is processed

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

### Data model

```
studies ─< source_files ─< samples ─< measurements >─ analytes
                              │              │
                         crew_members    timepoints
baseline_stats (crew_member × analyte)   ingest_runs

missions ─< astronauts ─< daily_records
                     ├─< vital_baselines
                     └─< synthetic_events (ground truth)

studies (AGBRESA) ─< source_files ─< analog_observations >─ analog_subjects

users   astronaut_preferences   checkins   pattern_detections   proactive_messages   support_requests
knowledge_documents ─< knowledge_chunks
```

### Endpoints (OSDR)

JSON keys are **camelCase**. Interactive docs at `/docs`, OpenAPI schema at `/openapi.json`.
`timepoint` parameters accept a code (`R+1`, URL-encoded as `R%2B1`), `default` (first post-flight, R+1) or `latest`.

#### Crew app (React Native)

| Method | Path | Use |
|---|---|---|
| GET | `/api/v1/crew?timepoint=` | crew list with overall status and counts |
| GET | `/api/v1/crew/{crewId}/summary?timepoint=&highlights=3` | home screen: status per body system, top measurements, alerts |
| GET | `/api/v1/crew/{crewId}/timeline` | status at every collection, for a timeline strip |
| GET | `/api/v1/crew/{crewId}/measurements?timepoint=&panel=&category=&status=` | detail list |
| GET | `/api/v1/crew/{crewId}/alerts?timepoint=` | alerts, most severe first |
| GET | `/api/v1/crew/{crewId}/analytes/{code}/series` | chart of one analyte: points, reference range, personal baseline |

#### Dashboard

| Method | Path | Use |
|---|---|---|
| GET | `/api/v1/dashboard/overview?timepoint=` | totals, status per timepoint, crew cards, last ingestion |
| GET | `/api/v1/dashboard/alerts?timepoint=&severity=high\|medium` | alerts across the crew |
| GET | `/api/v1/dashboard/heatmap?timepoint=&metric=delta_pct\|z_score\|value&panel=&category=` | crew × analyte matrix |
| GET | `/api/v1/dashboard/analytes/{code}/trend` | one analyte for every crew member, plus crew mean/min/max |

#### Catalog, query and export

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

#### Example: `GET /api/v1/crew/C004/summary` (trimmed)

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

## Consuming from React Native

```ts
const API_URL = "http://192.168.0.10:8000"; // the machine running the API, reachable from the phone

export async function login(username: string, password: string): Promise<string> {
  const res = await fetch(`${API_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error("Invalid username or password");
  return (await res.json()).accessToken; // keep it in SecureStore
}

export async function api(path: string, token?: string, init: RequestInit = {}) {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers },
  });
  if (res.status === 401) throw new Error("Session expired"); // send the user back to login
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}

// const token = await login("astronaut2", "spaceapps2026");
// await api("/api/v1/me/checkins", token, { method: "POST", body: JSON.stringify({ stress: 3, fatigue: 2, symptoms: [] }) });
// const crew = await api("/api/v1/crew"); // public OSDR data, no token needed
```

TypeScript types can be generated from the schema: `npx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.ts`.

On a physical device `localhost` is the phone itself; use the computer's LAN IP (or `10.0.2.2` on the Android emulator).

## Configuration

See `.env.example`. Main variables: `DATABASE_URL`, `JWT_SECRET`, `SEED_DEMO_USERS`, `DEMO_PASSWORD`, `CURRENT_MISSION_DAY`, `SCHEDULER_ENABLED`, `DETECTION_INTERVAL_MINUTES`, `PROACTIVE_MIN_INTERVAL_DAYS`, `SUPPORT_WEBHOOK_URL`, `INGEST_SOURCE`, `CORS_ORIGINS`, `ADMIN_API_KEY`, `SYNTHETIC_SEED`.

## Limitations

- **Pattern rules and messages are illustrative.** Thresholds are generic screening values and the message texts were not reviewed by a clinician or psychologist. They show how the system works; they are not medical criteria.
- **The scheduler runs inside the API process.** With several workers, each runs it; use one worker or move the cycle to a separate job for production.
- **No AI agent.** `/agent-context` only prepares data; nothing in this service calls a language model.
- **AGBRESA is an analog.** Bed rest reproduces some effects of microgravity (fluid shift, deconditioning) in healthy volunteers on Earth; it is not spaceflight, and these are not the Inspiration4 crew. Do not join it with the other data sets as if it described the same people.
- **Synthetic mission.** The daily vitals are generated, not measured. Effects follow published orders of magnitude for long ISS stays, but the individual curves, events and thresholds are illustrative. Use the data to build and evaluate the agent and the apps, not to draw physiological conclusions.
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
  schemas_missions.py response models for the synthetic mission
  osdr/              datasets, OSDR client, CSV parser, analyte catalog
  analog/            AGBRESA files, metric definitions, xlsx parser and decoding
  auth/              password hashing, JWT, role and per-astronaut access checks
  knowledge/         bundled PDF (NASA-STD-3001 Vol. 1 Rev. C)
  synthetic/         metric definitions, generator, check-in catalog and history, assessment; `python -m app.synthetic`
  services/          ingestion, analytics, synthetic store, patterns, proactive messages, support, monitoring cycle, PDF search, users
  routers/           auth, me, care, crew, dashboard, missions, analog, knowledge, catalog, ingest
  seed/              bundled copies of the three OSDR files and the two AGBRESA files
samples/             synthetic_daily_vitals.csv and its ground-truth events
tests/               parsers, download clients (mocked), API end-to-end, synthetic data, AGBRESA, monitoring and access control
```
