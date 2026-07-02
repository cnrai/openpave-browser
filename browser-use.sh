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

  SSH_OPTS="-o ConnectTimeout=5 -o BatchMode=yes"

  # Forward LOCATE_ANYTHING_* env vars to remote (for API mode)
  ENV_FWD=""
  for var in $(env | grep -o '^LOCATE_ANYTHING_[A-Z_]*' | sort -u 2>/dev/null || true); do
    val=$(printenv "$var" 2>/dev/null || true)
    if [ -n "$val" ]; then
      ENV_FWD="${ENV_FWD} ${var}=$(printf '%q' "$val")"
    fi
  done

  REMOTE_PREFIX="${REMOTE_PYTHON} ${REMOTE_SCRIPT}"
  if [ -n "$ENV_FWD" ]; then
    REMOTE_PREFIX="env${ENV_FWD} ${REMOTE_PREFIX}"
  fi

  # Check connectivity
  if ! ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "echo ok" >/dev/null 2>&1; then
    echo "ERROR: Cannot reach ${REMOTE_HOST}. Is it online?" >&2
    exit 1
  fi

  # Special handling for screenshot: run remote, then copy PNG back
  if [ "${1:-}" = "screenshot" ]; then
    REMOTE_TMP="/tmp/browser-use-screenshot-$(date +%s).png"
    OUTPUT=$(ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "${REMOTE_PREFIX} screenshot --output ${REMOTE_TMP}" 2>/dev/null)
    LOCAL_TMP="/tmp/browser-use-screenshot.png"
    scp $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_TMP}" "${LOCAL_TMP}" 2>/dev/null
    echo "$OUTPUT" | sed "s|${REMOTE_TMP}|${LOCAL_TMP}|"
    exit 0
  fi

  # Check if --ocr flag is present (auto-screenshot after action)
  HAS_OCR=false
  for arg in "$@"; do
    [ "$arg" = "--ocr" ] && HAS_OCR=true
  done

  # All other commands: forward directly (properly quoted)
  QUOTED_ARGS=""
  for arg in "$@"; do
    QUOTED_ARGS="$QUOTED_ARGS $(printf '%q' "$arg")"
  done

  if [ "$HAS_OCR" = true ]; then
    # Run command, then SCP screenshot back, patch path in JSON
    REMOTE_SCR="/tmp/browser-use-screenshot.png"
    OUTPUT=$(ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "${REMOTE_PREFIX} ${QUOTED_ARGS}" 2>/dev/null) || true
    LOCAL_SCR="/tmp/browser-use-screenshot.png"
    scp $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_SCR}" "${LOCAL_SCR}" 2>/dev/null || true
    echo "$OUTPUT" | sed "s|${REMOTE_SCR}|${LOCAL_SCR}|g"
    exit 0
  else
    exec ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "${REMOTE_PREFIX} ${QUOTED_ARGS}"
  fi
fi

# ── Local mode ─────────────────────────────────────────────────────────────

PYTHON="${BROWSER_USE_PYTHON:-python3}"

# Ensure puppeteer_bridge daemon is running (start if not)
ensure_bridge() {
  if [ ! -S /tmp/puppeteer-bridge.sock ] || [ ! -f /tmp/puppeteer-bridge-ready ]; then
    if [ -f "${SCRIPT_DIR}/node/puppeteer_bridge.js" ]; then
      # Install puppeteer if not present (downloads bundled Chromium ~280MB on first run)
      if [ ! -d "${SCRIPT_DIR}/node_modules/puppeteer" ]; then
        echo "Installing puppeteer (first run, downloads Chromium ~280MB)..." >&2
        (cd "${SCRIPT_DIR}" && npm install puppeteer 2>/dev/null)
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
  screenshot|dom|navigate|dom_type|dom_click|click|type|find|key|scroll|eval|url|focus|check)
    ensure_bridge
    ;;
esac

exec "$PYTHON" "${SCRIPT_DIR}/browser_agent.py" "$@"
