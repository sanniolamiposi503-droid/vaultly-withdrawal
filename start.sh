#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start.sh — run the backend + a public Cloudflare quick tunnel, and KEEP BOTH
# ALIVE. Either process dying is detected and restarted automatically, so the
# public link stops silently going dead.
#
#   ./start.sh              # supervise backend + tunnel (foreground)
#   ./start.sh --once       # start both, print the URL, exit (no supervision)
#   ./start.sh --server     # backend only (no tunnel)
#
# The current public URL is always written to  logs/public_url.txt
# Logs: logs/server.log, logs/cloudflared.log, logs/watchdog.log
#
# NOTE: a trycloudflare quick tunnel is *ephemeral by design* — the hostname is
# random and is released when the tunnel process stops. For a stable URL, deploy
# the Dockerfile / render.yaml (see README). This script keeps the temporary
# link alive; it cannot make it permanent.
# ---------------------------------------------------------------------------
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR" || exit 1

PORT="${PORT:-8000}"
export PORT
export DB_PATH="${DB_PATH:-$APP_DIR/data/app.db}"
export PYTHONUNBUFFERED=1

LOCAL="http://127.0.0.1:${PORT}"
mkdir -p data logs

SERVER_LOG="$APP_DIR/logs/server.log"
TUNNEL_LOG="$APP_DIR/logs/cloudflared.log"
WATCH_LOG="$APP_DIR/logs/watchdog.log"
URL_FILE="$APP_DIR/logs/public_url.txt"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$WATCH_LOG"; }

backend_up() { curl -fsS --max-time 5 "$LOCAL/api/health" >/dev/null 2>&1; }

start_backend() {
  log "starting backend on ${LOCAL} (db=${DB_PATH})"
  pkill -f "python3 .*server\.py" >/dev/null 2>&1
  sleep 1
  nohup python3 "$APP_DIR/server.py" >>"$SERVER_LOG" 2>&1 &
  for _ in $(seq 1 30); do
    sleep 0.5
    if backend_up; then log "backend is up"; return 0; fi
  done
  log "ERROR: backend did not come up — see $SERVER_LOG"
  return 1
}

ensure_backend() { backend_up || start_backend; }

CURRENT_URL=""
if [ -s "$URL_FILE" ]; then CURRENT_URL="$(cat "$URL_FILE")"; fi

start_tunnel() {
  log "creating a new cloudflare quick tunnel -> ${LOCAL}"
  pkill -f "cloudflared tunnel" >/dev/null 2>&1
  sleep 1
  : >"$TUNNEL_LOG"
  nohup cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 --url "$LOCAL" >>"$TUNNEL_LOG" 2>&1 &

  for _ in $(seq 1 60); do
    sleep 1
    CURRENT_URL="$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)"
    [ -n "$CURRENT_URL" ] && break
  done

  if [ -z "$CURRENT_URL" ]; then
    log "ERROR: no tunnel hostname appeared — see $TUNNEL_LOG"
    return 1
  fi

  # wait until the edge actually routes to us
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 10 "$CURRENT_URL/api/health" >/dev/null 2>&1; then
      echo "$CURRENT_URL" >"$URL_FILE"
      log "PUBLIC URL READY: $CURRENT_URL"
      return 0
    fi
    sleep 2
  done
  log "WARN: tunnel hostname $CURRENT_URL is not answering yet"
  echo "$CURRENT_URL" >"$URL_FILE"
  return 1
}

ensure_tunnel() {
  if [ -n "$CURRENT_URL" ] && curl -fsS --max-time 12 "$CURRENT_URL/api/health" >/dev/null 2>&1; then
    return 0
  fi
  start_tunnel
}

# ---- no-supervision modes -------------------------------------------------
case "${1:-}" in
  --server)
    ensure_backend && log "backend-only mode; serving $LOCAL" && exit 0
    exit 1
    ;;
  --once)
    ensure_backend || exit 1
    start_tunnel || exit 1
    exit 0
    ;;
esac

# ---- supervised mode ------------------------------------------------------
log "watchdog started (pid $$) — Ctrl-C to stop"
ensure_backend
ensure_tunnel

while true; do
  sleep 15
  if ! backend_up; then
    log "backend heartbeat failed — restarting"
    start_backend
  fi
  if ! curl -fsS --max-time 12 "$CURRENT_URL/api/health" >/dev/null 2>&1; then
    log "tunnel heartbeat failed — recreating tunnel"
    start_tunnel || true
  fi
done
