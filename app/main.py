"""Crew Health API: NASA OSDR blood panels from the Inspiration4 crew, cleaned and served."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app import db, provenance
from app.config import get_settings
from app.routers import analog, auth, care, catalog, crew, dashboard, ingest, knowledge, me, missions
from app.routers import app_ground, app_me, app_public, app_snapshot, app_sync
from app.routers import provenance as provenance_router
from app.services.ingest import database_is_empty, missing_datasets, run_ingestion
from app.cosmo.seed import ensure_extras as ensure_cosmo_extras
from app.cosmo.seed import seed_if_empty as seed_cosmo
from app.services.knowledge import load_bundled
from app.services.monitoring import backfill_if_empty, scheduler_loop
from app.services.synthetic_store import load_synthetic, synthetic_is_empty
from app.services.users import seed_demo_users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crew-health-api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    db.init_db()
    if settings.ingest_on_startup:
        with db.SessionLocal() as session:
            missing = missing_datasets(session)
            if database_is_empty(session) or missing:
                # Unchanged files are skipped by their SHA-256, so this only adds the new datasets.
                log.info("Ingesting OSDR data (source=%s, missing: %s)", settings.ingest_source, missing or "all")
                run = run_ingestion(session, settings)
                log.info("Ingestion %s; warnings: %s", run.status, run.warnings or "none")
    if settings.synthetic_on_startup:
        with db.SessionLocal() as session:
            if synthetic_is_empty(session):
                mission = load_synthetic(session, settings.synthetic_seed)
                log.info("Generated synthetic mission %s (seed=%s)", mission.id, mission.seed)
    with db.SessionLocal() as session:
        if settings.seed_demo_users:
            seed_demo_users(session, settings.demo_password)
        if settings.app_seed_on_startup:
            seed_cosmo(session, settings)
            ensure_cosmo_extras(session, settings)
        if load_bundled(session):
            log.info("Indexed the bundled PDF knowledge base")
        result = backfill_if_empty(session)
        if result:
            log.info("Replayed pattern detection over the mission history: %s", result)

    stop = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(stop)) if settings.scheduler_enabled else None
    yield
    if task:
        stop.set()
        await task


app = FastAPI(
    title="Crew Health API",
    version="1.0.0",
    summary=(
        "Astronaut blood panels from NASA OSDR (OSD-569, OSD-575) and a synthetic daily-vitals "
        "history for a 6-month mission, normalized for apps, dashboards and an AI agent."
    ),
    description=(
        "Complete blood count, comprehensive metabolic panel and a multiplex cardiovascular panel "
        "for the four SpaceX Inspiration4 crew members, collected from L-92 to R+194. Each value is "
        "checked against the lab reference range and against the crew member's own pre-flight "
        "baseline.\n\n"
        "`/api/v1/missions` serves a **synthetic** long-duration mission: 4 astronauts, a 30-day "
        "pre-flight baseline and 180 days in flight with daily blood pressure, heart rate, glucose, "
        "body temperature, sleep and mood, plus bone density, nutrition and white cells.\n\n"
        "`/api/v1/analog/agbresa` serves **real** heart rate, blood pressure, sleepiness, motion "
        "sickness and mood from the AGBRESA 60-day bed rest study (DLR/ESA/NASA), an Earth analog "
        "with healthy volunteers.\n\n"
        "Astronauts log in (`/api/v1/auth/login`) and see only their own data under `/api/v1/me`: "
        "daily check-ins (stress, fatigue, symptoms), patterns found in a rolling 7-day window, "
        "proactive messages (at most one every 7 days, can be turned off) and requests for human "
        "support. The care team works from `/api/v1/care`. `/api/v1/knowledge` searches PDF documents "
        "(NASA-STD-3001 Vol. 1 included).\n\n"
        "Every item and main object carries a `provenance` value (REAL_RESEARCH_DATA, SIMULATED_TELEMETRY, "
        "USER_REPORTED, RULE_DERIVED, REFERENCE_DOCUMENT; AI_GENERATED is reserved for the AI agent) and "
        "every response an `X-Data-Provenance` header; see `/api/v1/provenance`.\n\n"
        "`/api/v1/app` serves the **CosmoMedic** React Native app in its own data model (Crew ID + PIN login, "
        "logs, daily summaries, Orbi alerts and notes, ground messages with communication delay, avatar, shop, "
        "leaderboard, ground dashboard). **Not a medical device.**"
    ),
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials="*" not in settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Data-Provenance"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

for router in (
    auth.router, me.router, care.router, crew.router, dashboard.router, missions.router,
    analog.router, knowledge.router, catalog.router, ingest.router, provenance_router.router,
    app_public.router, app_me.router, app_sync.router, app_snapshot.router, app_ground.router,
):
    app.include_router(router)


@app.middleware("http")
async def provenance_header(request: Request, call_next):
    """Tell every client which kinds of data a route returns (see GET /api/v1/provenance)."""
    response = await call_next(request)
    values = provenance.for_path(request.url.path)
    if values:
        response.headers["X-Data-Provenance"] = ",".join(values)
    return response


@app.get("/health", tags=["meta"], summary="Liveness and data readiness")
def health():
    with db.SessionLocal() as session:
        empty = database_is_empty(session)
    return {"status": "ok", "dataReady": not empty}
