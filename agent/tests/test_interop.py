"""Interop tests for the vector table shared with n8n.

Marked `integration` because they need live pgvector and Ollama:

    pytest -m integration          # these
    pytest -m 'not integration'    # everything else, offline, no containers

Why these exist at all: the corpus is written and read by two different LangChain
implementations in two languages, whose defaults disagree on three of four column names,
on the metadata column type, and potentially on embedding width. Every one of those
mismatches fails *quietly* — empty results or wrong ranking, not an exception. A test is
the only thing that turns a silent failure into a loud one.

Run these after any langchain-postgres upgrade, before trusting a retrieval metric.
"""

import pytest
from langchain_core.documents import Document
from sqlalchemy import text

from app.config import EMBED_DIM, VECTOR_TABLE
from app.retrieval.store import (
    get_embeddings,
    get_store,
    get_sync_engine,
    init_table,
    verify_contract,
)

pytestmark = pytest.mark.integration

PROBE_URL = "https://interop.test/probe"


@pytest.fixture(autouse=True)
def _clean():
    """Remove probe rows before and after, so a failed run can't poison the corpus."""
    init_table()
    _delete_probes()
    yield
    _delete_probes()


def _delete_probes() -> None:
    with get_sync_engine().begin() as c:
        c.execute(
            text(f"delete from {VECTOR_TABLE} where metadata->>'url' = :u"), {"u": PROBE_URL}
        )


def test_schema_matches_n8n_contract():
    """The column names and types n8n's PGVector node requires.

    Guards the specific collisions found on 2026-08-06: langchain-postgres defaults to
    langchain_id/content/langchain_metadata where n8n needs id/text/metadata, and creates
    metadata as `json` where n8n needs `jsonb`.
    """
    assert verify_contract() == []


def test_embedding_width_is_locked():
    """768 is fixed for the life of the corpus — a change means re-ingesting everything."""
    assert len(get_embeddings().embed_query("probe")) == EMBED_DIM


def test_python_write_is_readable_the_way_n8n_reads():
    """Write via langchain-postgres, read via the raw SQL shape n8n's node issues.

    This is the direction that would break if the column overrides in store.py drifted:
    Python would keep working against its own names while n8n silently found nothing.
    """
    store = get_store()
    store.add_documents(
        [
            Document(
                page_content="A suspicious transaction report must be filed without delay.",
                metadata={"doc_id": "probe-doc", "section": "Article 15(2)", "url": PROBE_URL},
            ),
            Document(
                page_content="Annual leave entitlement is thirty calendar days.",
                metadata={"doc_id": "probe-other", "section": "Clause 4", "url": PROBE_URL},
            ),
        ]
    )

    q = get_embeddings().embed_query("when must I report a suspicious transaction?")
    with get_sync_engine().connect() as c:
        rows = c.execute(
            text(
                f"select text, metadata->>'section' as section "
                f"from {VECTOR_TABLE} "
                # `@>` is the jsonb containment operator. It does NOT exist for `json` —
                # this line is what fails if the metadata column type ever regresses.
                f"where metadata @> '{{\"doc_id\":\"probe-doc\"}}' "
                f"order by embedding <=> :q limit 5"
            ),
            {"q": str(q)},
        ).fetchall()

    assert len(rows) == 1, "jsonb metadata filter did not isolate the target document"
    assert rows[0].section == "Article 15(2)"


def test_cosine_ranking_is_semantic():
    """Guards against a distance-strategy mismatch, which misranks rather than errors."""
    store = get_store()
    store.add_documents(
        [
            Document(
                page_content="Customer due diligence must precede a business relationship.",
                metadata={"doc_id": "probe-doc", "section": "Article 8(1)", "url": PROBE_URL},
            ),
            Document(
                page_content="The office cafeteria closes at four in the afternoon.",
                metadata={"doc_id": "probe-doc", "section": "Notice 3", "url": PROBE_URL},
            ),
        ]
    )
    # Scoped to the probe document. Without the filter this searches the whole ingested
    # corpus, where hundreds of real chunks legitimately outrank two synthetic probes —
    # the test would fail for a reason that has nothing to do with distance strategy.
    hits = store.similarity_search(
        "what checks are needed before onboarding a client?",
        k=2,
        filter={"doc_id": "probe-doc"},
    )
    assert hits[0].metadata["section"] == "Article 8(1)", (
        "cosine ranking put an irrelevant chunk first — check DISTANCE_STRATEGY agrees "
        "with n8n's node setting"
    )


def test_id_column_generates_its_own_value():
    """The two writers disagree about who supplies the primary key.

    Python (langchain-postgres) generates a UUID client-side and includes it in the
    INSERT, so it works against a column with no default. LangChain.js — which n8n's
    PGVector node runs — omits the column and expects the database to fill it.

    `init_vectorstore_table` creates `id uuid NOT NULL` with NO default, so every insert
    from n8n failed with:

        null value in column "id" of relation "compliance_chunks"
        violates not-null constraint

    Found only by actually building an n8n workflow that writes. Column names and types
    all matched; the contract gap was about OWNERSHIP of a column, which a name/type check
    cannot see.
    """
    with get_sync_engine().connect() as conn:
        default = conn.execute(
            text(
                "select column_default from information_schema.columns "
                "where table_schema='public' and table_name=:t and column_name='id'"
            ),
            {"t": VECTOR_TABLE},
        ).scalar()
    assert default and "gen_random_uuid" in default, (
        "id has no server-side default — n8n cannot insert into this table"
    )


def test_insert_without_an_id_succeeds():
    """The behavioural version of the check above: exactly the SQL shape n8n emits."""
    with get_sync_engine().begin() as conn:
        conn.execute(
            text(
                f"insert into {VECTOR_TABLE} (text, embedding, metadata) "
                f"values ('interop id probe', array_fill(0.01, ARRAY[{EMBED_DIM}])::vector, "
                f"'{{\"url\":\"{PROBE_URL}\"}}'::jsonb)"
            )
        )
        row = conn.execute(
            text(
                f"select id, vector_dims(embedding) from {VECTOR_TABLE} "
                f"where metadata->>'url' = :u"
            ),
            {"u": PROBE_URL},
        ).first()
    assert row is not None and row[0] is not None, "database did not generate an id"
    assert row[1] == EMBED_DIM
