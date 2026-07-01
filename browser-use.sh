#!/usr/bin/env bash
# browser-use skill entrypoint
#
# Three modes of operation:
#   1. LOCAL (default): Python + Puppeteer run on this machine
#   2. REMOTE: SSH into a remote machine that has the model + Chrome
#
# Configure with environment variables:
#   BROWSER_USE_HOST   - remote host IP (e.g. 192.168.40.167). Empty = local.
#   BROWSER_USE_USER   - SSH user for remote (default: current user)
#   BROWSER_USE_PYTHON - Python path (default: python3, or ~/.venv-vllm-mlx/bin/python3 for remote)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Remote mode ────────────────────────────────────────────────────────────
REMOTE_HOST="${BROWSER_USE_HOST:-}"

if [ -n "$REMOTE_HOST" ]; then
  REMOTE_USER="${BROWSER_USE_USER:-$(whoami)}"
  REMOTE_PYTHON="${BROWSER_USE_PYTHON:-~/.venv-vllm-mlx/bin/python3}"

  # Try to find the skill on the remote (common locations)
  REMOTE_SCRIPT="${BROWSER_USE_REMOTE_SCRIPT:-}"
  if [ -z "$REMOTE_SCRIPT" ]; then
    for candidate in \
      "~/.pave/skills/browser-use/browser_agent.py" \
      "~/pave-apps/openpave-browser/browser_agent.py"; do
      if ssh -o ConnectTimeout=3 -o BatchMode=yes "${REMOTE_USER}@${REMOTE_HOST}" \
           "test -f ${candidate}" 2>/dev/null; then
        REMOTE_SCRIPT="$candidate"
        break
      fi
    done
  fi

  if [ -z "$REMOTE_SCRIPT" ]; then
    echo "ERROR: Cannot find browser_agent.py on ${REMOTE_HOST}." >&2
    echo "  Set BROWSER_USE_REMOTE_SCRIPT to the full path." >&2
    exit 1
  fi

  SSH_CMD="ssh -o ConnectTimeout=5 -o BatchMode=yes ${REMOTE_USER}@${REMOTE_HOST}"
  SCP_CMD="scp -o ConnectTimeout=5 -o BatchMode=yes"

  # Check connectivity
  if ! $SSH_CMD "echo ok" >/dev/null 2>&1; then
    echo "ERROR: Cannot reach ${REMOTE_HOST}. Is it online?" >&2
    exit 1
  fi

  # Special handling for screenshot: run remote, then copy PNG back
  if [ "${1:-}" = "screenshot" ]; then
    REMOTE_TMP="/tmp/browser-use-screenshot-$(date +%s).png"
    OUTPUT=$($SSH_CMD "${REMOTE_PYTHON} ${REMOTE_SCRIPT} screenshot --output ${REMOTE_TMP}" 2>/dev/null)
    LOCAL_TMP="/tmp/browser-use-screenshot.png"
    $SCP_CMD "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_TMP}" "${LOCAL_TMP}" 2>/dev/null
    echo "$OUTPUT" | sed "s|${REMOTE_TMP}|${LOCAL_TMP}|"
    exit 0
  fi

  # All other commands: forward directly (properly quoted)
  QUOTED_ARGS=""
  for arg in "$@"; do
    QUOTED_ARGS="$QUOTED_ARGS $(printf '%q' "$arg")"
  done
  exec $SSH_CMD "${REMOTE_PYTHON} ${REMOTE_SCRIPT} ${QUOTED_ARGS}"
fi

# ── Local mode ─────────────────────────────────────────────────────────────

PYTHON="${BROWSER_USE_PYTHON:-python3}"

# Ensure puppeteer_bridge daemon is running (start if not)
ensure_bridge() {
  if [ ! -S /tmp/puppeteer-bridge.sock ] || [ ! -f /tmp/puppeteer-bridge-ready ]; then
    if [ -f "${SCRIPT_DIR}/node/puppeteer_bridge.js" ]; then
      # Requires puppeteer-core (install if missing)
      if ! node -e "require('puppeteer-core')" 2>/dev/null; then
        if [ ! -d "${SCRIPT_DIR}/node_modules/puppeteer-core" ]; then
          echo "Installing puppeteer-core..." >&2
          (cd "${SCRIPT_DIR}" && npm install puppeteer-core 2>/dev/null)
        fi
      fi
      echo "Starting Puppeteer bridge..." >&2
      NODE_PATH="${SCRIPT_DIR}/node_modules:${NODE_PATH:-}" \
        node "${SCRIPT_DIR}/node/puppeteer_bridge.js" &
      # Wait for bridge to be ready
      for i in $(seq 1 10); do
        if [ -f /tmp/puppeteer-bridge-ready ]; then break; fi
        sleep 0.5
      done
    fi
  fi
}

# Only start bridge for commands that need it
case "${1:-}" in
  screenshot|dom|navigate|dom_type|dom_click|key|scroll|eval|url|check)
    ensure_bridge
    ;;
esac

exec "$PYTHON" "${SCRIPT_DIR}/browser_agent.py" "$@"
