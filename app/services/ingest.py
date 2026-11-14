"""Download (or load), parse and store the OSDR datasets.

Ingestion is idempotent: each dataset owns one SourceFile row, and re-ingesting
replaces that file's samples and measurements in one transaction. Files whose
SHA-256 has not changed are skipped unless ``force`` is set.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import SEED_DIR, Settings
from app.models import (
    Analyte,
    CrewMember,
    IngestRun,
    Measurement,
    Sample,
    SourceFile,
    Study,
    Timepoint,
    utcnow,
)
from app.osdr.analytes import MULTIPLEX_NOTE, describe
from app.osdr.client import OSDRClient, OSDRError
from app.osdr.datasets import DATASETS, MISSION, STUDIES, DatasetSpec
from app.osdr.parser import parse_csv, parse_timepoint
from app.services.analytics import recompute_derived

log = logging.getLogger(__name__)

SOURCES = {"auto", "remote", "bundled"}


@dataclass
class FetchedFile:
    content: bytes
    origin: str  # "remote" | "bundled"
    url: str


def fetch_dataset(
    spec: DatasetSpec,
    source: str,
    settings: Settings,
    client: OSDRClient | None,
    warnings: list[str],
) -> FetchedFile:
    if source in ("remote", "auto") and client is not None:
        try:
            content, url = client.download(spec)
            settings.raw_cache_dir.mkdir(parents=True, exist_ok=True)
            (settings.raw_cache_dir / spec.file_name).write_bytes(content)
            return FetchedFile(content, "remote", url)
        except OSDRError as exc:
            if source == "remote":
                raise
            warnings.append(f"{spec.file_name}: OSDR unreachable ({exc}); used the bundled copy")
    path = SEED_DIR / spec.file_name
    return FetchedFile(path.read_bytes(), "bundled", str(path.relative_to(SEED_DIR.parent.parent)))


def _timepoint_label(phase: str, offset: int) -> str:
    days = abs(offset)
    unit = "day" if days == 1 else "days"
    if phase == "preflight":
        return f"{days} {unit} before launch"
    if phase == "postflight":
        return f"{days} {unit} after return"
    return f"Flight day {days}"


def _ensure_reference_rows(session: Session) -> None:
    for spec in STUDIES:
        study = session.get(Study, spec.id) or Study(id=spec.id)
        study.title, study.mission, study.url = spec.title, MISSION, spec.url
        session.add(study)


def _ensure_crew(session: Session, crew_id: str) -> None:
    if session.get(CrewMember, crew_id) is None:
        number = int(crew_id[1:])
        session.add(CrewMember(id=crew_id, display_name=f"Crew Member {number}", mission=MISSION))


def _ensure_timepoint(session: Session, code: str, settings: Settings) -> None:
    if session.get(Timepoint, code) is not None:
        return
    tp = parse_timepoint(code)
    if tp.phase == "preflight":
        mission_day = tp.offset_days
    elif tp.phase == "postflight":
        mission_day = settings.mission_duration_days + tp.offset_days
    else:
        mission_day = max(tp.offset_days - 1, 0)
    session.add(
        Timepoint(
            code=code,
            phase=tp.phase,
            offset_days=tp.offset_days,
            mission_day=mission_day,
            label=_timepoint_label(tp.phase, tp.offset_days),
        )
    )


def _ensure_analyte(
    session: Session, spec: DatasetSpec, code: str, unit_slug: str | None, has_range: bool
) -> None:
    info, unit = describe(code, unit_slug)
    analyte = session.get(Analyte, code) or Analyte(code=code)
    analyte.panel = spec.panel
    analyte.panel_name = spec.panel_name
    analyte.display_name = info.name
    analyte.category = info.category
    analyte.unit = unit
    analyte.unit_raw = unit_slug
    analyte.description = info.description
    notes = [n for n in (info.unit_note, None if has_range else MULTIPLEX_NOTE) if n]
    analyte.unit_note = " ".join(notes) or None
    analyte.has_reference_range = has_range
    session.add(analyte)


def ingest_dataset(
    session: Session,
    spec: DatasetSpec,
    fetched: FetchedFile,
    settings: Settings,
    force: bool,
    warnings: list[str],
) -> dict:
    sha = hashlib.sha256(fetched.content).hexdigest()
    existing = session.scalar(select(SourceFile).where(SourceFile.dataset_key == spec.key))
    if existing is not None and existing.sha256 == sha and not force:
        return {
            "dataset": spec.key,
            "file": spec.file_name,
            "origin": fetched.origin,
            "sha256": sha,
            "skipped": True,
            "measurements": existing.measurement_count,
        }

    parsed = parse_csv(fetched.content.decode("utf-8-sig"), spec.layout)
    warnings.extend(f"{spec.file_name}: {w}" for w in parsed.warnings)

    source_file = existing or SourceFile(dataset_key=spec.key)
    if existing is not None:
        session.execute(delete(Measurement).where(Measurement.sample_id.in_(
            select(Sample.id).where(Sample.source_file_id == existing.id)
        )))
        session.execute(delete(Sample).where(Sample.source_file_id == existing.id))

    source_file.study_id = spec.study.id
    source_file.file_name = spec.file_name
    source_file.url = fetched.url
    source_file.origin = fetched.origin
    source_file.sha256 = sha
    source_file.size_bytes = len(fetched.content)
    source_file.row_count = len(parsed.samples)
    source_file.measurement_count = len(parsed.measurements)
    source_file.ingested_at = utcnow()
    session.add(source_file)
    session.flush()

    has_range = {
        code: any(
            m.ref_min is not None or m.ref_max is not None
            for m in parsed.measurements
            if m.analyte_code == code
        )
        for code in parsed.analytes
    }
    for code, unit_slug in parsed.analytes.items():
        _ensure_analyte(session, spec, code, unit_slug, has_range[code])

    sample_ids: dict[str, int] = {}
    for key in parsed.samples:
        _ensure_crew(session, key.crew_id)
        _ensure_timepoint(session, key.timepoint, settings)
        session.flush()
        sample = Sample(
            sample_name=key.sample_name,
            crew_id=key.crew_id,
            timepoint_code=key.timepoint,
            specimen=key.specimen,
            panel=spec.panel,
            source_file_id=source_file.id,
        )
        session.add(sample)
        session.flush()
        sample_ids[key.sample_name] = sample.id

    session.add_all(
        Measurement(
            sample_id=sample_ids[m.sample.sample_name],
            crew_id=m.sample.crew_id,
            timepoint_code=m.sample.timepoint,
            analyte_code=m.analyte_code,
            value=m.value,
            ref_min=m.ref_min,
            ref_max=m.ref_max,
            relative_percent=m.relative_percent,
            flag="missing",
        )
        for m in parsed.measurements
    )
    return {
        "dataset": spec.key,
        "file": spec.file_name,
        "origin": fetched.origin,
        "sha256": sha,
        "skipped": False,
        "samples": len(parsed.samples),
        "measurements": len(parsed.measurements),
    }


def run_ingestion(
    session: Session,
    settings: Settings,
    source: str | None = None,
    force: bool = False,
    client: OSDRClient | None = None,
) -> IngestRun:
    source = source or settings.ingest_source
    if source not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}")

    run = IngestRun(requested_source=source, status="running", files=[], warnings=[])
    session.add(run)
    session.commit()

    own_client = client is None and source != "bundled"
    if own_client:
        client = OSDRClient(settings.osdr_base_url, settings.osdr_timeout_seconds)
    warnings: list[str] = []
    files: list[dict] = []
    try:
        _ensure_reference_rows(session)
        for spec in DATASETS:
            fetched = fetch_dataset(spec, source, settings, client, warnings)
            files.append(ingest_dataset(session, spec, fetched, settings, force, warnings))
        session.flush()
        recompute_derived(session, settings)
        run.status = "success"
    except Exception as exc:  # keep the run record, roll back the data
        session.rollback()
        run = session.get(IngestRun, run.id)
        run.status = "failed"
        run.error = str(exc)
        log.exception("Ingestion failed")
    finally:
        if own_client and client is not None:
            client.close()
    run.files = files
    run.warnings = warnings
    run.finished_at = utcnow()
    session.add(run)
    session.commit()
    return run


def database_is_empty(session: Session) -> bool:
    return (session.scalar(select(func.count()).select_from(Measurement)) or 0) == 0
