"""Ingest the AGBRESA supplementary files (PLOS ONE) into the database."""

from __future__ import annotations

import hashlib
from collections import defaultdict

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.analog import agbresa
from app.config import SEED_DIR, Settings
from app.models import AnalogObservation, AnalogSubject, SourceFile, Study, utcnow

SEED = SEED_DIR / "agbresa"


class AnalogFetchError(RuntimeError):
    pass


def download(spec: agbresa.SourceSpec, timeout: float, transport: httpx.BaseTransport | None = None) -> bytes:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, transport=transport) as client:
            response = client.get(spec.url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AnalogFetchError(f"Download failed for {spec.file_name}: {exc}") from exc
    if not response.content.startswith(b"PK"):  # xlsx files are zip archives
        raise AnalogFetchError(f"PLOS did not return an xlsx file for {spec.file_name}")
    return response.content


def fetch(spec: agbresa.SourceSpec, source: str, settings: Settings, warnings: list[str],
          transport: httpx.BaseTransport | None = None) -> tuple[bytes, str, str]:
    if source in ("remote", "auto"):
        try:
            content = download(spec, settings.osdr_timeout_seconds, transport)
            settings.raw_cache_dir.mkdir(parents=True, exist_ok=True)
            (settings.raw_cache_dir / spec.file_name).write_bytes(content)
            return content, "remote", spec.url
        except AnalogFetchError as exc:
            if source == "remote":
                raise
            warnings.append(f"{spec.file_name}: PLOS unreachable ({exc}); used the bundled copy")
    path = SEED / spec.file_name
    return path.read_bytes(), "bundled", str(path.relative_to(SEED_DIR.parent.parent))


def _ensure_study(session: Session) -> None:
    study = session.get(Study, agbresa.STUDY_ID) or Study(id=agbresa.STUDY_ID)
    study.title = agbresa.STUDY_TITLE
    study.mission = agbresa.STUDY_CONTEXT
    study.url = agbresa.STUDY_URL
    session.add(study)
    session.flush()


def ingest_agbresa(
    session: Session,
    settings: Settings,
    source: str,
    force: bool,
    warnings: list[str],
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    _ensure_study(session)
    results = []
    changed = False
    for spec in agbresa.SOURCES:
        content, origin, url = fetch(spec, source, settings, warnings, transport)
        sha = hashlib.sha256(content).hexdigest()
        existing = session.scalar(select(SourceFile).where(SourceFile.dataset_key == spec.key))
        if existing is not None and existing.sha256 == sha and not force:
            results.append({"dataset": spec.key, "file": spec.file_name, "origin": origin, "sha256": sha,
                            "skipped": True, "measurements": existing.measurement_count})
            continue

        parsed = agbresa.PARSERS[spec.key](content)
        warnings.extend(parsed.warnings)
        source_file = existing or SourceFile(dataset_key=spec.key)
        if existing is not None:
            session.execute(delete(AnalogObservation).where(AnalogObservation.source_file_id == existing.id))
        source_file.study_id = agbresa.STUDY_ID
        source_file.file_name = spec.file_name
        source_file.url = url
        source_file.origin = origin
        source_file.sha256 = sha
        source_file.size_bytes = len(content)
        source_file.row_count = parsed.rows
        source_file.measurement_count = len(parsed.observations)
        source_file.ingested_at = utcnow()
        session.add(source_file)
        session.flush()

        for ps in parsed.subjects.values():
            subject = session.get(AnalogSubject, ps.id) or AnalogSubject(id=ps.id, study_id=agbresa.STUDY_ID, code=ps.code)
            subject.group, subject.sex = ps.group, ps.sex
            subject.age_group = ps.age_group if ps.age_group is not None else subject.age_group
            subject.campaign = ps.campaign or subject.campaign
            session.add(subject)
        session.flush()

        for o in parsed.observations:
            metric = agbresa.METRICS_BY_CODE[o.metric]
            session.add(
                AnalogObservation(
                    subject_id=f"AGB-{o.subject_code}", source_file_id=source_file.id, session=o.session,
                    session_index=o.session_index, bed_rest_day=o.bed_rest_day, month=o.month,
                    metric=o.metric, moment=o.moment, raw_value=o.raw_value, value=o.value,
                    quality=o.quality, quality_note=o.quality_note,
                    status=agbresa.status_for(metric, o.moment, o.value, o.quality),
                )
            )
        changed = True
        results.append({"dataset": spec.key, "file": spec.file_name, "origin": origin, "sha256": sha,
                        "skipped": False, "samples": parsed.rows, "measurements": len(parsed.observations)})
    session.flush()
    if changed:
        recompute_changes(session)
    return results


def recompute_changes(session: Session) -> None:
    """Change of each value versus the same subject, metric and moment on the first session."""
    groups = defaultdict(list)
    for o in session.scalars(select(AnalogObservation)):
        groups[(o.subject_id, o.metric, o.moment)].append(o)
    for items in groups.values():
        items.sort(key=lambda o: o.bed_rest_day)
        first = next((o.value for o in items if o.value is not None), None)
        for o in items:
            if o.value is None or first is None:
                o.delta_vs_first = o.pct_vs_first = None
            else:
                o.delta_vs_first = o.value - first
                o.pct_vs_first = (o.value / first - 1) * 100 if first else None
