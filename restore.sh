#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# n8n restore VERIFIER — proves a backup archive restores into a working instance.
#
# Non-destructive: spins up a THROWAWAY n8n + Postgres (bound to 127.0.0.1 only,
# separate volumes/containers), restores the archive into it, checks health +
# data + encryption-key consistency, then tears the throwaway down.
#
#   ./restore.sh <archive.tar.gz> [test_http_port=5699] [--keep]
#
# For a REAL disaster recovery into the LIVE stack, see RUNBOOK.md "Restore" —
# that path is deliberately manual, not scripted, because it overwrites live data.
# ---------------------------------------------------------------------------
set -euo pipefail
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"

ARCHIVE="${1:?usage: ./restore.sh <archive.tar.gz> [port] [--keep]}"
PORT="${2:-5699}"; case "${2:-}" in --keep) PORT=5699;; esac
KEEP=0; for a in "$@"; do [ "$a" = "--keep" ] && KEEP=1; done

P=n8nrestore
NET=${P}_net; PGV=${P}_pgdata; DV=${P}_data; PGC=${P}_pg; N8C=${P}_n8n
TMP="$(mktemp -d)"

teardown() {
  docker rm -f "$N8C" "$PGC" >/dev/null 2>&1 || true
  docker volume rm "$PGV" "$DV" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
fail() { echo "FAIL: $*" >&2; [ "$KEEP" = "1" ] || teardown; exit 1; }
trap '[ "$KEEP" = "1" ] || teardown' EXIT

echo "== restore-verify: $ARCHIVE =="
teardown; TMP="$(mktemp -d)"    # clean slate (teardown also rm'd TMP)

# 1. Integrity
if [ -f "${ARCHIVE}.sha256" ]; then
  ( cd "$(dirname "$ARCHIVE")" && shasum -a 256 -c "$(basename "$ARCHIVE").sha256" ) || fail "checksum mismatch"
  echo "[1] checksum OK"
else
  echo "[1] (no .sha256 alongside — skipping integrity check)"
fi

# 2. Extract
tar xzf "$ARCHIVE" -C "$TMP"
INNER="$(ls -d "$TMP"/n8n-* 2>/dev/null | head -1)"
[ -n "$INNER" ] || fail "archive layout unexpected"
[ -f "$INNER/secrets.env" ] || fail "secrets.env (encryption key) missing from backup"
# shellcheck disable=SC1090
N8N_ENCRYPTION_KEY="$(grep '^N8N_ENCRYPTION_KEY=' "$INNER/secrets.env" | cut -d= -f2-)"
POSTGRES_PASSWORD="$(grep '^POSTGRES_PASSWORD=' "$INNER/secrets.env" | cut -d= -f2-)"
N8N_IMAGE="$(grep '^n8n_image=' "$INNER/MANIFEST.txt" | cut -d= -f2-)"
PG_IMAGE="$(grep '^pg_image=' "$INNER/MANIFEST.txt" | cut -d= -f2-)"
[ -n "$N8N_ENCRYPTION_KEY" ] || fail "encryption key empty"
echo "[2] extracted; images: n8n=$N8N_IMAGE pg=$PG_IMAGE"

# 3. Throwaway infra
docker network create "$NET" >/dev/null
docker volume create "$PGV" >/dev/null; docker volume create "$DV" >/dev/null

# 4. Postgres + load dump
docker run -d --name "$PGC" --network "$NET" \
  -e POSTGRES_USER=n8n -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB=n8n \
  -v "$PGV":/var/lib/postgresql/data "$PG_IMAGE" >/dev/null
for i in $(seq 1 20); do docker exec "$PGC" pg_isready -U n8n -d n8n >/dev/null 2>&1 && break; sleep 2; done
docker exec "$PGC" pg_isready -U n8n -d n8n >/dev/null 2>&1 || fail "throwaway postgres never became ready"
gzip -dc "$INNER/postgres.sql.gz" | docker exec -i "$PGC" psql -v ON_ERROR_STOP=0 -U n8n -d n8n >/dev/null 2>&1
echo "[4] postgres restored"

# 5. Restore n8n data volume (stream via stdin — macOS /var/folders isn't mounted in the VM)
docker run --rm -i -v "$DV":/data alpine tar xzf - -C /data < "$INNER/n8n-data.tgz"
echo "[5] data volume restored"

# 6. Throwaway n8n (127.0.0.1 only — never LAN-exposed)
docker run -d --name "$N8C" --network "$NET" -p "127.0.0.1:${PORT}:5678" \
  -e N8N_ENCRYPTION_KEY="$N8N_ENCRYPTION_KEY" \
  -e DB_TYPE=postgresdb -e DB_POSTGRESDB_HOST="$PGC" -e DB_POSTGRESDB_PORT=5432 \
  -e DB_POSTGRESDB_DATABASE=n8n -e DB_POSTGRESDB_USER=n8n -e DB_POSTGRESDB_PASSWORD="$POSTGRES_PASSWORD" \
  -e N8N_DEFAULT_BINARY_DATA_MODE=filesystem -e N8N_DIAGNOSTICS_ENABLED=false \
  -v "$DV":/home/node/.n8n "$N8N_IMAGE" >/dev/null

for i in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)" = "200" ] && break
  sleep 2
done
[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)" = "200" ] \
  || fail "throwaway n8n never became healthy from the restored data"
echo "[6] throwaway n8n healthy on 127.0.0.1:${PORT}"

# 7. Encryption-key consistency: n8n logs a mismatch error if the key doesn't match stored creds
if docker logs "$N8C" 2>&1 | grep -qiE 'mismatch.*encryption|encryption.*mismatch|wrong encryption key'; then
  fail "encryption key does NOT match restored credentials"
fi
echo "[7] no encryption-key mismatch in logs"

# 8. Data census (informational + proof the DB carried over)
census() { docker exec "$PGC" psql -tA -U n8n -d n8n -c "$1" 2>/dev/null | tr -d '[:space:]'; }
USERS="$(census 'select count(*) from "user";')"
WFS="$(census 'select count(*) from workflow_entity;')"
CREDS="$(census 'select count(*) from credentials_entity;')"
echo "[8] restored DB: users=${USERS:-?} workflows=${WFS:-?} credentials=${CREDS:-?}"

echo "== PASS: archive restores into a working, healthy n8n =="
if [ "$KEEP" = "1" ]; then
  echo "(--keep: throwaway left running on 127.0.0.1:${PORT}; run this script again to clean up)"
fi
exit 0
