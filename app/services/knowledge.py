"""PDF knowledge base: text extraction, chunking and BM25 search with page references."""

from __future__ import annotations

import hashlib
import io
import logging
import math
import re
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import KnowledgeChunk, KnowledgeDocument

log = logging.getLogger(__name__)

BUNDLED_DIR = BASE_DIR / "knowledge"
BUNDLED_TITLES = {
    "nasa-std-3001-vol-1-rev-c.pdf": "NASA-STD-3001 Volume 1, Revision C: Crew Health (2023)",
}
CHUNK_CHARS = 900
CHUNK_OVERLAP = 150

STOPWORDS = set(
    """a an and are as at be by for from has have in is it its of on or that the this to was were will with
    which who what when where how not no can may shall should must all any each per than then these those
    o os as um uma uns umas de do da dos das em no na nos nas por para com sem que se ao aos e ou é são foi
    ser ter mais menos como quando onde qual quais seu sua seus suas""".split()
)


class KnowledgeError(ValueError):
    pass


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", normalize(text)) if len(t) > 1 and t not in STOPWORDS]


def extract_pages(content: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as exc:  # pypdf raises several error types
        raise KnowledgeError(f"Not a readable PDF: {exc}") from exc
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # a broken page should not drop the document
            text = ""
        pages.append(re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", "\n", text)).strip())
    return pages


def chunk_page(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):  # cut at a sentence or line end when possible
            cut = max(text.rfind(". ", start, end), text.rfind("\n", start, end))
            if cut > start + CHUNK_CHARS // 2:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [c for c in chunks if c]


def add_document(
    session: Session, content: bytes, file_name: str, title: str | None, source: str, uploaded_by: str | None = None
) -> tuple[KnowledgeDocument, bool]:
    """Store a PDF; returns (document, created). The same file is never stored twice."""
    if not content.startswith(b"%PDF"):
        raise KnowledgeError("The file is not a PDF")
    sha = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.sha256 == sha))
    if existing is not None:
        return existing, False
    pages = extract_pages(content)
    if not any(pages):
        raise KnowledgeError("The PDF has no text layer (scanned image?); run OCR before uploading")
    doc = KnowledgeDocument(
        title=title or Path(file_name).stem.replace("_", " ").replace("-", " "),
        file_name=file_name, sha256=sha, pages=len(pages), source=source, uploaded_by=uploaded_by,
    )
    for page_no, text in enumerate(pages, start=1):
        for idx, chunk in enumerate(chunk_page(text)):
            doc.chunks.append(KnowledgeChunk(page=page_no, chunk_index=idx, text=chunk))
    session.add(doc)
    session.commit()
    INDEX.invalidate()
    return doc, True


def load_bundled(session: Session) -> int:
    added = 0
    for path in sorted(BUNDLED_DIR.glob("*.pdf")):
        try:
            _, created = add_document(session, path.read_bytes(), path.name, BUNDLED_TITLES.get(path.name), "bundled")
            added += created
        except KnowledgeError as exc:
            log.warning("Skipped bundled PDF %s: %s", path.name, exc)
    return added


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    document_title: str
    page: int
    score: float
    snippet: str


class BM25Index:
    """In-memory BM25 over all chunks; rebuilt lazily when documents change."""

    k1, b = 1.5, 0.75

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._version: tuple | None = None
        self._docs: list[tuple[int, int, str, int, str]] = []  # chunk id, doc id, title, page, text
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._lengths: list[int] = []
        self._avg = 0.0
        self._penalty: list[float] = []

    def invalidate(self) -> None:
        with self._lock:
            self._version = None

    def _ensure(self, session: Session) -> None:
        version = tuple(session.execute(select(func.count(KnowledgeChunk.id), func.max(KnowledgeChunk.id))).one())
        with self._lock:
            if version == self._version:
                return
            rows = session.execute(
                select(KnowledgeChunk.id, KnowledgeChunk.document_id, KnowledgeDocument.title,
                       KnowledgeChunk.page, KnowledgeChunk.text)
                .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            ).all()
            self._docs = [tuple(r) for r in rows]
            self._tf = [Counter(tokenize(r[4])) for r in rows]
            self._df = Counter(t for tf in self._tf for t in tf)
            self._lengths = [sum(tf.values()) for tf in self._tf]
            self._avg = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
            # Tables of contents (dot leaders) match many words but answer nothing.
            self._penalty = [0.25 if len(re.findall(r"\.{8,}", r[4])) >= 3 else 1.0 for r in rows]
            self._version = version

    def search(self, session: Session, query: str, limit: int = 5, document_id: int | None = None) -> list[Hit]:
        self._ensure(session)
        terms = tokenize(query)
        if not terms or not self._docs:
            return []
        n = len(self._docs)
        scores = []
        for i, tf in enumerate(self._tf):
            if document_id is not None and self._docs[i][1] != document_id:
                continue
            score = 0.0
            for t in terms:
                f = tf.get(t)
                if not f:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                score += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self._lengths[i] / self._avg))
            if score > 0:
                scores.append((score * self._penalty[i], i))
        scores.sort(reverse=True)
        hits = []
        for score, i in scores[:limit]:
            chunk_id, doc_id, title, page, text = self._docs[i]
            hits.append(Hit(chunk_id, doc_id, title, page, round(score, 3), snippet(text, terms)))
        return hits


def snippet(text: str, terms: list[str], width: int = 320) -> str:
    norm = normalize(text)
    positions = [m.start() for t in terms for m in [re.search(r"\b" + re.escape(t), norm)] if m]
    center = min(positions) if positions else 0
    start = max(center - width // 3, 0)
    end = min(start + width, len(text))
    out = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + out + ("…" if end < len(text) else "")


INDEX = BM25Index()
