
"""Retrieve scientific references for Orbi's contextual health guidance."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import KnowledgeChunk
from app.services.knowledge import INDEX, tokenize

logger = logging.getLogger(__name__)

PAGE_QUERIES = {
    "sleep": (
        "sleep circadian rhythm sleep deprivation fatigue "
        "lighting noise sleep environment crew",
    ),
    "mood": (
        "behavioral health psychological support stress "
        "mental health isolation crew",
    ),
    "body": (
        "crew medical monitoring physiological health "
        "blood pressure cardiovascular health",
    ),
    "meals": (
        "nutrition food intake energy requirements "
        "protein dietary crew health",
    ),
    "exercise": (
        "exercise countermeasures physical fitness "
        "muscle bone health crew",
    ),
    "radiation": (
        "space radiation exposure protection "
        "radiation health monitoring",
    ),
    "home": (
        "crew health monitoring preventive care "
        "medical support wellbeing",
    ),
}

METRIC_TERMS = {
    "sleep_hours": "sleep circadian fatigue",
    "mood": "behavioral health psychological wellbeing",
    "systolic_bp": "blood pressure cardiovascular",
    "diastolic_bp": "blood pressure cardiovascular",
    "heart_rate": "heart rate cardiovascular",
    "blood_glucose": "glucose metabolic health",
    "body_temperature": "body temperature fever",
    "bone_density": "bone mineral density exercise",
    "white_blood_cells": "white blood cells immune",
    "calorie_intake": "nutrition energy food intake",
    "protein_intake": "protein nutrition muscle",
}


def _build_query(insight: dict) -> str:
    """Build a scientific search query from the current health context.

    Parameters
    ----------
    insight : dict
        Statistical analysis for the requested page.

    Returns
    -------
    str
        English search terms for the indexed documents.
    """
    page = insight.get("page", "home")

    metrics = sorted(
        insight.get("metrics", []),
        key=lambda item: (
            -item.get("priority", 0),
            item.get("metric", ""),
        ),
    )

    page_query = PAGE_QUERIES.get(page, PAGE_QUERIES["home"])

    if isinstance(page_query, tuple):
        page_query = " ".join(page_query)

    terms = [page_query]

    for metric in metrics[:2]:
        term = METRIC_TERMS.get(metric.get("metric"))

        if term:
            terms.append(term)

    return " ".join(terms)


def retrieve_scientific_evidence(
    session: Session,
    insight: dict,
    limit: int = 3,
) -> list[dict]:
    """Retrieve relevant passages with traceable document references.

    Parameters
    ----------
    session : Session
        Active database session.
    insight : dict
        Statistical analysis returned by Orbi.
    limit : int
        Maximum number of distinct passages to return.

    Returns
    -------
    list[dict]
        Scientific passages with document, page, and source identifiers.
    """
    if not insight.get("metrics") or limit < 1:
        return []

    query = _build_query(insight)
    query_terms = set(tokenize(query))
    evidence = []
    seen_pages = set()

    try:
        hits = INDEX.search(session, query, limit=max(limit * 5, 10))

        for hit in hits:
            page_key = (hit.document_id, hit.page)

            if page_key in seen_pages:
                continue

            chunk = session.get(KnowledgeChunk, hit.chunk_id)
            if chunk is None or not chunk.text.strip():
                continue

            passage_terms = set(tokenize(chunk.text))
            matching_terms = query_terms.intersection(passage_terms)

            if len(matching_terms) < 2:
                continue

            seen_pages.add(page_key)

            evidence.append(
                {
                    "sourceId": f"doc-{hit.document_id}-page-{hit.page}",
                    "documentId": hit.document_id,
                    "title": hit.document_title,
                    "page": hit.page,
                    "excerpt": chunk.text.strip()[:1200],
                    "referencePath": (
                        f"/api/v1/knowledge/documents/"
                        f"{hit.document_id}/pages/{hit.page}"
                    ),
                }
            )

            if len(evidence) >= limit:
                break

    except Exception:
        logger.exception("Scientific evidence retrieval failed")
        return []

    return evidence
