"""Catalog of data provenance types and sources."""

from __future__ import annotations

from fastapi import APIRouter

from app import provenance
from app.schemas import APIModel

router = APIRouter(prefix="/api/v1/provenance", tags=["provenance"])


class ProvenanceType(APIModel):
    code: str
    label: str
    description: str


class ProvenanceSource(APIModel):
    id: str
    provenance: str
    name: str
    population: str
    url: str | None
    license: str
    endpoints: list[str]


class ProvenanceRoute(APIModel):
    prefix: str
    provenance: list[str]


class ProvenanceCatalog(APIModel):
    header: str
    types: list[ProvenanceType]
    sources: list[ProvenanceSource]
    routes: list[ProvenanceRoute]


@router.get("", response_model=ProvenanceCatalog, summary="What each provenance value means and which sources feed each route")
def catalog():
    return ProvenanceCatalog(
        header="X-Data-Provenance",
        types=[ProvenanceType(**t) for t in provenance.TYPES],
        sources=[ProvenanceSource(**s) for s in provenance.SOURCES],
        routes=[ProvenanceRoute(prefix=p, provenance=v) for p, v in sorted(provenance.ROUTES)],
    )
