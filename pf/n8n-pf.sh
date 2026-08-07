#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# n8n pf firewall manager
#
# Restricts inbound TCP :5678 (n8n editor, published by Colima on the Mac's LAN
# interface) so it is reachable ONLY from the jump host — plus loopback.
#
# Design (per operator requirement):
#   * Uses a pf ANCHOR referenced from the EXISTING /etc/pf.conf.
#   * NEVER replaces the active ruleset — Apple's anchors and anything a VPN
#     client loaded stay intact. We only ADD `anchor "com.example.n8n"`.
#   * Every pf.conf edit is backed up and parse-checked (`pfctl -n`) before load.
#   * Persistence is a LaunchDaemon (root, boot) that re-applies at every boot
#     and self-heals if a macOS update resets /etc/pf.conf.
#
# Requires root.  Subcommands:
#   baseline   Show current pf state (run this FIRST, before any change).
#   gonogo     Transient block-ALL on :5678 (no allowlist, no daemon).
#              Use to PROVE pf filters the Colima-forwarded port before committing.
#   install    Allowlist (jump host) + load + install boot persistence.
#   boot       Re-apply allowlist + load (invoked by the LaunchDaemon).
#   allow      Rewrite anchor to allowlist and reload (toggle helper).
#   block      Rewrite anchor to block-all and reload (toggle helper).
#   status     Show current pf state + the loaded anchor rules.
#   uninstall  Remove anchor + reference + loader + daemon; restore.
# ---------------------------------------------------------------------------
set -euo pipefail

ANCHOR="com.example.n8n"
ANCHOR_FILE="/etc/pf.anchors/${ANCHOR}"
PFCONF="/etc/pf.conf"
LOADER="/usr/local/bin/n8n-pf.sh"
DAEMON_LABEL="com.example.n8n-pf"
DAEMON_PLIST="/Library/LaunchDaemons/${DAEMON_LABEL}.plist"
PORT=5678
# Jump host LAN source IPs (verified via `ip route get 10.0.0.10` on the jump host).
JUMP_IPS=("10.0.0.20" "10.0.0.21")

need_root() { [ "$(id -u)" = "0" ] || { echo "ERROR: run with sudo" >&2; exit 1; }; }
stamp()     { date +%Y%m%d-%H%M%S; }

write_anchor() {   # $1 = allow | blockall
  local mode="$1"
  {
    echo "# Managed by n8n-pf.sh — do not edit by hand."
    echo "# Loopback always allowed so Mac-local access + healthchecks keep working."
    echo "pass in quick proto tcp from 127.0.0.1 to any port ${PORT}"
    echo "pass in quick proto tcp from ::1 to any port ${PORT}"
    if [ "$mode" = "allow" ]; then
      for ip in "${JUMP_IPS[@]}"; do
        echo "pass in quick proto tcp from ${ip} to any port ${PORT}"
      done
    fi
    echo "block drop in quick proto tcp to any port ${PORT}"
  } > "$ANCHOR_FILE"
  chmod 644 "$ANCHOR_FILE"
}

ensure_reference() {   # idempotently add anchor refs to pf.conf, preserving everything else
  if ! grep -q "anchor \"${ANCHOR}\"" "$PFCONF"; then
    cp "$PFCONF" "${PFCONF}.bak.n8n.$(stamp)"
    printf '\nanchor "%s"\nload anchor "%s" from "%s"\n' \
      "$ANCHOR" "$ANCHOR" "$ANCHOR_FILE" >> "$PFCONF"
  fi
}

remove_reference() {
  if grep -q "anchor \"${ANCHOR}\"" "$PFCONF"; then
    cp "$PFCONF" "${PFCONF}.bak.n8n.$(stamp)"
    grep -v "\"${ANCHOR}\"" "$PFCONF" > "${PFCONF}.tmp"
    mv "${PFCONF}.tmp" "$PFCONF"
  fi
}

load() {
  # Parse-check the WHOLE ruleset first; if it fails, abort WITHOUT touching running state.
  if ! pfctl -n -f "$PFCONF"; then
    echo "ERROR: pf.conf failed parse-check — running ruleset left unchanged" >&2
    exit 1
  fi
  pfctl -f "$PFCONF"
  # -E enables + bumps the enable ref-count (coexists with other pf users); fall back to -e.
  pfctl -E 2>/dev/null || pfctl -e 2>/dev/null || true
}

show() {
  echo "### pfctl -s info"
  pfctl -s info 2>/dev/null | sed -n '1,4p'
  echo "### pfctl -s Anchors"
  pfctl -s Anchors 2>/dev/null || true
  echo "### rules in anchor ${ANCHOR}"
  pfctl -a "$ANCHOR" -s rules 2>/dev/null || echo "(anchor not loaded)"
}

install_persistence() {
  # Copy this script to a stable path and install a boot LaunchDaemon.
  local self; self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  install -m 0755 "$self" "$LOADER"
  cat > "$DAEMON_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${DAEMON_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${LOADER}</string>
        <string>boot</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/var/log/n8n-pf.out.log</string>
    <key>StandardErrorPath</key><string>/var/log/n8n-pf.err.log</string>
</dict>
</plist>
PLIST
  chown root:wheel "$DAEMON_PLIST"
  chmod 644 "$DAEMON_PLIST"
  launchctl bootout system "$DAEMON_PLIST" 2>/dev/null || true
  launchctl bootstrap system "$DAEMON_PLIST"
  echo "Installed loader ${LOADER} + LaunchDaemon ${DAEMON_PLIST} (runs at every boot)."
}

case "${1:-}" in
  baseline)  need_root; echo "== pf BEFORE any change =="; show ;;
  gonogo)    need_root; write_anchor blockall; ensure_reference; load
             echo "== TRANSIENT block-all on :${PORT} loaded (no allowlist, no daemon) =="; show ;;
  install)   need_root; write_anchor allow; ensure_reference; load; install_persistence
             echo "== allowlist loaded + persistence installed =="; show ;;
  boot)      need_root; write_anchor allow; ensure_reference; load ;;   # LaunchDaemon entrypoint
  allow)     need_root; write_anchor allow; load; echo "== allowlist =="; show ;;
  block)     need_root; write_anchor blockall; load; echo "== block-all =="; show ;;
  status)    need_root; show ;;
  uninstall) need_root
             launchctl bootout system "$DAEMON_PLIST" 2>/dev/null || true
             rm -f "$DAEMON_PLIST" "$LOADER" "$ANCHOR_FILE"
             remove_reference
             pfctl -f "$PFCONF" 2>/dev/null || true
             echo "== uninstalled (pf.conf reference removed, ruleset reloaded) ==" ;;
  *) echo "usage: sudo $0 {baseline|gonogo|install|allow|block|status|uninstall}" >&2; exit 2 ;;
esac
