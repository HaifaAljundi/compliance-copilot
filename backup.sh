#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# n8n nightly backup — Postgres (pg_dump) + data volume + the encryption key.
#
# Produces ONE self-contained archive per run:
#   postgres.sql.gz   logical dump (online, no downtime)
#   n8n-data.tgz      /home/node/.n8n  (binary data, config, community nodes)
#   secrets.env       N8N_ENCRYPTION_KEY + POSTGRES_PASSWORD   <-- restore is USELESS without this
#   MANIFEST.txt      pinned image versions at backup time
#
# DEST is the single knob to change if you later mirror off-disk (jump host / R2).
# Runs unattended from a LaunchAgent, so DOCKER_HOST + PATH are set explicitly
# (a launchd job does NOT inherit your interactive shell environment).
# ---------------------------------------------------------------------------
set -euo pipefail

export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="$HOME/Sites/n8n"
DEST="$HOME/Sites/n8n-backups"      # <-- point off-disk here to add a remote copy
RETENTION_DAYS=30
DATA_VOLUME="n8n_data"
PG_CONTAINER="n8n_postgres"

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="${DEST}/n8n-${STAMP}"
ARCHIVE="${DEST}/n8n-backup-${STAMP}.tar.gz"

mkdir -p "$DEST"; chmod 700 "$DEST"    # archives contain the encryption key — keep private
mkdir -p "$WORK"

# 1. Postgres logical dump (online, consistent)
docker exec "$PG_CONTAINER" pg_dump -U n8n -d n8n --clean --if-exists \
  | gzip > "${WORK}/postgres.sql.gz"

# 2. n8n data volume (binary data + config + community nodes)
docker run --rm -v "${DATA_VOLUME}":/data:ro -v "${WORK}":/backup alpine \
  tar czf /backup/n8n-data.tgz -C /data .

# 3. Recovery secrets (encryption key + PG password)
grep -E '^N8N_ENCRYPTION_KEY=|^POSTGRES_PASSWORD=' "${PROJECT_DIR}/.env" > "${WORK}/secrets.env"
chmod 600 "${WORK}/secrets.env"

# 4. Manifest (so a restore uses matching image versions)
{
  echo "n8n_backup=${STAMP}"
  echo "n8n_image=$(docker inspect --format '{{.Config.Image}}' n8n 2>/dev/null || echo unknown)"
  echo "pg_image=$(docker inspect --format '{{.Config.Image}}' ${PG_CONTAINER} 2>/dev/null || echo unknown)"
} > "${WORK}/MANIFEST.txt"

# 5. Single archive + checksum; drop the work dir
tar czf "$ARCHIVE" -C "$DEST" "n8n-${STAMP}"
chmod 600 "$ARCHIVE"
shasum -a 256 "$ARCHIVE" > "${ARCHIVE}.sha256"
rm -rf "$WORK"

# 6. Retention
find "$DEST" -maxdepth 1 -type f -name 'n8n-backup-*.tar.gz*' -mtime +${RETENTION_DAYS} -delete

echo "OK $(date +%FT%T) ${ARCHIVE} ($(du -h "$ARCHIVE" | cut -f1))"
