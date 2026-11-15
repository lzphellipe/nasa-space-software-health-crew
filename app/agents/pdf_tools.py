
"""Read and search internal PDF documents with page references."""

from functools import lru_cache
from pathlib import Path
import re

from pypdf import PdfReader


DOCUMENTS_DIR = Path(__file__).resolve().parents[1] / "documents"


@lru_cache(maxsize=8)
def load_pdf(path: str) -> list[tuple[int, str]]:
    """
    Extract text from a PDF document.

    Parameters
    ----------
    path : str
        Absolute path to the PDF document.

    Returns
    -------
    list[tuple[int, str]]
        List of page numbers and their extracted text.
    """
    pages = []

    for number, page in enumerate(PdfReader(path).pages, start=1):
        content = (page.extract_text() or "").strip()

        if content:
            pages.append((number, content))

    if not pages:
        raise ValueError(
            "The PDF contains no extractable text. "
            "Scanned documents require OCR."
        )

    return pages


def search_pdf(
    question: str,
    filename: str = "protocolo_saude.pdf",
    limit: int = 4,
) -> str:
    """
    Retrieve relevant PDF pages based on keyword overlap.

    Parameters
    ----------
    question : str
        Question submitted by the user.
    filename : str
        Name of the authorized PDF file in the documents directory.
    limit : int
        Maximum number of pages to retrieve.

    Returns
    -------
    str
        Relevant page excerpts with source references.
    """
    if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
        raise ValueError("Invalid document filename.")

    path = DOCUMENTS_DIR / filename

    if not path.is_file():
        raise FileNotFoundError(f"Document not found: {filename}")

    words = set(re.findall(r"\w{3,}", question.lower()))
    ranked = []

    for number, content in load_pdf(str(path)):
        tokens = set(re.findall(r"\w{3,}", content.lower()))
        score = len(words & tokens)

        if score:
            ranked.append((score, number, content))

    ranked.sort(key=lambda item: (-item[0], item[1]))

    return "\n\n".join(
        f"[Page {number}]\n{content[:4500]}"
        for _, number, content in ranked[:limit]
    )
