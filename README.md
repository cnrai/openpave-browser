# openpave-browser

Browser automation skill for OpenPAVE — control a web browser through Puppeteer (precise selector-based control) and pyautogui (coordinate-based fallback). Includes OCR-on-by-default so every command returns the page's visible text.

## Quick Start

```bash
pave install cnrai/openpave-browser
```

Then verify your setup:

```bash
browser-use check
```

Fix anything it says is missing, then:

```bash
browser-use navigate "https://example.com"
browser-use url        # returns URL, title, and OCR text
```

## Prerequisites

### 1. Node.js + Puppeteer (required)

The skill uses Puppeteer with **bundled Chromium** — no need to install or configure Chrome yourself.

```bash
# Node.js 16+ required
node --version

# Install puppeteer (downloads Chromium ~280MB on first run)
cd ~/.pave/skills/browser-use/node && npm install
```

The browser bridge auto-detects at startup:
- **If Chrome is running** with `--remote-debugging-port=9222` → attaches to it (shares your cookies/logins)
- **If not** → launches bundled Chromium with a persistent profile at `~/.pave/browser-profile` (visible window, you can interact with it)

To use your own Chrome:
```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

For headless mode (servers, CI):
```bash
export PUPPETEER_HEADLESS=1
```

### 2. Python Dependencies (required)

```bash
pip3 install -r requirements.txt
```

This installs `mss`, `pyautogui`, `Pillow`, and `requests`.

### 3. OCR Engine (required, on by default)

OCR is **on by default** — every command returns the page's visible text. You need:

```bash
pip3 install rapidocr-onnxruntime
```

To disable OCR for a single command (faster response):

```bash
browser-use navigate "https://example.com" --no-ocr
```

### 4. LocateAnything-3B (optional)

Only needed for the `find` command (visual grounding by natural-language description).

**Option A: Hosted API (recommended, no download)**

If you're logged into OpenPAVE (`pave login`), the API URL and JWT are auto-configured. The `find` command sends screenshots to the hosted LocateAnything service via your PAVE account. No additional setup needed.

The JWT is read fresh from `~/.pave/membership-credentials.json` on each call, so token rotation is handled automatically.

To verify:
```bash
browser-use check
# Should show LocateAnything-3B: status=ok, mode=api
```

**Option B: Local model**

```bash
pip3 install mlx-vlm
huggingface-cli download nvidia/LocateAnything-3B
```

All other 14 commands work without LocateAnything.

## Architecture

```
PAVE Agent (the brain)
  │
  │  calls browser-use.sh
  ▼
browser-use.sh ── routes to ──┐
                              │
                   ┌──────────┴──────────┐
                   │                     │
            Local mode            Remote mode (SSH)
            (default)             Set BROWSER_USE_HOST
                   │                     │
                   ▼                     ▼
          browser_agent.py      SSH → remote browser_agent.py
                   │
          ┌────────┴────────┐
          │                 │
  Puppeteer bridge    pyautogui + mss
  (selector-based)    (coordinate-based)
  via Unix socket     (fallback / screenshots)
          │
  ┌───────┴───────┐
  │               │
  Attach mode     Launch mode
  (Chrome :9222)  (bundled Chromium)
```

### Three Layers of Control

1. **Puppeteer bridge** — Precise DOM interactions via CSS selectors (`dom_click`, `dom_type`, `dom`, `eval`). Also handles `click`/`type` at viewport coordinates.
2. **pyautogui** — OS-level mouse/keyboard fallback for coordinate-based actions (`key`, `scroll`)
3. **LocateAnything-3B** — Visual grounding to find elements by description (`find`)

### Browser Modes

| Mode | When | Behavior |
|------|------|----------|
| **Attach** | Chrome running on :9222 | Connects to your Chrome, shares cookies/logins |
| **Launch** | No Chrome on :9222 | Starts bundled Chromium, visible window, persistent profile |
| **Headless** | `PUPPETEER_HEADLESS=1` | Same as Launch but no visible window |

## Modes

### Local Mode (default)

Runs entirely on the current machine. Requires Node.js 16+ and Python 3.9+.

### Remote Mode

For deployments where the model/GPU or Chrome is on a different machine. Set these in `~/.pave/tokens.yaml`:

```yaml
BROWSER_USE_HOST: "192.168.1.100"
BROWSER_USE_USER: "myuser"
BROWSER_USE_PYTHON: "/path/to/python3"
```

All commands are forwarded over SSH. Screenshots are SCP'd back automatically.

**Remote prerequisites:**
- Passwordless SSH access (`ssh-copy-id user@host`)
- Python deps installed on the remote
- `browser_agent.py` on the remote (auto-detected at `~/.pave/skills/browser-use/`)

## Commands

| Command | Description |
|---------|-------------|
| `screenshot` | Capture page screenshot |
| `dom [--text-only]` | Extract interactive DOM elements with CSS selectors |
| `navigate <url>` | Navigate to URL |
| `dom_type <selector> <text>` | Type text into element by CSS selector |
| `dom_click <selector>` | Click element by CSS selector |
| `click <x> <y>` | Click at viewport coordinates |
| `type <x> <y> <text>` | Type at viewport coordinates |
| `find <description>` | Locate element via visual grounding |
| `key <combo>` | Press key combo (e.g. `enter`, `ctrl+a`) |
| `scroll <up\|down>` | Scroll page |
| `eval <code>` | Execute JavaScript in page |
| `url` | Get current page URL and title |
| `focus` | Focus browser window |
| `wait <seconds>` | Sleep |
| `check` | Verify all components are ready |

All action commands accept `--no-ocr` to skip OCR (faster response).

## Environment Variables

All optional:

| Variable | Description | Default |
|----------|-------------|---------|
| `BROWSER_USE_HOST` | Remote host IP (enables SSH mode) | _(empty = local)_ |
| `BROWSER_USE_USER` | SSH user for remote mode | Current user |
| `BROWSER_USE_PYTHON` | Python binary path | `python3` |
| `BROWSER_USE_REMOTE_SCRIPT` | Path to `browser_agent.py` on remote | Auto-detect |
| `PUPPETEER_HEADLESS` | Set to `1` for headless bundled Chromium | _(visible by default)_ |
| `LOCATE_ANYTHING_API_URL` | API endpoint for hosted LocateAnything | Auto-derived from PAVE_EPM_URL |
| `LOCATE_ANYTHING_MODEL` | Model name for hosted API | `locate` |
| `LOCATE_ANYTHING_LOCAL` | Set to `1` to force local model | _(auto-detect)_ |

## License

MIT
