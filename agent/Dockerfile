# Pinned base, matching this repo's convention for every image (never :latest).
FROM python:3.13-slim-bookworm

# poppler-utils provides `pdftotext`, which ingest.py shells out to.
#
# A SYSTEM dependency, not a Python one — so it is invisible to pyproject.toml and will
# not fail until the first ingestion, deep inside a running container. Installed here
# explicitly rather than discovered later.
#
# -layout preservation is why poppler is used over a pure-Python extractor: the section
# heading patterns in corpus.py are anchored to the PDF's original indentation, and
# pypdf interleaves columns on these two-column regulatory documents.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, in their own layer: application edits then rebuild in seconds
# instead of re-resolving the whole dependency tree.
COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && uv pip install --system --no-cache .

COPY app ./app
COPY evals ./evals

# Non-root. This container holds a Groq key and reaches the vector store; there is no
# reason for it to run as root.
RUN useradd --create-home --uid 10001 agent \
 && mkdir -p /app/data /app/corpus \
 && chown -R agent:agent /app
USER agent

EXPOSE 8000

# Matches the compose healthcheck and the /healthz endpoint, which is deliberately the
# only unauthenticated route.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
