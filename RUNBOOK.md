# n8n runbook — n8n.example.com

Single, self-hosted n8n for personal/internal automation. Runs as two pinned
containers (n8n + Postgres) under **Colima Docker** on the Mac. TLS is
terminated **upstream** by Caddy on the jump host; this box serves plain HTTP on
`:5678`, restricted by a `pf` rule to the jump host only.

```
Browser / 3rd-party ──HTTPS──▶ Caddy (jump host 203.0.113.10, LAN .20)
                                 │ terminates TLS (Let's Encrypt)
                                 │ /webhook*,/form* = public ; else = forward-auth-gated
                                 ▼ HTTP over LAN → 10.0.0.10:5678
                       [ pf: pass 5678 ONLY from the jump host; block all else ]
                                 ▼
                    n8n (2.31.6) ──┐
                                   ├─ Docker network  ── postgres (16.14-alpine)
                    volume n8n_data┘                     volume n8n_pgdata
                          │  ▲
              http://agent:8000  http://n8n:5678/webhook/*
                          ▼  │
                    agent (LangGraph compliance assistant)  ── pgvector (pg16)
                    volumes n8n_agent_{data,corpus}            volume n8n_pgvector_data
```

**AI/RAG services** (added 2026-08-07 — see `agent/README.md`)
- `pgvector` — vector store, shared by n8n's PGVector node and the agent. Publishes
  **127.0.0.1:5433 only** (loopback, unlike n8n's LAN-visible :5678, so no pf rule needed).
- `agent` — LangGraph supervisor/worker graph over the compliance corpus. **No published
  port**; n8n calls it as `http://agent:8000`, it calls n8n as `http://n8n:5678`. Both
  directions stay on the compose network — nothing new is exposed publicly.
- Secrets live in `agent/.env` (separate from this directory's `.env`, so the agent never
  reads `N8N_ENCRYPTION_KEY`). `PGVECTOR_PASSWORD` must match in both files.
- Embeddings run on the Mac's Ollama (`nomic-embed-text`, 768-dim, **fixed at index
  time** — changing the model means re-ingesting the whole corpus).
- `n8n_agent_data` / `n8n_agent_corpus` are **managed, not external**: checkpoints are
  in-flight state and the corpus is re-downloadable, so `down -v` reclaiming them is
  correct. The three volumes above hold irreplaceable data and stay external.

**Key facts**
- Project dir: `~/Sites/n8n` · Backups: `~/Sites/n8n-backups` (internal disk)
- Images PINNED: `docker.n8n.io/n8nio/n8n:2.31.6`, `postgres:16.14-alpine`
- Data volumes are **external** (`n8n_data`, `n8n_pgdata`) → `down -v` can't delete them
- Secrets in `~/Sites/n8n/.env` (chmod 600, gitignored): `N8N_ENCRYPTION_KEY`, `POSTGRES_PASSWORD`
- All docker/compose commands need `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock`
  (already exported by the scripts; export it in your shell for manual commands)

---

## 1. First-time bring-up

```bash
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
cd ~/Sites/n8n

# .env must exist with a generated key (do this ONCE; never regenerate the key):
#   umask 177; printf 'N8N_ENCRYPTION_KEY=%s\nPOSTGRES_PASSWORD=%s\n' \
#     "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" > .env

docker volume create n8n_data
docker volume create n8n_pgdata
docker-compose up -d
docker-compose ps          # both should be healthy within ~40s
```

Then do sections **2 (firewall)**, **3 (Caddy)**, **4 (post-install)**.

---

## 2. Firewall (`pf`) — restrict :5678 to the jump host

Colima publishes `:5678` on the Mac's LAN interface, so without this rule the
editor is reachable by anything on the LAN. `pf/n8n-pf.sh` locks it to the jump
host using a pf **anchor** referenced from `/etc/pf.conf` (it never replaces the
active ruleset). Needs sudo.

```bash
# 2a. Baseline — capture the current pf state (before/after evidence)
sudo bash ~/Sites/n8n/pf/n8n-pf.sh baseline

# 2b. GO/NO-GO — transient block-ALL, then test filtering from the jump host.
#     From the jump host, this MUST now fail/timeout:
sudo bash ~/Sites/n8n/pf/n8n-pf.sh gonogo
#     ssh jumphost 'curl -m6 -sS -o /dev/null -w "%{http_code}\n" http://10.0.0.10:5678'
#     -> connection refused / timeout  == pf filters the Colima port. Proceed.
#     -> 200                            == pf does NOT filter it. STOP. Switch to the
#                                          127.0.0.1-bind + reverse-SSH-tunnel design.

# 2c. Install the real allowlist + boot persistence (LaunchDaemon re-applies at boot)
sudo bash ~/Sites/n8n/pf/n8n-pf.sh install
#     From the jump host this MUST now succeed (200). From any other LAN host it must fail.

sudo bash ~/Sites/n8n/pf/n8n-pf.sh status     # show loaded anchor rules
```

Jump-host source IPs allowed: `10.0.0.20`, `10.0.0.21` (edit `JUMP_IPS`
in the script if the jump host's LAN IP changes).

**Uninstall / rollback:** `sudo bash ~/Sites/n8n/pf/n8n-pf.sh uninstall`
(removes the anchor + the `/etc/pf.conf` reference + the LaunchDaemon, reloads pf).

**macOS updates** can reset `/etc/pf.conf`. The boot LaunchDaemon
(`/Library/LaunchDaemons/com.example.n8n-pf.plist`) re-adds the anchor reference and
reloads at every boot, so the rule self-heals. Check `/var/log/n8n-pf.err.log`.

---

## 3. Reverse proxy (Caddy on the jump host)

Apply `caddy/n8n.Caddyfile` on the jump host (you manage the Linux side):

```bash
# on the jump host, in your Caddy config dir:
cp Caddyfile Caddyfile.bak.pre-n8n-$(date +%Y%m%d-%H%M%S)
# append the n8n block from caddy/n8n.Caddyfile, then:
docker exec caddy-proxy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy-proxy caddy reload   --config /etc/caddy/Caddyfile
```

- TLS auto-issues on first HTTPS hit (Let's Encrypt). DNS resolves via the
  `*.example.com` wildcard — no per-host record.
- **Fill in the authelia portal domain** in the gated `handle {}` block (or replace
  it with your existing `import authelia` snippet). Public paths `/webhook*` and
  `/form*` are intentionally un-gated.
- The block sets its OWN 3600s timeouts (not the shared 300s `proxy-transport`
  snippet) for long executions.

---

## 4. Post-install (in the n8n UI, first login)

1. **Create the owner account** at `https://n8n.example.com` (strong password).
2. **Enable 2FA/MFA**: Settings → *(user menu)* → Two-factor authentication.
3. **Register the free Community edition licence**: Settings → *Usage and plan* →
   enter your email to get a free licence key (unlocks free community features;
   this is NOT enterprise `.ee`).
4. **Webhook hardening**: on each Webhook node that faces the internet, set
   *Authentication → Header Auth* (a shared secret header) so only callers that
   know it can trigger the workflow. This is the primary abuse control until Caddy
   rate limiting is available (section "Rate limiting").

---

## 5. Health checks

```bash
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
docker-compose ps                              # both healthy
curl -I http://localhost:5678                  # 200 from the Mac
curl -s  http://localhost:5678/healthz         # {"status":"ok"}
docker logs n8n --tail 30                       # look for errors
# externally:
curl -I https://n8n.example.com                 # 200/302 with valid cert
```

---

## 6. Deliberate upgrade

Images are pinned so nothing changes under you. To upgrade on purpose:

```bash
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
cd ~/Sites/n8n
./backup.sh                                    # ALWAYS back up first
# edit docker-compose.yml: bump the n8n tag to the new version (check
#   https://github.com/n8n-io/n8n/releases for breaking changes first)
docker-compose pull n8n
docker-compose up -d n8n
docker logs -f n8n                              # watch migrations; check for deprecations
```
Rollback: put the old tag back and `docker-compose up -d n8n`. Postgres data is
untouched by an n8n version change (schema migrations are forward-compatible
within a major; a MAJOR n8n bump may migrate the DB — the backup is your safety net).

**Postgres upgrades**: a major PG bump (16→17) needs a dump/restore, not just a tag
change. Procedure: `./backup.sh`, stop the stack, `docker volume rm n8n_pgdata`,
change the tag, `docker volume create n8n_pgdata`, `docker-compose up -d postgres`,
then load the dump (`gzip -dc <backup>/postgres.sql.gz | docker exec -i n8n_postgres psql -U n8n -d n8n`).

---

## 7. Backups & restore

- **Automatic**: LaunchAgent `com.example.n8n-backup` runs `backup.sh` at **03:00**
  Asia/Dubai. Output: `~/Sites/n8n-backups/n8n-backup-<stamp>.tar.gz` (+ `.sha256`).
  Retention 30 days. Each archive contains the pg dump, the data volume, **and the
  encryption key** (`secrets.env`) — so it is fully self-sufficient for restore.
  Logs: `~/Library/Logs/n8n/backup.{out,err}.log`.
- **Verify a backup** (safe, non-destructive — spins up a throwaway and tears it down):
  ```bash
  ./restore.sh ~/Sites/n8n-backups/n8n-backup-<stamp>.tar.gz
  ```
- **Real disaster recovery into the LIVE stack** (destructive — overwrites live data):
  ```bash
  export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
  cd ~/Sites/n8n
  A=~/Sites/n8n-backups/n8n-backup-<stamp>.tar.gz
  tar xzf "$A" -C /tmp && D=$(ls -d /tmp/n8n-*)
  # 1. ensure .env has the SAME key as the backup (compare $D/secrets.env) — restore is
  #    useless with a different key. If lost, copy secrets.env values into .env.
  docker-compose down
  docker volume rm n8n_data n8n_pgdata && docker volume create n8n_data && docker volume create n8n_pgdata
  docker-compose up -d postgres        # wait healthy
  gzip -dc "$D/postgres.sql.gz" | docker exec -i n8n_postgres psql -U n8n -d n8n
  docker run --rm -i -v n8n_data:/data alpine tar xzf - -C /data < "$D/n8n-data.tgz"
  docker-compose up -d n8n
  ```

> **The encryption key is the single point of unrecoverability.** If it changes,
> every saved credential is permanently unreadable. It lives in `.env` and in every
> backup archive. Never log it, never commit it. `.env` and `n8n-backups/` are gitignored.

---

## 8. Reboot / power resilience

Already configured on this Mac (verify, don't assume):
- `restart: always` on both containers → Docker starts them when the daemon starts.
- Colima starts via the **`com.example.colima`** LaunchAgent (RunAtLoad) + a watchdog.
- **Auto-login is ON** (`sysadminctl -autologin status` → the admin user). This is *load-bearing*:
  the Colima backend is **`vz`** (Apple Virtualization.framework), which cannot start
  before a GUI session exists — so a pre-login boot daemon is impossible and auto-login
  is what brings Colima (hence n8n) back after an unattended reboot.
- Sleep disabled, `autorestart` (restart after power loss) ON.

**Caveat:** if the admin account password changes, RE-ENABLE auto-login
(`sysadminctl -autologin set ...`) or the Mac stalls at the login screen after a
reboot and nothing comes up.

To prove recovery definitively: `colima stop && colima start` (NOTE: this bounces
**all** containerised client apps for ~1-2 min), then confirm `docker-compose ps`
shows n8n healthy with no manual login.

---

## 9. Common failures & fixes

| Symptom | Cause | Fix |
|---|---|---|
| Editor loads then hangs; no useful logs | push/websocket not proxied | Caddy upgrades websockets automatically — confirm you used *this* site block (it has `flush_interval -1`), not a plain proxy. |
| Login bounces back to sign-in over HTTPS | n8n didn't see `X-Forwarded-Proto: https` | `N8N_PROXY_HOPS=1` must be set (it is). If you add another proxy in front, bump it. |
| Webhook URL shows `http://localhost:5678/...` | `N8N_WEBHOOK_URL`/`N8N_EDITOR_BASE_URL` missing | Both are set to `https://n8n.example.com/` in compose. |
| Long AI workflow cut off mid-run | proxy read/write timeout too low | This block uses 3600s (not the shared 300s). |
| Credentials all "could not be decrypted" | `N8N_ENCRYPTION_KEY` changed | Restore the original key from a backup's `secrets.env`. |
| Jump host can't reach n8n after a macOS update | pf.conf reset OR IP changed | Boot LaunchDaemon re-applies; if the jump host IP changed, edit `JUMP_IPS` and re-run `sudo n8n-pf.sh install`. |
| Everything down after a reboot, no site up | auto-login broke (e.g., password change) | Log into the desktop once; re-enable auto-login. |
| `Python task runner ... Python 3 is missing` | image has no Python | Harmless unless you use **Python** Code nodes. JS Code nodes work. For Python, run task runners in external mode (separate `n8nio/runners` image). |
| Deprecation warnings on startup | future default changes | Harmless while the image is pinned. On upgrade, set `N8N_UNVERIFIED_PACKAGES_ENABLED`, `N8N_COMPRESSION_NODE_MAX_*` explicitly if you rely on current limits. `N8N_RUNNERS_TASK_TIMEOUT=300` is already pinned. |

---

## 10. Rate limiting (optional hardening)

Native Caddy has no rate limiter and the jump-host Caddy build lacks the module
(`caddy list-modules | grep rate` → none). To enable the commented `rate_limit`
block in `caddy/n8n.Caddyfile`, rebuild the jump-host Caddy:
```bash
xcaddy build --with github.com/mholt/caddy-ratelimit   # produces a caddy binary/image
```
…then swap it into `caddy-proxy` (affects all sites — do it in a window) and
uncomment the block. Until then, use **Header Auth** on webhook nodes (section 4).

---

## 11. Multi-year fragility notes

- The Colima VM is shared with other production workloads. `mem_limit` caps n8n at 3 GiB
  and Postgres at 1 GiB so a runaway hits its own cap (and `restart: always` revives
  it) instead of OOM-killing a co-tenant app.
- Backups are on the **same internal disk** as the data (operator decision). One disk
  failure loses both. To add an off-disk copy, point `DEST` in `backup.sh` at a jump-host
  path (rsync) or R2 — it's a one-line change.
- Disable *automatic install* of macOS updates (they force mid-day reboots here and can
  reset `pf.conf`).
- pf takes over the active ruleset via an anchor added to `/etc/pf.conf`; if you later
  run a VPN client that manages pf, verify both coexist (`sudo pfctl -s Anchors`).
