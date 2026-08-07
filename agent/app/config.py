"""Single source of truth for configuration.

Two kinds of value live here, and the distinction is deliberate:

1.  MODULE CONSTANTS (below) — the storage contract shared with n8n. These are NOT
    environment-configurable. Changing one silently breaks interoperability with the
    n8n PGVector node or invalidates the entire index, so it must be a code change
    that shows up in review, not an env var someone flips on a Tuesday.

2.  Settings (the pydantic class) — secrets, hostnames, model names and graph tuning.
    These legitimately differ between a laptop and the compose network.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# The storage contract with n8n. Do not "clean these up".
# ---------------------------------------------------------------------------
# n8n's PGVector node and this app address ONE table. The two sides are written in
# different languages by different teams and their library defaults DISAGREE on three
# of the four column names:
#
#   column      n8n default   langchain-postgres default
#   id          id            langchain_id                 <- collision
#   content     text          content                      <- collision
#   embedding   embedding     embedding                    ok
#   metadata    metadata      langchain_metadata           <- collision
#
# Left at defaults, Python creates a table n8n cannot read, and the failure surfaces
# only when someone wires up a node in the UI and gets an empty result — not at
# insert time, when it would be obvious.
#
# We therefore pin the column names to N8N'S defaults and override on the Python side.
# Rationale for that direction: the n8n side is configured by hand in a web form, so
# it is where a typo is most likely and least visible. Matching its defaults means the
# node works with the Column Names section left untouched.
VECTOR_TABLE = "compliance_chunks"
ID_COLUMN = "id"          # n8n: idColumnName
CONTENT_COLUMN = "text"        # n8n: contentColumnName
EMBEDDING_COLUMN = "embedding"   # n8n: vectorColumnName
METADATA_COLUMN = "metadata"    # n8n: metadataColumnName

# Distance function. Both sides must agree or ranking is silently wrong rather than
# broken — the worst failure mode, because it still returns plausible-looking rows.
# n8n's node defaults to 'cosine'; nomic-embed-text is trained for cosine similarity.
DISTANCE_STRATEGY = "cosine"

# Embedding width, measured (not assumed) from a live nomic-embed-text call.
#
# THIS IS LOCKED FOR THE LIFE OF THE CORPUS. It becomes `vector(768)` in DDL. Changing
# the embedding model means a different width, which means DROP + re-embed + re-ingest
# every document. It is a constant here, referenced everywhere, so that changing it is
# a one-line diff whose blast radius is legible in review.
EMBED_DIM = 768

# n8n's optional "collection" feature (a second table for namespacing) stays OFF.
# One corpus, one table. Turning it on adds a join and a second schema to keep in sync
# for a multi-tenancy requirement that does not exist.


class Settings(BaseSettings):
    """Environment-derived configuration.

    Every field is required unless it has a default. Missing secrets raise at import
    time rather than at first use — the same fail-fast property the compose file gets
    from `${PGVECTOR_PASSWORD:?...}`. A container that starts and then 500s on the
    first real request is strictly worse than one that refuses to start.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # the shared .env also holds n8n's own keys; don't choke on them
    )

    # --- Vector store -------------------------------------------------------
    # Service name on the compose network, NOT localhost: this process runs in a
    # container alongside n8n. Overridden to 127.0.0.1 + a published port only when
    # running the ingestion script from the host during development.
    pgvector_host: str = "pgvector"
    pgvector_port: int = 5432
    pgvector_user: str = "rag"
    pgvector_db: str = "rag"
    pgvector_password: str = Field(..., description="from .env PGVECTOR_PASSWORD")

    # --- Embeddings (Ollama, local) ----------------------------------------
    # host.docker.internal is how a Colima container reaches the Mac's Ollama. Verified
    # reachable from the n8n container; re-verify after any `colima restart`, since the
    # port forward is what makes this work despite Ollama binding 127.0.0.1 on the host.
    ollama_base_url: str = "http://host.docker.internal:11434"
    embed_model: str = "nomic-embed-text"

    # --- Chat models (Groq) -------------------------------------------------
    groq_api_key: str = Field(..., description="from .env GROQ_API_KEY")

    # Three roles, named separately on purpose. The supervisor only emits a routing
    # token, so it never needs the big model; giving each role its own setting is what
    # makes the cost breakdown in the eval actionable rather than a single number.
    supervisor_model: str = "openai/gpt-oss-120b"
    analyst_model: str = "openai/gpt-oss-120b"
    # The verifier MUST differ from the analyst. A model grading its own output approves
    # it ~95%+ of the time, which collapses the A/B into "the verifier does nothing" and
    # takes the headline result with it. Enforced below, not left to discipline.
    #
    # qwen3.6-27b is a different family from gpt-oss-120b (so genuinely not self-grading),
    # is already approved for use (another internal project uses it as GROQ_VISION_MODEL), and is
    # small enough that per-claim entailment stays cheap. Confirmed reachable on this key.
    verifier_model: str = "qwen/qwen3.6-27b"

    temperature: float = 0.0  # eval reproducibility; raise only for the demo UI

    # --- n8n integration ----------------------------------------------------
    # Compose network again: the agent does NOT go out through Caddy and back in over
    # the public internet to reach a service sitting one bridge away. This keeps the
    # public /webhook* path for genuine third parties.
    n8n_base_url: str = "http://n8n:5678"
    n8n_action_webhook_path: str = "/webhook/agent-action"
    n8n_webhook_secret: str = Field(..., description="Header Auth value + HMAC key")

    # --- This service's own API --------------------------------------------
    agent_api_key: str = Field(..., description="bearer token n8n presents to us")

    # --- Graph tuning -------------------------------------------------------
    # Verifier loop bound. Graceful: on exhaustion the graph emits a refusal, which for
    # compliance questions is a correct answer, not an error. Distinct from
    # recursion_limit below, which raises GraphRecursionError — an exception, not an
    # answer, and therefore only a backstop.
    max_attempts: int = 3
    recursion_limit: int = 25  # a hit here means a genuine bug, not a hard question

    # Retrieval breadth. k_escalated applies on verifier loop-back: widening the net is
    # one of the three things that make pass N differ from pass N-1 (the others being
    # the rewritten query and dropped filters). Without a change, the loop re-derives
    # the same failed answer at 3x the cost.
    k_initial: int = 5
    k_escalated: int = 12

    # Bounds the Analyst's inner ReAct loop. Nested inside the outer verifier cycle,
    # an unbounded tool loop multiplies: 3 attempts x unbounded is unbounded.
    max_tool_calls: int = 6

    # --- Checkpointing ------------------------------------------------------
    # sqlite for day-1 development; "postgres" stores threads in the pgvector instance.
    # Load-bearing, not decorative: it is what lets the graph pause at the approval
    # interrupt, survive a container restart, and resume when n8n POSTs /resume.
    checkpointer: Literal["sqlite", "postgres"] = "sqlite"
    checkpoint_db_path: str = "./data/checkpoints.sqlite"

    @property
    def pgvector_dsn(self) -> str:
        """SQLAlchemy URL for langchain-postgres.

        The `+psycopg` driver suffix is required — it selects psycopg 3. Plain
        `postgresql://` resolves to psycopg2, which PGEngine does not support, and the
        resulting error names neither the driver nor this URL.
        """
        return (
            f"postgresql+psycopg://{self.pgvector_user}:{self.pgvector_password}"
            f"@{self.pgvector_host}:{self.pgvector_port}/{self.pgvector_db}"
        )

    @property
    def n8n_action_url(self) -> str:
        return f"{self.n8n_base_url.rstrip('/')}{self.n8n_action_webhook_path}"

    @model_validator(mode="after")
    def _verifier_must_differ_from_analyst(self) -> "Settings":
        """Fail startup rather than ship a self-grading verifier.

        This is the quiet failure the whole eval design hinges on: if the verifier is
        the analyst, it rubber-stamps its own answers, every metric looks fine, and the
        before/after table shows no difference on the last afternoon. Cheap to prevent
        here; expensive to diagnose there.
        """
        if self.verifier_model == self.analyst_model:
            raise ValueError(
                f"verifier_model and analyst_model are both {self.analyst_model!r}. "
                "A model grading its own output approves it ~95%+ of the time, which "
                "makes the Verifier node measurably useless. Set VERIFIER_MODEL to a "
                "different model."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached accessor.

    lru_cache so the .env is read once and every module sees the same object —
    importantly, so tests can clear the cache to inject a different configuration.
    """
    return Settings()  # type: ignore[call-arg]  # pydantic-settings fills from env
