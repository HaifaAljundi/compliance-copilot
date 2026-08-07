"""Fetch → extract → section-aware chunk → embed → upsert.

The central decision in this file: sections are found FIRST, and size-based splitting
happens only *within* a section. Chunks never span a section boundary.

That ordering is what makes citation possible. A generic character splitter cuts wherever
the character budget runs out — routinely mid-article — and produces a chunk whose text
belongs to Article 12 and Article 13 at once. There is no honest `section` value for such
a chunk, so the Verifier cannot check a citation against it and the answer cannot say
where it came from. Retrieval quality would look fine; citation quality would be
unfixable downstream. Getting this wrong is not recoverable by better prompting.

Requires the `pdftotext` binary (poppler-utils) — see extract_text().
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import text as sql

from app.retrieval.corpus import CORPUS, FETCH_USER_AGENT, DocumentSpec
from app.retrieval.store import get_store, get_sync_engine, init_table
from app.config import VECTOR_TABLE

# Chunk sizing. ~1200 chars keeps a typical article or sub-clause group intact while
# staying well inside nomic-embed-text's window. Overlap carries a sentence of context
# across a split so a chunk that begins mid-argument still embeds sensibly.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150

# Lines with 6+ consecutive dots are table-of-contents leaders ("1.2 Purpose ...... 5").
# They must go: a ToC chunk is lexically dense with heading words, so it scores well on
# almost any query and pushes real content out of top-k. This one filter is worth
# several points of recall.
_TOC_LEADER = re.compile(r"\.{6,}")

# "1 Available at https://www.centralbank.ae/..." — a footnote, not a section. It matches
# the CBUAE heading pattern (digit, space, capital), so it needs explicit exclusion.
_FOOTNOTE = re.compile(r"^\s*\d{1,2}\s+\S.*https?://")

# pdftotext -layout preserves the PDF's column indentation, which leaves ragged runs of
# spaces inside wrapped headings ("2. The Role and     Significance    of   CDD").
_WS = re.compile(r"\s+")


@dataclass
class Section:
    number: str            # "12" (DIFC) or "3.2.4" (CBUAE)
    title: str
    body: str
    division: str | None   # "Schedule 1" when inside one; None in the main text


def fetch_pdf(spec: DocumentSpec, cache_dir: Path) -> Path:
    """Download to a local cache. Re-ingestion should not re-download.

    The browser user-agent is required for rulebook.centralbank.ae, which 403s default
    client UAs. Verified: identical URLs return 200 application/pdf with this header.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{spec.doc_id}.pdf"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    with httpx.Client(follow_redirects=True, timeout=90.0) as client:
        resp = client.get(spec.url, headers={"User-Agent": FETCH_USER_AGENT})
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "pdf" not in ctype.lower():
            # Fail loudly. A login page or an error page saved as .pdf would extract to
            # plausible-looking garbage and silently become part of the corpus.
            raise ValueError(f"{spec.doc_id}: expected PDF, got {ctype!r} from {spec.url}")
        dest.write_bytes(resp.content)
    return dest


def extract_text(pdf_path: Path) -> str:
    """Extract with `pdftotext -layout` (poppler).

    -layout preserves the original indentation. That matters because the heading patterns
    in corpus.py are anchored with `^\\s*`, and because losing column layout on these
    documents interleaves footnotes into body text.

    This is a system binary, not a Python package: the container image must install
    poppler-utils. Preferred over a pure-Python extractor anyway — pypdf's output on these
    two-column regulatory PDFs interleaves columns, which corrupts section boundaries.
    """
    out = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True,
        check=True,
    )
    return out.stdout.decode("utf-8", errors="replace")


def split_sections(raw: str, spec: DocumentSpec) -> list[Section]:
    """Walk the document, emitting one Section per heading.

    A line-by-line state machine rather than a regex split, because a section's body is
    everything up to the next heading — including blank lines and sub-clause numbering
    that a split pattern would have to re-assemble.
    """
    heading_re = re.compile(spec.heading_re)
    division_re = re.compile(spec.division_re) if spec.division_re else None

    sections: list[Section] = []
    current: Section | None = None
    division: str | None = None

    for line in raw.splitlines():
        if _TOC_LEADER.search(line) or _FOOTNOTE.match(line):
            continue

        if division_re and (m := division_re.match(line)):
            # A schedule boundary. Everything after it renumbers from 1, so the division
            # must be captured BEFORE the heading check or the first schedule item is
            # attributed to the main text.
            if current and current.body.strip():
                sections.append(current)
                current = None
            division = m.group(1).strip().title()
            continue

        if m := heading_re.match(line):
            if current and current.body.strip():
                sections.append(current)
            current = Section(
                number=m.group(1).strip().rstrip("."),
                title=_WS.sub(" ", m.group(2)).strip(),
                body="",
                division=division,
            )
            continue

        if current is not None:
            current.body += line + "\n"

    if current and current.body.strip():
        sections.append(current)
    return sections


def stable_chunk_id(doc_id: str, section_label: str, ordinal: int) -> str:
    """Deterministic id, so re-ingesting the same document produces the same ids.

    Derived from position (doc, section, ordinal) and NOT from the chunk text: a
    typo-level amendment upstream would otherwise change every downstream id and orphan
    every citation already recorded in an eval run.
    """
    return hashlib.sha256(f"{doc_id}|{section_label}|{ordinal}".encode()).hexdigest()[:16]


def chunk_document(spec: DocumentSpec, raw: str) -> list[Document]:
    """Sections -> LangChain Documents carrying the metadata a citation needs."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_CHARS,
        chunk_overlap=CHUNK_OVERLAP,
        # Ordered most- to least-preferred break point. "\n(" is deliberately high:
        # in both documents it starts a numbered sub-clause, so preferring it means a
        # split lands between clauses rather than inside a legal sentence.
        separators=["\n\n", "\n(", "\n", ". ", " ", ""],
    )

    docs: list[Document] = []
    for section in split_sections(raw, spec):
        body = _WS.sub(" ", section.body).strip()
        if len(body) < 80:
            continue  # heading with no substance — a page artefact, not content

        # The locator a human would cite, in the issuing document's own convention.
        # Inside a schedule the numbering restarts, so the division qualifies it —
        # "Schedule 1, Paragraph 1" is unambiguous where bare "Article 1" is not.
        if section.division:
            label = f"{section.division}, {spec.division_item_label} {section.number}"
        else:
            label = f"{spec.section_label} {section.number}"

        for i, piece in enumerate(splitter.split_text(body)):
            docs.append(
                Document(
                    page_content=piece,
                    metadata={
                        # Keyed on the full label, not the bare number: that is what makes
                        # schedule paragraphs distinct from main-text articles.
                        "chunk_id": stable_chunk_id(spec.doc_id, label, i),
                        "doc_id": spec.doc_id,
                        "section": label,
                        "section_title": section.title,
                        "title": spec.title,
                        "issuer": spec.issuer,
                        "domain": spec.domain,
                        "url": spec.url,
                        "chunk_index": i,
                    },
                )
            )
    return docs


def ingest(spec: DocumentSpec, cache_dir: Path) -> int:
    """Ingest one document, replacing any previous version of it.

    Delete-then-insert scoped to this doc_id, rather than an upsert: an amended
    regulation may have FEWER sections than before, and an upsert would leave the deleted
    ones in the index as retrievable, uncorrected, and outdated text — the worst possible
    residue in a compliance corpus.
    """
    init_table()
    raw = extract_text(fetch_pdf(spec, cache_dir))
    docs = chunk_document(spec, raw)
    if not docs:
        raise ValueError(f"{spec.doc_id}: produced 0 chunks — check heading_re in corpus.py")

    with get_sync_engine().begin() as conn:
        conn.execute(
            sql(f"delete from {VECTOR_TABLE} where metadata->>'doc_id' = :d"),
            {"d": spec.doc_id},
        )
    get_store().add_documents(docs)
    return len(docs)


def ingest_all(cache_dir: Path | None = None) -> dict[str, int]:
    cache_dir = cache_dir or Path(tempfile.gettempdir()) / "compliance-corpus"
    return {spec.doc_id: ingest(spec, cache_dir) for spec in CORPUS}


if __name__ == "__main__":
    for doc_id, n in ingest_all(Path("./corpus")).items():
        print(f"{doc_id:<26} {n:>4} chunks")
