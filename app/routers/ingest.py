"""Trigger and inspect OSDR ingestion runs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import IngestRun
from app.schemas import IngestRunOut
from app.services.ingest import run_ingestion

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


def require_admin(x_api_key: str | None = Header(None)) -> None:
    expected = get_settings().admin_api_key
    if expected and x_api_key != expected:
        raise HTTPException(401, "Missing or invalid X-API-Key")


@router.post("", response_model=IngestRunOut, dependencies=[Depends(require_admin)], summary="Pull data from OSDR")
def trigger_ingest(
    source: Literal["auto", "remote", "bundled"] = Query(
        "auto", description="auto: OSDR with fallback to the bundled copy"
    ),
    force: bool = Query(False, description="Re-ingest even if the file checksum is unchanged"),
    db: Session = Depends(get_db),
):
    run = run_ingestion(db, get_settings(), source=source, force=force)
    if run.status == "failed":
        raise HTTPException(502, detail={"message": "Ingestion failed", "error": run.error, "runId": run.id})
    return run


@router.get("/runs", response_model=list[IngestRunOut], summary="Recent ingestion runs")
def list_runs(limit: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    return db.scalars(select(IngestRun).order_by(IngestRun.id.desc()).limit(limit)).all()
