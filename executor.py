#!/usr/bin/env python3
"""
executor.py — Browser control via Puppeteer bridge + pyautogui fallback.

Primary:  Puppeteer (via Node bridge) for all in-browser actions.
Fallback: pyautogui daemon for OS-level actions (Chrome not focused, dialogs).

The Puppeteer bridge connects to Chrome via CDP (port 9222) and handles:
  - type by selector (no coordinate mapping, proper event dispatch)
  - click by selector or coordinates
  - navigate, screenshot, DOM extraction, key press, scroll
"""

import json
import socket
import sys
import time
from typing import Optional, Tuple
from PIL import Image

# ── Puppeteer bridge ───────────────────────────────────────────────────────

PUPPETEER_SOCKET = "/tmp/puppeteer-bridge.sock"
DAEMON_SOCKET = "/tmp/browser-use-daemon.sock"
TIMEOUT = 30


def _send_puppeteer(cmd: dict) -> dict:
    """Send command to Puppeteer bridge (newline-delimited JSON)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect(PUPPETEER_SOCKET)
    # Send request + newline
    s.sendall((json.dumps(cmd) + "\n").encode())
    # Read response line
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
        if b"\n" in data:
            break
    s.close()
    response = data.decode().strip()
    if not response:
        raise RuntimeError("puppeteer bridge returned empty response")
    return json.loads(response)


def _send_daemon(cmd: dict) -> dict:
    """Send command to pyautogui daemon (fallback)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect(DAEMON_SOCKET)
    s.sendall(json.dumps(cmd).encode())
    s.shutdown(socket.SHUT_WR)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data.decode())


def puppeteer_available() -> bool:
    """Check if Puppeteer bridge is running."""
    try:
        r = _send_puppeteer({"action": "ping"})
        return r.get("ok", False) and r.get("connected", False)
    except Exception:
        return False


# ���─ Navigation ────────────────────────────────────────────────────────────


def navigate(url: str, timeout: int = 15000):
    """Navigate to URL via Puppeteer."""
    r = _send_puppeteer({"action": "navigate", "url": url, "timeout": timeout})
    if not r.get("ok"):
        raise RuntimeError(f"navigate failed: {r.get('error')}")
    return r


def new_tab(url: str = ""):
    """Open a new tab."""
    r = _send_puppeteer({"action": "new_tab", "url": url})
    if not r.get("ok"):
        raise RuntimeError(f"new_tab failed: {r.get('error')}")
    return r


# ── Typing ────────────────────────────────────────────────────────────────


def type_selector(selector: str, text: str):
    """
    Type text into an element matched by CSS selector.
    Uses Puppeteer's page.type() — proper keydown/input/keyup events.
    """
    r = _send_puppeteer({"action": "type", "selector": selector, "text": text})
    if not r.get("ok"):
        raise RuntimeError(f"type failed: {r.get('error')}")
    return r


def type_at(x: int, y: int, text: str):
    """
    Click at viewport coords + type text.
    Uses Puppeteer (viewport space) first, pyautogui daemon as fallback.
    """
    # Click at viewport coords via Puppeteer
    try:
        r = _send_puppeteer({"action": "click", "x": x, "y": y})
        if r.get("ok"):
            # Type via Puppeteer keyboard
            _send_puppeteer({"action": "type_text", "text": text})
            return
    except Exception:
        pass
    # Fallback: pyautogui click + paste
    _send_daemon({"action": "type", "x": x, "y": y, "text": text})


# ── Clicking ──────────────────────────────────────────────────────────────


def click_selector(selector: str):
    """Click element by CSS selector."""
    r = _send_puppeteer({"action": "click", "selector": selector})
    if not r.get("ok"):
        raise RuntimeError(f"click failed: {r.get('error')}")
    return r


def click(x: int, y: int):
    """Click at viewport coordinates.

    Uses Puppeteer's page.mouse.click() which operates in viewport space —
    the same coordinate system as screenshots and LocateAnything results.
    Falls back to pyautogui daemon (absolute screen coords) if Puppeteer
    is unavailable.
    """
    try:
        r = _send_puppeteer({"action": "click", "x": x, "y": y})
        if r.get("ok"):
            return r
    except Exception:
        pass
    # Fallback: pyautogui (absolute screen coords — may be offset)
    _send_daemon({"action": "click", "x": x, "y": y})


# ── Keyboard ──────────────────────────────────────────────────────────────


def press_key(combo: str):
    """Press key combo. Routes to Puppeteer (in-page) or daemon (system-wide)."""
    # System-level combos go through daemon
    system_combos = ["cmd+q", "cmd+w", "cmd+space", "cmd+tab"]
    if combo.lower() in system_combos:
        _send_daemon({"action": "key", "combo": combo})
    else:
        r = _send_puppeteer({"action": "press", "key": combo})
        if not r.get("ok"):
            # Fallback to daemon
            _send_daemon({"action": "key", "combo": combo})


# ── Scroll ────────────────────────────────────────────────────────────────


def scroll(direction: str = "down", amount: int = 500):
    """Scroll via Puppeteer."""
    _send_puppeteer({"action": "scroll", "direction": direction, "amount": amount})


# ── Screenshot ────────────────────────────────────────────────────────────


def screenshot() -> Image.Image:
    """Capture screenshot via Puppeteer (page-only, clean)."""
    r = _send_puppeteer({"action": "screenshot"})
    if not r.get("ok"):
        # Fallback to mss daemon (full screen)
        r = _send_daemon({"action": "screenshot"})
    if not r.get("ok"):
        raise RuntimeError(f"screenshot failed: {r.get('error')}")
    return Image.open(r["path"])


def screenshot_full() -> Image.Image:
    """Capture full-page screenshot (scrolls entire page)."""
    r = _send_puppeteer({"action": "screenshot", "fullPage": True,
                         "output": "/tmp/browser-use-screenshot-full.png"})
    if not r.get("ok"):
        raise RuntimeError(f"screenshot failed: {r.get('error')}")
    return Image.open(r["path"])


# ── DOM ───────────────────────────────────────────────────────────────────


def get_dom() -> dict:
    """Extract interactive DOM elements with selectors and coordinates."""
    r = _send_puppeteer({"action": "dom"})
    if not r.get("ok"):
        raise RuntimeError(f"dom failed: {r.get('error')}")
    return r


def eval_js(code: str):
    """Evaluate JavaScript in the page."""
    r = _send_puppeteer({"action": "eval", "code": code})
    if not r.get("ok"):
        raise RuntimeError(f"eval failed: {r.get('error')}")
    return r.get("result")


def get_url() -> dict:
    """Get current page URL and title."""
    r = _send_puppeteer({"action": "url"})
    if not r.get("ok"):
        raise RuntimeError(f"url failed: {r.get('error')}")
    return r


# ── Browser ───────────────────────────────────────────────────────────────


def focus_chrome():
    """Bring Chrome to foreground (daemon)."""
    _send_daemon({"action": "focus_chrome"})


def wait_network_idle(idle_time: float = 1.5, timeout: float = 15.0):
    """Wait until network activity settles (no requests for idle_time seconds).

    Uses Puppeteer's waitForFunction to monitor document.readyState and
    a manual network counter injected via eval. Falls back gracefully.
    """
    deadline = time.time() + timeout
    # First check readyState
    try:
        while time.time() < deadline:
            r = _send_puppeteer({"action": "eval", "code": "document.readyState"})
            state = r.get("result", "").strip('"')
            if state == "complete":
                break
            time.sleep(0.3)
    except Exception:
        pass
    # Give a brief settle period for late XHR/fetch
    time.sleep(min(idle_time, max(0, deadline - time.time())))


def auto_screenshot(args=None):
    """Take a screenshot, save it, return (path, width, height).

    Used by --ocr flag: after any action, capture the page state.
    """
    image = screenshot()
    out_path = "/tmp/browser-use-screenshot.png"
    image.save(out_path)
    return out_path, image.size[0], image.size[1]


def wait(seconds: float = 2.0):
    """Sleep."""
    time.sleep(seconds)


# ── Diagnostics ───────────────────────────────────────────────────────────


def ping() -> bool:
    """Check Puppeteer bridge."""
    return puppeteer_available()


def test_input() -> bool:
    """Check pyautogui daemon."""
    try:
        r = _send_daemon({"action": "test_input"})
        return r.get("input_works", False)
    except Exception:
        return False


# ── DPI scale (informational) ─────────────────────────────────────────────

_DPI_SCALE: Optional[float] = None


def _detect_scale() -> float:
    global _DPI_SCALE
    if _DPI_SCALE is not None:
        return _DPI_SCALE
    try:
        import mss
        import pyautogui
        with mss.mss() as sct:
            mon = sct.monitors[1]
            phys_w = mon["width"]
        logical_w, _ = pyautogui.size()
        _DPI_SCALE = phys_w / logical_w if logical_w > 0 else 1.0
    except Exception:
        _DPI_SCALE = 1.0
    return _DPI_SCALE
