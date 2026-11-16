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
| `immune` | [OSD-575](https://osdr.nasa.gov/bio/repo/data/studies/OSD-575) | `LSDS-8_Multiplex_serum_immune_EvePanel_TRANSFORMED.csv` | **Research:** immune cytokine array (Eve HD71), 71 analytes, pg/mL, serum |
| `alamar` | [OSD-575](https://osdr.nasa.gov/bio/repo/data/studies/OSD-575) | `LSDS-8_Multiplex_serum.immune.AlamarPanel_TRANSFORMED.csv` | **Research:** inflammation panel (Alamar NULISAseq), 203 proteins, NPQ (log2), serum |

Clinical panels: 84 samples, 48 analytes, 1,344 measurements. Research panels: 55 samples (28 Eve, 27 Alamar: C003 has no L-44), 274 analytes, 7,469 measurements.

**Research panels** (`immune`, `alamar`) are hundreds of proteins without clinical reference ranges. They are stored and fully queryable (`/analytes?panel=`, `/measurements?panel=`, `/crew/{id}/measurements?panel=`, `/crew/{id}/analytes/{code}/series`, `/dashboard/heatmap?panel=`, the CSV export), but **left out of the crew status, summaries, alerts and the overview counts**, so they do not drown the 48 clinical analytes. The overview lists them in `researchPanels`.

- Alamar codes are prefixed `alamar_` (e.g. `alamar_il6`): EGF, LIF and TSLP are measured by both arrays.
- Alamar values are NPQ, a log2 scale (+1 NPQ is about twice as much). `deltaAbs` is in NPQ, `deltaPct` is the fold change, `(2^deltaAbs − 1) × 100`, and `watch` needs |deltaAbs| ≥ 1 (2-fold) and |z| ≥ 2.
- None of the OSD-575 serum panels has in-flight samples: only L-92, L-44, L-3 and R+1…R+194.
- A database created by an older version gets new datasets on the next start; unchanged files are skipped by their SHA-256.

The download URL is resolved through the study file listing (`/osdr/data/osd/files/<n>`), with the documented `geode-py` URL as fallback; the download redirects to a signed S3 URL. A copy of each file ships in `app/seed/`, so the API also starts offline (`INGEST_SOURCE=auto` falls back to it automatically).

## Quick start

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload            # http://localhost:8000/docs
pytest
```

On first start the database is empty, so the API ingests the five OSDR files and the two AGBRESA files (from NASA and PLOS, or the bundled copies if they are unreachable) and generates the synthetic mission. Data lands in `data/crew_health.db` (SQLite).

On Windows, if port 8000 is blocked (`WinError 10013`), use another one: `uvicorn app.main:app --reload --port 8080`.

With PostgreSQL:

```bash
docker compose up --build                # API on :8000, Postgres on the internal network
```

To run the tests against PostgreSQL: `TEST_DATABASE_URL=postgresql+psycopg://user@host:5432/empty_db pytest`.

## CosmoMedic app (`/api/v1/app`)

The CosmoMedic frontend (React Native app + ground web dashboard) has its own data model, `docs/data/data-model.json`, and demo data, `docs/data/mock-db.json`. The API loads that demo data into its own tables (`app_*`) on first start and serves every screen in that exact format, so the app can swap its mock for the API without changing its types. The backend computes what the mock had frozen: daily score, body areas, alerts, Orbi notes, coins, "logged today", message status.

- **Crew:** Ana Souza `ast-01` (`ORB3-ANA`), Raj Patel `ast-02` (`ORB3-RAJ`), Mika Tanaka `ast-03` (`ORB3-MIKA`), Leo Martins `ast-04` (`ORB3-LEO`), with the profile, baseline, avatar, coins, items and history of `mock-db.json`. PIN `2026` for all four (`APP_DEMO_PIN`).
- **Test account:** Crew ID `TEST`, PIN `0000` (`APP_TEST_PIN`): 999,999 coins to try the whole shop, onboarding already done, no logs. It is `ast-test` and stays out of the ground crew table and of the ranking.
- **Mission Control:** Crew ID `GROUND`, PIN `2026` (`APP_GROUND_PIN`), through the same login. It sees `/api/v1/app/ground/*`.
- **Mission:** Orbit-3, low Earth orbit, 30 days, today is **day 12** (`APP_MISSION_DAY`: empty = the mission's own day, a number, or `auto` = days since `startDate`). Communication delay 1,200 s (20 min) each way.
- **Ground team:** the care-team accounts (`surgeon` and `admin`) also see `/api/v1/app/ground/*`, through `/api/v1/auth/login` or the app login. Astronauts get 403 there; each astronaut only sees their own data under `/api/v1/app/me`.

### Login

```http
POST /api/v1/app/auth/login
{"crewId": "ORB3-ANA", "pin": "2026"}
→ {"accessToken": "…", "tokenType": "bearer", "expiresIn": 1800,
   "refreshToken": "…", "refreshExpiresIn": 2592000, "role": "astronaut",
   "astronaut": {"id": "ast-01", "name": "Ana Souza", "crewId": "ORB3-ANA", "onboardingDone": true}, "next": "/home"}
```

- The access token lasts 30 minutes. Renew it with `POST /api/v1/auth/refresh {"refreshToken": "…"}`: it returns a new pair, and the refresh token used is no longer valid (keep the newest one). The refresh token lasts 30 days.
- `role` is `astronaut` or `ground`. For `GROUND`, `astronaut` is `null` and `next` is `/ground`.

### Endpoints

| Screen | Method and route |
|---|---|
| Login | `POST /app/auth/login` |
| Onboarding, profile, devices | `GET /app/me/profile` · `POST /app/me/onboarding` · `PATCH /app/me/devices` |
| Home (x/6 logs, coins, status tag, Orbi note, unread messages) | `GET /app/me/home` |
| Crew status (score, 7-day scrubber, body areas, callouts) | `GET /app/me/status?day=` |
| Daily summaries | `GET /app/me/summaries?fromDay=&toDay=` |
| Save / edit / list logs | `POST /app/me/logs` · `PUT /app/me/logs/{id}` · `GET /app/me/logs?type=&fromDay=&toDay=` |
| Module overview (mood, sleep, radiation, body, meals, exercise, relax) | `GET /app/me/logs/{module}/overview` |
| Alerts | `GET /app/me/alerts?status=active\|resolved\|all` |
| Orbi notes | `GET /app/me/orbi-notes` · `POST /app/me/orbi-notes/{id}/read\|dismiss` |
| Notification preferences (opt-out) | `GET`/`PUT /app/me/preferences` |
| Message Mission Control | `GET`/`POST /app/me/ground-messages` · `POST /app/me/ground-messages/{id}/read` |
| Chat with Orbi | `POST /app/me/orbi/chat` · `GET /app/me/orbi/messages` |
| Device sync (wearable, cuff, dosimeter, EVA suit) | `POST /app/me/telemetry` · `POST /app/me/telemetry/simulate` · `GET /app/me/telemetry` · `GET /app/me/devices/status` · `GET /app/telemetry/metrics` |
| Orbi confirmations (confirm a reading, quick checks) | `GET /app/me/confirmations` · `POST /app/me/confirmations/{id}/confirm\|answer\|dismiss` |
| Orbi reflection (journal + numbers) and consultation | `GET /app/me/orbi/reflection?ai=` · `POST`/`GET /app/me/consultations` · ground: `GET /app/ground/astronauts/{id}/reflection` · `GET /app/ground/consultations` |
| Offline: local copy, queue upload, changes | `GET /app/me/offline-bundle` · `POST /app/me/sync` · `GET /app/me/changes?since=` · `GET /app/me/sync/ops` |
| Whole local database in the frontend format (mock-db.json shape) | `GET /app/snapshot` |
| Uploads (voice note, injury photo) | `POST /app/me/uploads` · `GET /app/uploads/{name}` |
| Meal plan, food catalog | `GET /app/me/meal-plan?day=` · `GET /app/foods?q=` |
| Avatar, shop | `GET`/`PUT /app/me/avatar` · `POST /app/me/avatar/purchase` · `POST /app/me/avatar/equip` · `GET /app/shop` |
| Leaderboard | `GET /app/leaderboard?by=logs\|coins` |
| Calendar, journal | `GET /app/me/calendar` · `GET /app/me/journal` |
| Mission | `GET /app/mission` |
| **Ground dashboard** | `GET /app/ground/crew` · `/alerts?minLevel=` · `/sleep` · `/environment` · `/astronauts/{id}/status\|summaries\|logs\|meal-plan\|telemetry\|sync` · `GET`/`POST /messages` · `POST /messages/{id}/read` |

All routes start with `/api/v1`.

### Saving a log

`POST /api/v1/app/me/logs` takes the `type` and a `data` object in the frontend format; validation errors only refer to that type. The server sets `missionDay`, `coinsEarned` (+10, meditation 0, plus the streak bonus), computed fields (`label`, `durationMin`, `doseUsv`, `cadence`, `deltaVsBaselinePct`, `flaggedToGround`), recomputes the alerts and, when allowed, writes an Orbi note.

```jsonc
{"type": "mood", "data": {"level": 4, "tags": ["calm"]}}
{"type": "sleep", "environmentId": "sleep", "data": {"bedtime": "2026-11-14T00:40:00Z", "wakeTime": "2026-11-14T06:50:00Z", "quality": 3, "wakeUps": "1-2", "source": "manual"}}
{"type": "radiation", "data": {"value": 41, "unit": "uSv", "source": "personal"}}
{"type": "measurement", "data": {"metric": "blood_pressure", "values": {"systolic": 118, "diastolic": 76, "pulse": 62}}}
{"type": "measurement", "data": {"metric": "bone_density", "values": {"value": 1.18}}}
{"type": "injury", "data": {"bodyPart": "knee", "side": "left", "kind": "strain", "severity": "severe", "pain": 4, "description": "…"}}
{"type": "exercise", "environmentId": "lab", "data": {"modality": "aerobic", "equipment": "treadmill", "durationMin": 45, "effort": 3}}
```

Response: `{"log": {…}, "coins": 1260, "coinsEarned": 10, "loggedToday": true, "alerts": [new or escalated alerts], "supportRequestId": null, "message": "Log saved to your mission record · +10 mission coins"}`. A **severe injury** opens an urgent request in the care team's support queue (`/api/v1/care/support`) and returns its id.

### Device telemetry and Orbi confirmations

Wearable + EVA telemetry → Orbi → the astronaut reviews and confirms; the medical team follows the trends. The phone reads the devices (Health Connect on Android, HealthKit on iPhone, Bluetooth Low Energy for other sensors) and sends the readings in batches; the suit sends EVA telemetry. **There is no hardware in the prototype:** `POST /app/me/telemetry/simulate` makes the same readings, labelled `SIMULATED_TELEMETRY`. Readings from real devices are `DEVICE_MEASURED`. Every crew member starts with 24 simulated hours, so the Health Overview has data.

```http
POST /api/v1/app/me/telemetry
{"source": "health_connect", "device": "wrist_band", "readings": [
  {"metric": "heart_rate", "measuredAt": "2026-11-15T13:30:00Z", "value": 64},
  {"metric": "spo2", "measuredAt": "2026-11-15T13:30:00Z", "value": 97},
  {"metric": "sleep_session", "measuredAt": "2026-11-15T06:30:00Z",
   "values": {"bedtime": "2026-11-15T00:55:00Z", "wakeTime": "2026-11-15T06:30:00Z", "avgHr": 55, "awakenings": 2}}]}
→ 201 {"accepted": 3, "duplicates": 0, "byMetric": {…}, "confirmations": [ …what Orbi will ask… ],
       "sync": {"lastSyncAt": "…", "devices": [ … ], "pendingConfirmations": 1}, "provenance": "DEVICE_MEASURED"}
```

- Metrics (`GET /app/telemetry/metrics`): `heart_rate`, `resting_heart_rate`, `hrv`, `spo2`, `body_temperature`, `respiratory_rate`, `sleep_session`, `blood_pressure`, `radiation_dose`, and during an EVA `eva_heart_rate`, `eva_suit_pressure`, `eva_co2`, `eva_metabolic_rate`. Out-of-range values are rejected (422, with the index of each bad reading). Resending a batch is safe: same metric, device and time are skipped.
- `POST /app/me/telemetry/simulate {"hours": 24, "eva": false, "anomaly": "none"}`; `anomaly` is `low_spo2`, `fever`, `high_heart_rate`, `eva_co2` or `eva_pressure`, to demo a quick check.
- **Readings never become logs by themselves.** Orbi asks (`GET /app/me/confirmations`):
  - `kind: "log"`: a sleep session, blood pressure or dosimeter dose that is not logged today. `question` says what was measured; `proposed` is the log in the app format. `POST …/confirm` saves it as is, or with `{"correction": {"quality": 2}}` (+10 coins, the same as typing it, with the device's provenance). `POST …/dismiss` discards it.
  - `kind: "check"`: a value outside its usual range: SpO2 < 92%, temperature ≥ 37.8 °C, breathing > 24/min, resting heart rate ≥ 120, EVA heart rate ≥ 160, suit CO2 ≥ 8 mmHg, suit pressure < 27.5 kPa. One quick question, one per metric per day. `POST …/answer {"answer": "ok"}` or `{"answer": "not_ok", "symptoms": ["shortness_of_breath"]}`: "not OK" creates a `device_check` alert sent to the ground and a support request (urgent for SpO2 and EVA checks). Thresholds are illustrative.
- **Health Overview:** `GET /app/me/home` adds `sync` (last sync, minutes since, devices), `vitals` (latest heart rate, SpO2, temperature, breathing, HRV) and `pendingConfirmations`.
- **Ground:** `ground/crew` adds `lastSyncAt`, `pendingConfirmations` and `vitals`; `GET /app/ground/astronauts/{id}/telemetry?metric=&hours=` and `GET /app/ground/confirmations?astronautId=&status=`.

### Orbi reflection and consultation suggestion

`GET /app/me/orbi/reflection` reads the journal of the last 7 mission days (journal mode only; private chats are never stored) together with the same days' logs and device readings, area by area (sleep, mood and stress, pain or injury, food, exercise):

- `links[]`: `consistent` (the journal and the numbers both point to a problem), `journal_only` (written about, not in the numbers yet) or `numbers_only` (flagged by the numbers, not mentioned), with the journal days, the numbers and the evidence ids.
- `consultation`: whether Orbi suggests a medical consultation, with `reasons[]` and `priority`. **Decided by rules, not by the language model:** a journal entry about self-harm or urgent symptoms (urgent; the message says to talk to the flight surgeon or a crewmate now), a device check answered "not OK", an active high alert, two or more areas in alert, or the same problem in the journal and the numbers on 2+ days. Thresholds are illustrative.
- `narrative`: a short text. With `ORBI_AI_ENABLED=true` and `GROQ_API_KEY`, a language model words it from journal summaries and numbers (no names), and the answer says `aiGenerated: true` and `X-Data-Provenance` ends with `AI_GENERATED`; otherwise it is built from the rules. Answers with medical advice are dropped.

**Orbi tells the astronaut.** When a suggestion becomes due (after a log is saved or edited, a journal chat, a quick check answered "not OK", or an offline batch), Orbi writes it as a message in the astronaut's Orbi chat (journal mode, `source: "consult"`), which the app shows from the snapshot with no screen change. One message per set of reasons, not again within 3 mission days, none while a consultation is open, and none when the astronaut turned proactive messages off (`PUT /app/me/preferences {"proactiveMessages": false}`), except urgent ones. Replying "yes" in the chat (also through the offline queue) opens the consultation, and Orbi confirms with the request number. It is a chat message, not a phone notification.

The astronaut decides: `POST /app/me/consultations {"confirmed": true, "note": "..."}` opens a support request for the flight surgeon with Orbi's reasons (one open at a time; also as `consultation.request` in the offline queue, and with `Idempotency-Key`). The ground team sees it in `GET /app/ground/consultations`, in `consultation` of each `ground/crew` row and in `/api/v1/care/support`, and answers with `PATCH /api/v1/care/support/{id}`. `GET /app/me/home` has `consultationSuggestion` (rules only), and the offline bundle carries the reflection.

### Offline use and communication delay

Far from Earth the link has a delay (`commDelaySec` of the mission) and drops. The app has to keep working with no server, and the ground team has to know how old the data on its screen is. The server side of that:

**1. Local copy.** `GET /app/me/offline-bundle` returns, in one answer (about 70 KB), what the app needs with no link: profile and baseline, home, status, 7 daily summaries, the logs of the last 14 days, active alerts, pending confirmations, the thread with Mission Control, the meal plan of today and the next 2 days, the food catalog, and:

- `rules`: the rules of this API as data (score weights, status levels, coins, mission goals, and `deviceChecks` with `metric`, `op`, `threshold`, `urgent`, `question`). The app can run the same quick checks on a reading taken offline.
- `orbi`: Orbi's rule-based replies per topic, built with today's data, with their keywords. For `crisis` and `urgent`, `offlineNote` says that the ground team will only be reached when the link is back and to tell a crewmate now.
- `cursor`: pass it to `/sync` or `/changes` to get only what changed later.

**2. Queue.** While offline, the app saves each action in a local queue with an id it makes itself (`opId`, a UUID) and the time on the device (`recordedAt`). When the link is back it sends the queue:

```jsonc
// POST /api/v1/app/me/sync
{"deviceId": "phone-1", "clientTime": "2026-11-15T18:00:00Z", "since": "<cursor>", "queuedRemaining": 0, "ops": [
  {"opId": "9f0c…", "type": "log.create", "recordedAt": "2026-11-14T21:10:00Z", "missionDay": 11,
   "payload": {"type": "mood", "data": {"level": 2, "tags": ["tired"]}}},
  {"opId": "41ab…", "type": "log.update", "targetId": "op:9f0c…", "payload": {"type": "mood", "data": {"level": 3}}},
  {"opId": "77de…", "type": "telemetry", "payload": {"source": "ble", "device": "pulse_oximeter",
   "readings": [{"metric": "spo2", "measuredAt": "2026-11-15T09:00:00Z", "value": 89}]}},
  {"opId": "c3d4…", "type": "confirmation.answer", "targetId": "op:77de…",
   "payload": {"metric": "spo2", "answer": "not_ok", "symptoms": ["shortness_of_breath"]}},
  {"opId": "e5f6…", "type": "message.send", "payload": {"text": "Felt off yesterday.", "attachments": ["op:9f0c…"]}}
]}
// → {"serverTime": "…", "clockSkewSec": 1, "summary": {"received": 5, "applied": 5, "duplicates": 0, "rejected": 0, "conflicts": 0},
//    "results": [{"opId": "9f0c…", "status": "applied", "duplicate": false, "entityId": "log-1a2b3c4d", "lagSec": 75000, "result": { … }}, … ],
//    "changes": {"cursor": "…", "logs": [ … ], "alerts": [ … ], "activeAlertIds": [ … ], "confirmations": [ … ],
//                "pendingConfirmationIds": [ … ], "groundMessages": [ … ], "home": { … }}}
```

- Types: `log.create`, `log.update`, `telemetry`, `confirmation.confirm`, `confirmation.answer`, `confirmation.dismiss`, `message.send`, `message.read`, and `chat.save` (a journal-mode exchange with Orbi answered on the device: `{text, reply, summary?, moodLevel?}`; saves both messages and the journal entry). `payload` is the body of the matching online route, and the same rules run (coins, alerts, Orbi notes, support requests). Up to 500 operations per batch, applied in the order sent.
- **Each `opId` is applied at most once.** If the answer is lost, send the same batch again: operations already received come back with `duplicate: true` and their first result. Nothing is saved twice and coins are not given twice.
- `status` of each result: `applied`; `rejected` (the server refused it, `error` says why: take it out of the queue and show it); `conflict` (`log.update` with `baseUpdatedAt` older than the server copy, which comes in `result`; the app decides what to keep and sends a new operation). A 5xx or a network error means nothing is known: send again.
- `targetId: "op:<opId>"` points to something created earlier in the queue, which has no server id yet: a log (to edit it or attach it to a message), or a telemetry operation (to answer the question the app asked on the device; `payload.metric` picks the confirmation).
- `missionDay` puts a `log.create` on the day it was recorded (not after today). Alerts are evaluated again for today, and their 7-day window includes the late log. `recordedAt` later than the server clock is brought back to now (`clockAdjustedOps`).
- `changes` in the answer is what changed after `since`, including what this batch created. `GET /app/me/changes?since=` returns the same with no upload. Ground messages only appear once they arrive (communication delay).
- Single requests can be retried safely too: `POST /app/me/logs` and `POST /app/me/ground-messages` accept the header `Idempotency-Key: <uuid>`.
- Online only: chat with Orbi through the server (use the bundled replies), uploads, shop purchases and meal-plan edits by the ground team.

**Snapshot for the CosmoMedic app.** The frontend keeps an in-memory database with the shape of its `src/mocks/mock-db.json`. `GET /app/snapshot` returns that same shape with live data (`mission`, `astronauts`, `logs`, `daily_summaries`, `environment_readings`, `ai_chat`, `ground_messages`, `shop`, `calendar`, plus `role` and `leaderboard`), so the app replaces the mock at sign-in and after each `/sync`, with no change to the screens. An astronaut gets their own data and only the name and avatar of crewmates; the ground team gets the whole crew, without the chats with Orbi.

**3. Ground view.** Logs carry `timestamp` (when it happened) and `receivedAt` (when the server got it). `GET /app/ground/crew` adds `link` to each row: `status` (`online`: contact in the last 15 minutes; `delayed`: up to 12 hours; `offline`: longer; `unknown`: never), `lastContactAt`, `lastDeviceSyncAt`, `minutesSinceContact`, `queuedOnDevice` and `lastBatch` (how many operations and the largest delay). `GET /app/ground/astronauts/{id}/sync` lists the operations received, each with `lagSec`, the time between the astronaut doing it and the server receiving it.

What is not in the backend: the queue and the local storage themselves live in the app (for example SQLite or AsyncStorage in React Native). The current frontend calls the API directly and does not use these routes yet.

### Module names and log types

The home screen lists modules; logs are saved by type. Each home item carries its `logTypes`.

| Home module | Log `type` | Counts for `logsTotal` |
|---|---|---|
| `mood` | `mood` | yes |
| `sleep` | `sleep` | yes |
| `radiation` | `radiation` | yes |
| `meals` | `meal` | yes |
| `exercise` | `exercise` | yes |
| `body` | `measurement` (and `injury`) | yes (`measurement`) |
| `relax` | `meditation` | no ("coming soon") |

### Coins

+10 per saved log (meditation 0; edits give nothing). **Streak bonus:** the first mood check-in of the day that completes 7 days in a row (also 14, 21…) gives +50 more, so `coinsEarned` is 60 and the response has `bonus: {"type": "mood_streak_7", "coins": 50, "streak": 7}`.

### Chat with Orbi

```http
POST /api/v1/app/me/orbi/chat
{"text": "I slept badly again", "mode": "journal"}        // mode: journal | private
→ {"reply": "Your last nights averaged 5.4 h (goal 7 h). …", "mode": "journal", "topic": "sleep", "saved": true,
   "journalEntry": {"id": "jrn-…", "summary": "I slept badly again", "moodLevel": 2, "sourceMessageIds": ["msg-…", "msg-…"]},
   "messages": [ …the two saved messages… ], "provenance": "RULE_DERIVED"}
```

- `journal` saves the message, the reply and a journal entry (also listed in `GET /app/me/journal` and in the mood overview). `private` saves **nothing**: `journalEntry` is `null` and the history does not change.
- `GET /app/me/orbi/messages` returns the history, oldest first: `{id, astronautId, author: orbi | astronaut, text, timestamp, mode}`.
- **Replies are rule-based** (keywords plus the day's summary: sleep, exercise, nutrition), labelled `RULE_DERIVED`. There is no language model in this service; one can replace `app/cosmo/orbi.py` behind the same route, and must label its output `AI_GENERATED`. Messages about self-harm or urgent symptoms are answered by pointing to the flight surgeon and Mission Control.

### Uploads: voice note and injury photo

```http
POST /api/v1/app/me/uploads        (multipart, field "file")
→ 201 {"url": "https://…/api/v1/app/uploads/3f2a….m4a", "path": "/api/v1/app/uploads/3f2a….m4a",
       "contentType": "audio/mp4", "sizeBytes": 48211, "kind": "audio"}
```

Use the `url` in `voiceNote` of a mood log, `{"type": "mood", "data": {"level": 3, "voiceNote": {"url": "…", "durationSec": 12.5, "transcript": "…"}}}`, or in `photoUrl` of an injury. Audio (m4a, aac, mp3, wav, webm, ogg, 3gp) and images (jpg, png, webp, heic), up to 10 MB (`UPLOAD_MAX_MB`), stored in `UPLOAD_DIR`. Files get a random name and `GET /app/uploads/{name}` needs no token, so **whoever has the url can open the file**; use signed urls or private storage outside a demo.

### Meal plan

The medical team sets the menu of each mission day, with its nutrition. The astronaut only marks each planned meal.

- `GET /app/me/meal-plan?day=12`: `{missionDay, meals: [{id, slot, time, name, items, kcal, proteinG, carbsG, fatG, logged}], totals, eatenKcal, eatenPct, loggedMeals}`. `logged` is `null` or `{logId, adherence, eatenPct, kcal}`.
- `GET /app/foods?q=rice`: station catalog for swaps, `{id, name, servingG, kcal, proteinG, carbsG, fatG}` per serving.
- Ground: `PUT /app/ground/astronauts/{id}/meal-plan/{day}` replaces one day with `{"meals": [...]}`; `POST /app/ground/astronauts/{id}/meal-plan` takes `{"days": [{"day": 13, "meals": [...]}]}` and/or `{"seed": true, "fromDay": 1, "toDay": 30}` (default 3-day rotation); `GET` on the same path shows a day with the adherence. A meal is `{slot, time?, name, items, kcal, proteinG, carbsG, fatG}` or `{slot, foods: [{foodId, servings}]}` (nutrition computed). One meal per slot: `breakfast`, `lunch`, `dinner`, `snack`.
- Every astronaut starts with the default menu for the 30 days (about 2,500 kcal a day). The catalog and the menu are illustrative.

Logging a meal (`POST /app/me/logs`, `type: "meal"`). The API computes `kcal` and the macros; `name` and `tasteRating` are optional:

```jsonc
{"type": "meal", "data": {"planMealId": "pm-ast-01-d12-breakfast", "adherence": "followed"}}
{"type": "meal", "data": {"planMealId": "pm-ast-01-d12-lunch", "adherence": "partial", "eatenPct": 50, "tasteRating": 2}}
{"type": "meal", "data": {"planMealId": "pm-ast-01-d12-snack", "adherence": "swapped", "swaps": [{"foodId": "food-rice", "servings": 2}]}}
{"type": "meal", "data": {"planMealId": "pm-ast-01-d12-dinner", "adherence": "skipped"}}
{"type": "meal", "data": {"swaps": [{"foodId": "food-brownie", "servings": 1}]}}   // outside the plan
{"type": "meal", "data": {"name": "Crew pizza night", "kcal": 700}}                 // outside the plan, old format
```

- `followed` = 100% of the planned meal, `partial` = `eatenPct`, `swapped` = the sum of the swaps, `skipped` = 0. A planned meal is logged once (409 after that; edit it with `PUT /app/me/logs/{id}`).
- The nutrition summary reports the share of the plan eaten: `planPct` and `planText` ("82% of plan") in the meals overview, in the home detail of `meals` and as `nutritionPlanPct` in `ground/crew`.

### How the summaries and alerts are computed

Rules look at the last 7 mission days (thresholds in `app/cosmo/logic.py`):

| Domain | Warn | Alert | Body area |
|---|---|---|---|
| Mood | average of the last 3 check-ins ≤ 2.6, or stress tags on 3 days | ≤ 1.7 | head |
| Sleep | last 3 nights < 6 h on average, or 3 short nights | < 5 h | head |
| Blood pressure | ≥ 130/85 | ≥ 140/90 | torso |
| Muscle (strength or mass vs pre-flight baseline) | ≤ −5% | ≤ −10% | left arm |
| Bone density | ≤ −1.0% | ≤ −1.5% | legs |
| Fitness (aerobic vs baseline; days with ≥ 60 min) | ≤ −8%; < 55% of days | ≤ −10%; < 30% | legs |
| Radiation* (today vs own average) | > 1.5× | > 2× | right arm |
| Nutrition (intake vs goal; menu fatigue) | < 65% | < 50% | torso |
| Injury | moderate or active | severe | the injured area |

- **Score** = 80% health (100 minus 5 per warn and 10 per alert) + 20% completeness (logs done today out of 6).
- **Status tag:** `statusLevel` is `ok` (score ≥ 70), `warn` (50–69) or `alert` (< 50), in home, status, summaries and `ground/crew`. `statusLabel` follows it (`stable`, `attention`, `alert`).
- **Daily logs:** `logsTotal` is 6: mood, sleep, radiation, meals, exercise and body. Relax/meditation ("coming soon") and injuries do not count.
- **Alerts** follow the frontend types (`poor_sleep`, `low_mood`, `fitness_drop`, `bone_loss`, `menu_fatigue`, `injury`); `medium` and `high` are sent to the ground. Alerts from the mock are kept unless the rules escalate them; rule alerts that stop firing are resolved.
- **Orbi notes:** at most one every 7 mission days, only for `medium`/`high` alerts, always with the opt-out line; `PUT /app/me/preferences {"proactiveMessages": false}` turns them off. A `high` alert also opens a routine support request.
- **Messages:** each message gets `estimatedArrivalAt = sentAt + commDelaySec`. Status: `in_transit` → `delivered` → `read`. Ground replies only appear in the astronaut's thread once they arrive.
- **Radiation is illustrative** (`illustrative: true`): NASA sets no fixed daily limit for long missions.

With the demo data on day 12: Ana 74 `ok` (poor sleep alert), Raj 97 `ok`, Mika 67 `warn` (low mood, menu fatigue), Leo 40 `alert` (poor sleep, low mood, fitness, knee injury).

Provenance: logs and messages from the mock are `SIMULATED_TELEMETRY`, those sent from the app `USER_REPORTED`; scores, areas, rule alerts and Orbi notes are `RULE_DERIVED`; device readings, and logs confirmed from them, are `DEVICE_MEASURED` (or `SIMULATED_TELEMETRY` when the simulator made them).

### Data provenance

Every item has a `provenance` and every response an `X-Data-Provenance` header; `GET /api/v1/provenance` lists the types, sources and routes.

| `provenance` | Meaning |
|---|---|
| `REAL_RESEARCH_DATA` | measured in published studies (OSDR Inspiration4, AGBRESA) |
| `SIMULATED_TELEMETRY` | generated by this project: synthetic mission, CosmoMedic demo data, device simulator |
| `USER_REPORTED` | typed in by the astronaut |
| `RULE_DERIVED` | computed by documented rules, no AI |
| `REFERENCE_DOCUMENT` | text from the PDF knowledge base |
| `AI_GENERATED` | text written by a language model: only `/api/v1/orbi` (insights and consultation briefings), when `ORBI_AI_ENABLED=true` and `GROQ_API_KEY` is set; each answer says so in `aiGenerated` and `messageSource` |
| `DEVICE_MEASURED` | sent by a paired wearable, cuff, dosimeter or EVA suit |

### Replacing the mock in the frontend

```ts
const API = "http://192.168.0.10:8000/api/v1/app";

export async function crewLogin(crewId: string, pin: string) {
  const res = await fetch(`${API}/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ crewId, pin }),
  });
  if (!res.ok) throw new Error("Invalid Crew ID or PIN");
  return res.json(); // { accessToken, astronaut, next }
}

const get = (path: string, token: string) =>
  fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token}` } }).then(r => r.json());

// const { accessToken, refreshToken } = await crewLogin("ORB3-ANA", "2026");
// const home = await get("/me/home", accessToken);
// const sleep = await get("/me/logs/sleep/overview", accessToken);
// await fetch(`${API}/me/logs`, { method: "POST", headers: {...}, body: JSON.stringify({ type: "mood", data: { level: 4, tags: [] } }) });
```

## Crew monitoring

### Authentication

Astronauts and the care team log in and send the token as `Authorization: Bearer <token>`.

```http
POST /api/v1/auth/login
{ "username": "astronaut2", "password": "spaceapps2026" }
→ { "accessToken": "...", "tokenType": "bearer", "expiresIn": 1800, "refreshToken": "...", "refreshExpiresIn": 2592000, "user": { "role": "astronaut", "astronautId": "A002", ... } }
```

Access tokens expire after 30 minutes. Use `POST /api/v1/auth/refresh` with `{ "refreshToken": "..." }` to rotate the refresh token and receive a new access token. Refresh sessions have a 30-day absolute lifetime; reusing a rotated token revokes that session family. `POST /api/v1/auth/logout` with the current refresh token revokes the session, but any access token already issued remains valid until its 30-minute expiry. Store refresh tokens securely and configure the same stable `JWT_SECRET` across app restarts and workers.

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

Passwords are stored as PBKDF2-SHA256 hashes; access tokens are HS256 JWTs signed with `JWT_SECRET` (set it to a long random value; without it, a random one is used and access tokens stop working after a restart). **Change the demo password or turn the demo accounts off outside a demo.**

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
| GET | `/api/v1/missions/{id}/astronauts/{aid}/today?day=` | app home: one day vs baseline (`metrics` list and the same entries keyed by metric in `values`), one-line `summary`, latest bone/WBC values, active episodes |
| GET | `/api/v1/missions/{id}/astronauts/{aid}/daily?fromDay=&toDay=` | daily records, each with a one-line `summary` of the vitals |
| GET | `/api/v1/missions/{id}/astronauts/{aid}/baseline` | 30-day pre-flight baseline: object with `summary`, `values` (mean per metric) and `metrics` (n, mean, SD per metric) |
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
  "missionDay": 49, "status": "out_of_range",
  "summary": "BP 111/71 mmHg · HR 71 bpm · glucose 88 mg/dL · 38.4 °C · sleep 5.8 h · mood 3/5",
  "date": "2027-04-19", "phase": "inflight", "astronautId": "A002", "missionId": "SYN-LDM-01",
  "values": { "systolic_bp": 111, "heart_rate": 71, "body_temperature": 38.43, "sleep_hours": 5.8, "mood": 3, "white_blood_cells": 11.3, "...": "..." },
  "metricStatus": { "body_temperature": "out_of_range", "white_blood_cells": "out_of_range", "protein_intake": "out_of_range", "sleep_hours": "watch", "...": "..." }
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

app_missions ─< app_astronauts ─< app_logs, app_alerts, app_ground_messages, app_journal, app_calendar,
                                  app_chat_messages, app_meal_plan
app_shop_items   app_environment_readings   app_foods   (CosmoMedic)
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

## Deploy (Azure App Service)

Live demo: **https://crew-health-api-univesp.azurewebsites.net** (`/docs` for the interactive docs).

`deploy/azure.sh` creates a new App Service (Linux, Python 3.12) and deploys the API to it:

```bash
az login
./deploy/azure.sh crew-health-api-univesp mexicocentral B1   # app name, region, SKU
```

- It creates the resource group `rg-<app>`, the plan `plan-<app>` and the web app. It then sets the app settings, the startup command, Always On and the `/health` check, and deploys a zip of `app/` and `requirements.txt` (App Service installs the requirements).
- SQLite lives in `/home/data`, which App Service keeps across restarts. Use one worker, so there is a single scheduler and SQLite stays safe. Data is loaded from the bundled files on first start (`INGEST_SOURCE=bundled`).
- `JWT_SECRET` and `ADMIN_API_KEY` are generated at random and stored only in the app settings.
- Azure for Students subscriptions only allow some regions (here: `austriaeast`, `canadacentral`, `belgiumcentral`, `northcentralus`, `mexicocentral`). Check yours with `az policy assignment list`.
- B1 costs about US$13 a month. To stop the charges, run `az group delete -n rg-<app>`; `az webapp stop` is not enough, because the plan is still billed.

The `Dockerfile` reads the port from `PORT` (default 8000), so the same image also runs on container hosts.

### Continuous deploy (GitHub Actions)

`.github/workflows/deploy.yml` runs on every push to `main` (a merged pull request) and can also be started by hand (Actions > deploy > Run workflow). It runs the tests, deploys the zip of `app/` and `requirements.txt` to the App Service, waits for `/health` and checks that the new routes are in `/openapi.json`. If the tests fail, nothing is deployed. Pull requests only run the tests (`tests.yml`).

Setup, once:

1. Azure portal > the App Service > Configuration > General settings > **SCM Basic Auth Publishing Credentials: On** > Save. The publish profile needs it. (CLI: `az resource update -g rg-<app> --namespace Microsoft.Web --resource-type basicPublishingCredentialsPolicies --parent sites/<app> --name scm --set properties.allow=true`.)
2. Overview > **Download publish profile**.
3. GitHub > the repository > Settings > Secrets and variables > Actions > New repository secret: name `AZURE_WEBAPP_PUBLISH_PROFILE`, value the whole content of the downloaded file.

The publish profile is a deploy password for this one app: keep it only in the secret. To replace it, use "Reset publish profile" in the portal and update the secret. Do not change app settings while a deploy is running: the restart interrupts it.

To deploy by hand instead: zip `app` and `requirements.txt`, then `az webapp deploy -g rg-<app> -n <app> --src-path app.zip --type zip`.

## Configuration

See `.env.example`. Main variables: `DATABASE_URL`, `JWT_SECRET`, `SEED_DEMO_USERS`, `DEMO_PASSWORD`, `CURRENT_MISSION_DAY`, `SCHEDULER_ENABLED`, `DETECTION_INTERVAL_MINUTES`, `PROACTIVE_MIN_INTERVAL_DAYS`, `SUPPORT_WEBHOOK_URL`, `INGEST_SOURCE`, `CORS_ORIGINS`, `ADMIN_API_KEY`, `SYNTHETIC_SEED`, `APP_SEED_ON_STARTUP`, `APP_MISSION_DAY`, `APP_DEMO_PIN`, `APP_TEST_PIN`, `APP_GROUND_PIN`, `UPLOAD_DIR`, `UPLOAD_MAX_MB`.

## Limitations

- **Pattern rules and messages are illustrative.** Thresholds are generic screening values and the message texts were not reviewed by a clinician or psychologist. They show how the system works; they are not medical criteria.
- **The scheduler runs inside the API process.** With several workers, each runs it; use one worker or move the cycle to a separate job for production.
- **AI agent only in `/api/v1/orbi`.** Insights and consultation briefings call a language model (CrewAI with Groq) when `ORBI_AI_ENABLED=true` and `GROQ_API_KEY` is set, and then send selected (simulated) health measurements to that provider; without them the text is statistical (`messageSource: statistical_fallback`). The model's text is not clinically validated. See `app/ORBI_STEP2_README.md`. The rest of the service, including the CosmoMedic chat, is rule-based.
- **AGBRESA is an analog.** Bed rest reproduces some effects of microgravity (fluid shift, deconditioning) in healthy volunteers on Earth; it is not spaceflight, and these are not the Inspiration4 crew. Do not join it with the other data sets as if it described the same people.
- **Synthetic mission.** The daily vitals are generated, not measured. Effects follow published orders of magnitude for long ISS stays, but the individual curves, events and thresholds are illustrative. Use the data to build and evaluate the agent and the apps, not to draw physiological conclusions.
- **Four people, seven timepoints.** The baseline uses three samples per person; z-scores are indicative, not statistical tests.
- **Multiplex panels.** Research-assay concentrations with no clinical reference range; the `relativePercent` column is passed through as reported. These analytes only ever get `watch` or `normal`, and vary a lot between pre-flight samples (e.g. CRP). With three baseline samples and hundreds of proteins, some `watch` values in the research panels are expected by chance.
- **Device telemetry is simulated** in the prototype, and its quick-check thresholds are illustrative screening values, not medical criteria.
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
  cosmo/             CosmoMedic app layer: tables, schemas, rules (score, areas, alerts), Orbi chat rules, meal plan, device telemetry and confirmations, seed
  synthetic/         metric definitions, generator, check-in catalog and history, assessment; `python -m app.synthetic`
  services/          ingestion, analytics, synthetic store, patterns, proactive messages, support, monitoring cycle, PDF search, users
  routers/           auth, me, care, crew, dashboard, missions, analog, knowledge, catalog, ingest, provenance, app_public, app_me, app_ground
  seed/              bundled copies of the five OSDR files, the two AGBRESA files and the CosmoMedic data (cosmomedic/)
samples/             synthetic_daily_vitals.csv and its ground-truth events
tests/               parsers, download clients (mocked), API end-to-end, synthetic data, AGBRESA, monitoring, access control, provenance, CosmoMedic app, device telemetry, OSD-575 immune panels
```
