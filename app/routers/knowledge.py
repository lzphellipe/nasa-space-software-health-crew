"""Search the PDF knowledge base (NASA-STD-3001 Vol. 1 is bundled; the care team can upload more)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_care_team
from app.config import get_settings
from app.db import get_db
from app.models import KnowledgeChunk, KnowledgeDocument, User
from app.schemas import APIModel
from app.services.knowledge import INDEX, KnowledgeError, add_document

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge (PDF search)"], dependencies=[Depends(get_current_user)])


class DocumentOut(APIModel):
    id: int
    title: str
    file_name: str
    pages: int
    chunks: int
    source: str
    uploaded_by: str | None


class HitOut(APIModel):
    document_id: int
    document_title: str
    page: int
    score: float
    snippet: str


class SearchOut(APIModel):
    query: str
    total: int
    hits: list[HitOut]


class PageOut(APIModel):
    document_id: int
    document_title: str
    page: int
    text: str


def document_out(db: Session, d: KnowledgeDocument) -> DocumentOut:
    chunks = db.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.document_id == d.id)) or 0
    return DocumentOut(
        id=d.id, title=d.title, file_name=d.file_name, pages=d.pages, chunks=chunks,
        source=d.source, uploaded_by=d.uploaded_by,
    )


@router.get("/documents", response_model=list[DocumentOut], summary="Documents in the knowledge base")
def list_documents(db: Session = Depends(get_db)):
    return [document_out(db, d) for d in db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.id))]


@router.get("/search", response_model=SearchOut, summary="Keyword search with page references")
def search(
    q: str = Query(..., min_length=2, description="Words to look for, in the document's language"),
    limit: int = Query(5, ge=1, le=20),
    document_id: int | None = Query(None, alias="documentId"),
    db: Session = Depends(get_db),
):
    hits = INDEX.search(db, q, limit, document_id)
    return SearchOut(
        query=q, total=len(hits),
        hits=[HitOut(document_id=h.document_id, document_title=h.document_title, page=h.page,
                     score=h.score, snippet=h.snippet) for h in hits],
    )


@router.get("/documents/{document_id}/pages/{page}", response_model=PageOut, summary="Full text of one page")
def read_page(document_id: int, page: int, db: Session = Depends(get_db)):
    doc = db.get(KnowledgeDocument, document_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    if not 1 <= page <= doc.pages:
        raise HTTPException(404, f"Page must be between 1 and {doc.pages}")
    chunks = db.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id, KnowledgeChunk.page == page)
        .order_by(KnowledgeChunk.chunk_index)
    ).all()
    text = chunks[0].text if chunks else ""
    for c in chunks[1:]:  # chunks overlap; append only the new tail
        overlap = next((k for k in range(min(len(text), len(c.text)), 0, -1) if text.endswith(c.text[:k])), 0)
        text += c.text[overlap:]
    return PageOut(document_id=doc.id, document_title=doc.title, page=page, text=text)


@router.post("/documents", response_model=DocumentOut, status_code=201, summary="Upload a PDF (care team)")
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    user: User = Depends(require_care_team),
    db: Session = Depends(get_db),
):
    limit = int(get_settings().knowledge_max_upload_mb * 1024 * 1024)
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, f"File larger than {get_settings().knowledge_max_upload_mb:g} MB")
    try:
        doc, created = add_document(db, content, file.filename or "upload.pdf", title, "upload", user.username)
    except KnowledgeError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not created:
        raise HTTPException(409, f"This PDF is already in the knowledge base (document {doc.id})")
    return document_out(db, doc)
