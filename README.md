# openpave-browser

Browser automation skill for OpenPAVE — control a web browser through Puppeteer (precise selector-based control) and pyautogui (coordinate-based fallback). Includes OCR-on-by-default so every command returns the page's visible text.

## Quick Start

```bash
pave install cnrai/openpave-browser
```

Then verify your setup:

```bash
# This checks Chrome, Python deps, Node.js, and prints setup hints
browser-use check
```

Fix anything it says is missing, then:

```bash
browser-use navigate "https://example.com"
browser-use url        # returns URL, title, and OCR text
```

## Prerequisites

### 1. Chrome with Remote Debugging (required)

Launch Chrome with the debugging port before using any browser commands:

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
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

### 4. Node.js + Puppeteer (required for selector commands)

```bash
# Node.js 16+ required
node --version

# Install puppeteer-core in the skill directory
cd ~/.pave/skills/browser-use && npm install
```

### 5. LocateAnything-3B (optional)

Only needed for the `find` command (visual grounding by natural-language description):

```bash
pip3 install mlx-vlm
huggingface-cli download nvidia/LocateAnything-3B
```

All other 14 commands work without it.

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
```

### Three Layers of Control

1. **Puppeteer bridge** — Precise DOM interactions via CSS selectors (`dom_click`, `dom_type`, `dom`, `eval`)
2. **pyautogui** — OS-level mouse/keyboard for coordinate-based actions (`click`, `type`, `key`, `scroll`)
3. **LocateAnything-3B** — Visual grounding to find elements by description (`find`)

## Modes

### Local Mode (default)

Runs entirely on the current machine. Requires Chrome, Python 3.9+, and Node.js 16+ locally.

### Remote Mode

For deployments where the model/GPU or Chrome is on a different machine. Set these in `~/.pave/tokens.yaml`:

```yaml
BROWSER_USE_HOST: "192.168.1.100"
BROWSER_USE_USER: "myuser"
BROWSER_USE_PYTHON: "/path/to/python3"
```

Or as environment variables:

```bash
export BROWSER_USE_HOST=192.168.1.100
export BROWSER_USE_USER=myuser
export BROWSER_USE_PYTHON=/path/to/python3
```

All commands are forwarded over SSH. Screenshots are SCP'd back automatically.

**Remote prerequisites:**
- Passwordless SSH access (`ssh-copy-id user@host`)
- Chrome with `--remote-debugging-port=9222` running on the remote
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
| `focus` | Focus Chrome window |
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
| `LOCATE_ANYTHING_PATH` | Path to LocateAnything-3B model | Auto-detect |

## License

MIT
