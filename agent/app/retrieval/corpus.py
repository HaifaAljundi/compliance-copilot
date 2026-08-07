"""What to ingest — declarative source definitions, separate from how ingestion works.

Kept apart from ingest.py because these are the two things that change for different
reasons: this file changes when the corpus changes (a new regulation, an amended
version), ingest.py changes when the chunking strategy changes. Mixing them means every
corpus edit risks the parser and vice versa.

Every document here is publicly published by its issuing authority for compliance
purposes. Text is fetched into a private local index; nothing is redistributed.

STRUCTURE VERIFIED, NOT ASSUMED. Each spec's heading pattern was checked against the
actual extracted text on 2026-08-06. The two issuers number sections differently, which
is exactly why heading_re is per-document rather than one global regex:

    DIFC   "12.     Consent"              -> `12.` then title      (single level)
    CBUAE  "1.1.    Purpose"              -> `1.1.` trailing dot   (hierarchical)

A single pattern covering both would either miss CBUAE's trailing dot or swallow DIFC's
numbered list items as if they were headings.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentSpec:
    doc_id: str
    title: str
    issuer: str
    url: str
    domain: str
    # Matches a section heading in the extracted text. Group 1 = the section number,
    # group 2 = its title. Anchored at line start; leading whitespace is tolerated
    # because pdftotext -layout preserves the PDF's indentation.
    heading_re: str
    # What this issuer calls a numbered division, used verbatim in citations. DIFC's law
    # self-cites as "Article 32"; the CBUAE guidance uses "Section 3.10". Explicit rather
    # than inferred, so a citation always reads the way the source document reads.
    section_label: str = "Section"
    # Matches a division that RESTARTS section numbering — e.g. "SCHEDULE 1". Group 1 is
    # the division name. Legal documents routinely restart at 1 inside a schedule, so
    # without this, "Article 1" names two different texts and a citation is ambiguous.
    #
    # Contrast with the Part headings note below: schedules qualify because their headings
    # are reliably present in the body AND unambiguously end the previous sequence. Only
    # carry forward what the document actually states.
    division_re: str | None = None
    # What a numbered item inside a division is called.
    division_item_label: str = "Paragraph"


# `-layout` preserves indentation, which is what makes the heading patterns anchorable.
# It also preserves the table-of-contents dot leaders, which ingest.py strips.
DIFC_DATA_PROTECTION = DocumentSpec(
    doc_id="difc-dp-law-5-2020",
    title="DIFC Data Protection Law, DIFC Law No. 5 of 2020 (consolidated)",
    issuer="Dubai International Financial Centre",
    url="https://assets.u.ae/api/public/content/bb94d503629243a7aa548a8cd387ea86?v=df38af26",
    domain="data-protection",
    # "12.     Consent" — one or two digits, a dot, whitespace, then a capitalised title.
    # Requiring the capital letter is what rejects "(1)" sub-clauses and page numbers.
    heading_re=r"^\s*(\d{1,3})\.\s+([A-Z][A-Za-z][^\n]{2,80})$",
    section_label="Article",
    # "SCHEDULE 1" / "SCHEDULE 2", centred and upper-case on their own line (verified at
    # lines 2748 and 3031 of the extracted text). Each restarts numbering at 1, so the
    # Law's "Article 1: Title and repeal" and Schedule 1's "1. Rules of interpretation"
    # would otherwise both be cited as "Article 1" — and collide on chunk_id.
    division_re=r"^\s*(SCHEDULE\s+\d+)\s*$",
)
# NOTE on "Part" headings, deliberately NOT captured.
#
# The law is organised into Parts 1-10, and it is tempting to carry the current Part into
# each chunk as extra context. Verified on the actual extracted text: only the
# sub-lettered Parts (2A, 2B, 2C, 2D, 3A, 3B) appear as headings in the body. Parts 1, 4,
# 5 ... 10 exist ONLY in the table of contents (which ingest.py strips) and as inline
# cross-references like "the sanctions set out in Part 9".
#
# So a carry-forward has nothing to terminate it: it labelled 44 sections spanning
# Articles 1-65 as "Part 3B". In a compliance citation a confidently wrong locator is
# worse than a missing one, and the field earns nothing — the law self-cites as
# "Article 32", never by Part. Dropped rather than half-fixed.
#
# If Part context is ever wanted, the honest source is the table of contents, which is
# the document's own authoritative article -> Part index. That is a parser, not a regex.

CBUAE_TFS_GUIDANCE = DocumentSpec(
    doc_id="cbuae-tfs-guidance",
    title="CBUAE Guidance for LFIs on Targeted Financial Sanctions",
    issuer="Central Bank of the UAE",
    url="https://rulebook.centralbank.ae/sites/default/files/en_net_file_store/CBUAE_EN_6593_VER1.pdf",
    domain="aml-sanctions",
    # "1.1.  Purpose" / "2.  The Role and Significance of CDD" — note the TRAILING dot
    # after the last number, which is why DIFC's pattern does not fit here.
    heading_re=r"^\s*(\d{1,2}(?:\.\d{1,2})*)\.\s+([A-Z][A-Za-z][^\n]{2,90})$",
)

CBUAE_CDD_GUIDANCE = DocumentSpec(
    doc_id="cbuae-cdd-guidance",
    title="CBUAE Guidance for LFIs on Customer Due Diligence and Correspondent Banking",
    issuer="Central Bank of the UAE",
    url="https://rulebook.centralbank.ae/sites/default/files/en_net_file_store/CBUAE_EN_6587_VER1.pdf",
    domain="aml-kyc",
    heading_re=r"^\s*(\d{1,2}(?:\.\d{1,2})*)\.\s+([A-Z][A-Za-z][^\n]{2,90})$",
)

CORPUS: list[DocumentSpec] = [
    DIFC_DATA_PROTECTION,
    CBUAE_TFS_GUIDANCE,
    CBUAE_CDD_GUIDANCE,
]

# Two distinct domains is a deliberate property of this corpus, not an accident of what
# was available. A single-domain corpus makes the supervisor's routing and the
# retriever's doc_id filter untestable — every question would have one plausible source,
# so a broken filter would still score perfectly.
DOMAINS = sorted({d.domain for d in CORPUS})

# rulebook.centralbank.ae returns 403 to default client user-agents. Verified: the same
# URLs return 200 application/pdf with a browser UA. Not an attempt to evade anything —
# these are public documents linked from the CBUAE's own guidance pages.
FETCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def by_id(doc_id: str) -> DocumentSpec:
    for spec in CORPUS:
        if spec.doc_id == doc_id:
            return spec
    raise KeyError(f"unknown doc_id {doc_id!r}; known: {[d.doc_id for d in CORPUS]}")
