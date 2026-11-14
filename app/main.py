"""Crew Health API: NASA OSDR blood panels from the Inspiration4 crew, cleaned and served."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app import db
from app.config import get_settings
from app.routers import catalog, crew, dashboard, ingest
from app.services.ingest import database_is_empty, run_ingestion

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crew-health-api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    db.init_db()
    if settings.ingest_on_startup:
        with db.SessionLocal() as session:
            if database_is_empty(session):
                log.info("Empty database: ingesting OSDR data (source=%s)", settings.ingest_source)
                run = run_ingestion(session, settings)
                log.info("Ingestion %s; warnings: %s", run.status, run.warnings or "none")
    yield


app = FastAPI(
    title="Crew Health API",
    version="1.0.0",
    summary="Astronaut blood panels from NASA OSDR (OSD-569, OSD-575), normalized for apps and dashboards.",
    description=(
        "Complete blood count, comprehensive metabolic panel and a multiplex cardiovascular panel "
        "for the four SpaceX Inspiration4 crew members, collected from L-92 to R+194. Each value is "
        "checked against the lab reference range and against the crew member's own pre-flight "
        "baseline. **Not a medical device.**"
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
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

for router in (crew.router, dashboard.router, catalog.router, ingest.router):
    app.include_router(router)


@app.get("/health", tags=["meta"], summary="Liveness and data readiness")
def health():
    with db.SessionLocal() as session:
        empty = database_is_empty(session)
    return {"status": "ok", "dataReady": not empty}
