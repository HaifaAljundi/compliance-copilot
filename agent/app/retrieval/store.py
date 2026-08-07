"""Vector store wiring — and the sole owner of the table contract shared with n8n.

Nothing else in this codebase issues DDL against the vector table or names its columns.
That containment is the point: the contract is enforceable only if there is one place to
enforce it.

The corpus is addressed by two different LangChain implementations — this one (Python,
langchain-postgres) and n8n's PGVector node (TypeScript, LangChain.js). They agree on
neither column names nor id types by default, so every override here exists to make one
side match the other. See `_COLUMN_KWARGS`.
"""

from functools import lru_cache

from langchain_ollama import OllamaEmbeddings
from langchain_postgres import PGEngine, PGVectorStore
from langchain_postgres.v2.indexes import DistanceStrategy
from sqlalchemy import Engine, create_engine, text

from app.config import (
    CONTENT_COLUMN,
    EMBED_DIM,
    EMBEDDING_COLUMN,
    ID_COLUMN,
    METADATA_COLUMN,
    VECTOR_TABLE,
    get_settings,
)

# Declared ONCE and splatted into both init_vectorstore_table() and create_sync().
#
# This is not DRY-for-its-own-sake. The two calls take the same column arguments with the
# same defaults, so writing them out twice invites exactly one bug: the table gets created
# with one set of names and read with another. That failure returns empty results rather
# than an error — the retriever "works", finds nothing, and the graph blames retrieval
# quality. Splatting one dict makes drift impossible.
_COLUMN_KWARGS = {
    "content_column": CONTENT_COLUMN,
    "embedding_column": EMBEDDING_COLUMN,
    "metadata_json_column": METADATA_COLUMN,
    "id_column": ID_COLUMN,
}

# n8n's node defaults to 'cosine'; langchain-postgres defaults to COSINE_DISTANCE. They
# already agree, but it is stated explicitly because a mismatch here does NOT raise — it
# silently reorders results. A retriever that returns plausible-but-wrongly-ranked chunks
# is far harder to notice than one that errors.
_DISTANCE = DistanceStrategy.COSINE_DISTANCE


@lru_cache
def get_embeddings() -> OllamaEmbeddings:
    """Local embedding model. No egress, no per-token cost.

    Consequential for a compliance corpus: document text never leaves the machine at
    index time. Only the eventual question and retrieved excerpts reach Groq.
    """
    s = get_settings()
    return OllamaEmbeddings(model=s.embed_model, base_url=s.ollama_base_url)


@lru_cache
def get_engine() -> PGEngine:
    """Connection pool for the vector store. Cached: one pool per process.

    Note this pool is ASYNC internally, even behind the *_sync helpers. Schema
    introspection therefore uses its own sync engine below rather than borrowing this
    one — see get_sync_engine().
    """
    return PGEngine.from_connection_string(url=get_settings().pgvector_dsn)


@lru_cache
def get_sync_engine() -> Engine:
    """Plain SQLAlchemy engine, for schema introspection only.

    Separate from PGEngine on purpose. PGEngine wraps an async pool whose connections
    can't be used from synchronous code, and its only sync escape hatch is a private
    method. Introspection is a different concern from vector I/O anyway — it asks about
    the table's shape, not its contents — so it gets its own small, public-API connection
    rather than reaching into library internals that a 0.0.x release may rename.
    """
    return create_engine(get_settings().pgvector_dsn, pool_pre_ping=True)


def table_exists() -> bool:
    with get_sync_engine().connect() as conn:
        return bool(
            conn.execute(
                text("select to_regclass(:t) is not null"), {"t": f"public.{VECTOR_TABLE}"}
            ).scalar()
        )


def init_table(*, overwrite: bool = False) -> None:
    """Create the vector table, with the column names n8n expects.

    `id_column` as a plain string produces a UUID column (verified in the library source),
    which is what n8n's node expects — so the id type needs no special handling, but it
    does need to not be "fixed" into a text column by a future reader.

    `vector_size=EMBED_DIM` becomes `vector(768)`. This is the irreversible decision in
    this file: it is fixed for the life of the corpus, and switching embedding models
    later means dropping and re-ingesting everything.
    """
    if table_exists() and not overwrite:
        return
    get_engine().init_vectorstore_table(
        table_name=VECTOR_TABLE,
        vector_size=EMBED_DIM,
        store_metadata=True,
        overwrite_existing=overwrite,
        **_COLUMN_KWARGS,
    )
    _coerce_metadata_to_jsonb()
    _add_id_default()


def _add_id_default() -> None:
    """Give the id column a server-side default.

    The two implementations disagree about WHO generates the primary key:

      * Python (langchain-postgres) generates a UUID client-side and includes it in the
        INSERT. It therefore works fine against a column with no default.
      * TypeScript (LangChain.js, which n8n's PGVector node runs) omits the column
        entirely and expects the DATABASE to fill it.

    `init_vectorstore_table` creates `id uuid NOT NULL` with NO default, so an insert from
    n8n fails with:

        null value in column "id" of relation "compliance_chunks" violates
        not-null constraint

    Unlike the other interop mismatches in this file, this one at least fails LOUDLY —
    but only on the n8n side, and only at insert time, so it is invisible until someone
    actually builds a workflow that writes.

    gen_random_uuid() is built into PostgreSQL 13+; no pgcrypto extension needed.

    Idempotent: setting the same default twice is a no-op.
    """
    with get_sync_engine().begin() as conn:
        conn.execute(
            text(
                f'ALTER TABLE public."{VECTOR_TABLE}" '
                f'ALTER COLUMN "{ID_COLUMN}" SET DEFAULT gen_random_uuid()'
            )
        )


def _coerce_metadata_to_jsonb() -> None:
    """Convert the metadata column from `json` to `jsonb`.

    langchain-postgres creates it as `json`. n8n's PGVector node — and effectively every
    metadata filter built on pgvector — filters with the `@>` containment operator, which
    PostgreSQL defines for `jsonb` ONLY. Verified on this database:

        select ('{"a":1}'::json)  @> '{"a":1}'  ->  ERROR: no operator matches
        select ('{"a":1}'::jsonb) @> '{"a":1}'  ->  true

    So on a `json` column, inserts and reads from n8n succeed and only *filtered* queries
    fail. That is the worst shape of bug available here: the corpus looks fine, unfiltered
    retrieval looks fine, and "give me only chunks from document X" quietly returns
    nothing or errors deep in a node's stack trace.

    jsonb is the better choice independently — it is the only one that can carry a GIN
    index, which is what makes metadata filters fast once the corpus grows.

    Idempotent: ALTER on a column that is already jsonb is a no-op cast.
    """
    with get_sync_engine().begin() as conn:
        conn.execute(
            text(
                f'ALTER TABLE public."{VECTOR_TABLE}" '
                f'ALTER COLUMN "{METADATA_COLUMN}" TYPE jsonb '
                f'USING "{METADATA_COLUMN}"::jsonb'
            )
        )


@lru_cache
def get_store() -> PGVectorStore:
    """The store used for both ingestion and retrieval.

    Same column kwargs as init_table — see the note on _COLUMN_KWARGS.
    """
    return PGVectorStore.create_sync(
        engine=get_engine(),
        embedding_service=get_embeddings(),
        table_name=VECTOR_TABLE,
        distance_strategy=_DISTANCE,
        **_COLUMN_KWARGS,
    )


# What n8n's PGVector node requires, as actual Postgres types. Asserted against the live
# database rather than trusted, because every value here is a default that disagreed with
# langchain-postgres and had to be overridden.
_EXPECTED_SCHEMA = {
    ID_COLUMN: "uuid",
    CONTENT_COLUMN: "text",
    METADATA_COLUMN: "jsonb",
    EMBEDDING_COLUMN: "USER-DEFINED",  # how information_schema reports the `vector` type
}


def verify_contract() -> list[str]:
    """Check the live table against what n8n needs. Returns problems; empty means good.

    Run this before ingesting anything and after any langchain-postgres upgrade. The
    failure it guards against is not a crash — it is n8n's node returning zero rows from
    a table that is visibly full, which reads as "retrieval is bad" and costs a day.
    """
    problems: list[str] = []
    if not table_exists():
        return [f"table public.{VECTOR_TABLE} does not exist — run init_table()"]

    with get_sync_engine().connect() as conn:
        rows = conn.execute(
            text(
                "select column_name, data_type from information_schema.columns "
                "where table_schema='public' and table_name=:t"
            ),
            {"t": VECTOR_TABLE},
        ).fetchall()
        actual = {r[0]: r[1] for r in rows}

        for col, expected_type in _EXPECTED_SCHEMA.items():
            if col not in actual:
                problems.append(f"missing column {col!r} (n8n cannot read this table)")
            elif actual[col] != expected_type:
                problems.append(
                    f"column {col!r} is {actual[col]!r}, n8n expects {expected_type!r}"
                )

        # Width is checked separately: information_schema reports `vector` as
        # USER-DEFINED without its dimension, so a 768-vs-1024 mismatch would pass the
        # type check above and then fail on every insert.
        dim = conn.execute(
            text(
                "select a.atttypmod from pg_attribute a "
                "join pg_class c on c.oid = a.attrelid "
                "where c.relname = :t and a.attname = :col"
            ),
            {"t": VECTOR_TABLE, "col": EMBEDDING_COLUMN},
        ).scalar()
        if dim is not None and dim != EMBED_DIM:
            problems.append(
                f"embedding is vector({dim}) but EMBED_DIM is {EMBED_DIM} — "
                "every insert will fail; the corpus must be re-ingested to change this"
            )

        # The id column must generate its own value.
        #
        # Checking names and types was not enough: this table passed every earlier check
        # while still rejecting every insert from n8n, because Python supplies the id and
        # LangChain.js expects the database to. A contract between two writers covers who
        # provides which column, not just what the columns are called.
        id_default = conn.execute(
            text(
                "select column_default from information_schema.columns "
                "where table_schema='public' and table_name=:t and column_name=:c"
            ),
            {"t": VECTOR_TABLE, "c": ID_COLUMN},
        ).scalar()
        if not id_default:
            problems.append(
                f"column {ID_COLUMN!r} has no DEFAULT — inserts from n8n will fail with "
                "'null value in column violates not-null constraint'. Python supplies the "
                "id client-side; LangChain.js does not. Run init_table() to add "
                "gen_random_uuid()."
            )

    return problems
