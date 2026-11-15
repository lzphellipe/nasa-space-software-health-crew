# Orbi — Step 2: AI-generated page briefings

This package contains **complete files**, not patches. It is based on the merged repository ZIP and the Step 1 Orbi implementation.

## Files to copy into the project

- `app/services/orbi_insights.py` — unchanged Step 1 statistical service, included for convenience.
- `app/services/orbi_narrative.py` — **new** CrewAI narrative service.
- `app/routers/orbi.py` — **updated** route that calls the narrative service.
- `app/main.py` — unchanged Step 1 application file, included for convenience.

The existing PDF chat agent (`app/agents/health_agent.py`) is **not modified**.

## Configuration

The narrative service uses the existing `GROQ_API_KEY` and `AGENT_MODEL` environment variables.
Optional: `ORBI_AI_ENABLED=false` to skip AI calls and use deterministic statistical text.

## Run

```powershell
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs`, authenticate with a demo astronaut account, and test:

`GET /api/v1/orbi/insights?page=sleep&missionDay=120`

For A004, sign in as `astronaut4` or use a medical account with `astronautId=A004`.

## Response fields

- `message`: AI-generated narrative or statistical fallback.
- `messageSource`: `ai_generated`, `ai_cached`, or `statistical_fallback`.
- `aiGenerated`: boolean.
- `metrics`: unmodified statistical evidence, including measurement freshness.
- `dataSource`: `SIMULATED_TELEMETRY`.

The same astronaut, mission day, page and evidence produce a cached message while this server process is running. A different mission day uses different evidence and prompt context, but distinct wording is **not guaranteed** if there are no meaningful changes. Cache is in memory and resets on restart.

**Limitations:** The LLM can still produce inaccurate language; it is not clinically validated. CrewAI/Groq requires connectivity. The fallback works without a model key. In this step, no raw chat or symptom notes are transmitted; selected health measurements are sent to the configured model provider when AI is enabled. Do not use with real sensitive health data without privacy and security review. Do not use this prototype for clinical decisions.
