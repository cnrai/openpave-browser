# openpave-browser

🌐 Browser automation skill for OpenPAVE — control a web browser through Puppeteer (precise selector-based control) and LocateAnything-3B (visual grounding fallback).

## Installation

```bash
# From GitHub
pave install cnrai/openpave-browser

# From local directory
pave install ~/pave-apps/openpave-browser
```

## Prerequisites

### Chrome with Remote Debugging

Launch Chrome with remote debugging enabled:

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

### Python Dependencies

```bash
pip3 install -r requirements.txt
```

### Node.js Dependencies (for Puppeteer bridge)

```bash
npm install
```

This installs `puppeteer-core` which connects to your existing Chrome instance via CDP — no bundled Chromium download needed.

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
  LocateAnything-3B
  (visual grounding)
```

### Three Layers of Control

1. **Puppeteer bridge** — Precise DOM interactions via CSS selectors (`dom_click`, `dom_type`, `dom`, `eval`)
2. **pyautogui** — OS-level mouse/keyboard for coordinate-based actions (`click`, `type`, `key`, `scroll`)
3. **LocateAnything-3B** — Visual grounding to find elements by description (`find`)

## Modes

### Local Mode (default)

Runs entirely on the current machine. Requires Chrome, Python 3.9+, and Node.js 16+ locally.

```bash
~/.pave/skills/browser-use/browser-use.sh navigate "https://example.com"
```

### Remote Mode

For deployments where the model/GPU or Chrome is on a different machine. Set `BROWSER_USE_HOST`:

```bash
export BROWSER_USE_HOST=192.168.40.167
export BROWSER_USE_USER=cnradmin
export BROWSER_USE_PYTHON=~/.venv-vllm-mlx/bin/python3
```

All commands are forwarded over SSH. Screenshots are SCP'd back automatically.

## Commands

| Command | Description |
|---------|-------------|
| `screenshot` | Capture page screenshot |
| `dom` | Extract interactive DOM elements with CSS selectors |
| `dom --text-only` | DOM as compact text summary |
| `navigate <url>` | Navigate to URL |
| `dom_type <selector> <text>` | Type text into element by CSS selector |
| `dom_click <selector>` | Click element by CSS selector |
| `click <x> <y>` | Click at screen coordinates |
| `type <x> <y> <text>` | Type at screen coordinates |
| `find <description>` | Locate element via visual grounding |
| `key <combo>` | Press key combo (e.g. `enter`, `ctrl+a`) |
| `scroll <up\|down>` | Scroll page |
| `eval <code>` | Execute JavaScript in page |
| `url` | Get current page URL and title |
| `focus` | Focus Chrome window |
| `wait <seconds>` | Sleep |
| `check` | Verify all components are ready |

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
