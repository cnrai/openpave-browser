#!/usr/bin/env python3
"""
cdp_client.py — Chrome DevTools Protocol client.

Connects to Chrome's CDP endpoint (port 9222) to:
  - Extract interactive DOM elements with bounding rects
  - Execute JavaScript
  - Navigate to URLs
  - Click elements by selector

This provides structured DOM context to complement LocateAnything's
visual grounding. The agent can use both: DOM for precise element lists,
LocateAnything for pixel coordinates of visual elements.
"""

import json
import socket
import struct
import os
import urllib.request
from typing import Optional

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222


def _list_pages() -> list:
    """Get list of open Chrome tabs."""
    try:
        data = urllib.request.urlopen(
            f"http://{CDP_HOST}:{CDP_PORT}/json", timeout=3
        ).read()
        return json.loads(data)
    except Exception:
        return []


def _get_active_page_ws() -> Optional[str]:
    """Get WebSocket URL of the first page tab."""
    pages = _list_pages()
    for p in pages:
        if p.get("type") == "page" and not p.get("parentId"):
            return p.get("webSocketDebuggerUrl")
    return None


# ── Minimal WebSocket client (no external deps) ───────────────────────────


class _WSClient:
    """Minimal WebSocket client for CDP communication."""

    def __init__(self, url: str):
        import re
        m = re.match(r"ws://([^:/]+):(\d+)(/.+)", url)
        if not m:
            raise ValueError(f"bad ws url: {url}")
        self.host = m.group(1)
        self.port = int(m.group(2))
        self.path = m.group(3)
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((self.host, self.port))

        import base64, hashlib
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.send(handshake.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(4096)

    def send_msg(self, data: dict):
        payload = json.dumps(data).encode("utf-8")
        mask = os.urandom(4)
        frame = bytearray([0x81])  # FIN + text

        if len(payload) < 126:
            frame.append(0x80 | len(payload))
        elif len(payload) < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", len(payload)))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", len(payload)))

        frame.extend(mask)
        for i, b in enumerate(payload):
            frame.append(b ^ mask[i % 4])

        self.sock.send(frame)

    def recv_msg(self) -> dict:
        data = b""
        # Read frame header
        while len(data) < 2:
            data += self.sock.recv(4096)

        fin = data[0] & 0x80
        opcode = data[0] & 0x0F
        masked = (data[1] & 0x80) != 0
        idx = 2
        payload_len = data[1] & 0x7F

        if payload_len == 126:
            while len(data) < idx + 2:
                data += self.sock.recv(4096)
            payload_len = struct.unpack(">H", data[idx:idx + 2])[0]
            idx += 2
        elif payload_len == 127:
            while len(data) < idx + 8:
                data += self.sock.recv(4096)
            payload_len = struct.unpack(">Q", data[idx:idx + 8])[0]
            idx += 8

        if masked:
            while len(data) < idx + 4:
                data += self.sock.recv(4096)
            mask = data[idx:idx + 4]
            idx += 4

        # Read full payload
        remaining = idx + payload_len - len(data)
        while remaining > 0:
            chunk = self.sock.recv(min(remaining, 65536))
            data += chunk
            remaining = idx + payload_len - len(data)

        raw = data[idx:idx + payload_len]
        if masked:
            raw = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))

        return json.loads(raw.decode())

    def close(self):
        if self.sock:
            self.sock.close()


# ── CDP API ───────────────────────────────────────────────��───────────────


def _eval_js(js: str, timeout: int = 10) -> any:
    """Evaluate JavaScript in the active tab, return the result."""
    ws_url = _get_active_page_ws()
    if not ws_url:
        raise RuntimeError("No active Chrome page found (CDP)")

    ws = _WSClient(ws_url)
    ws.connect()
    try:
        ws.send_msg({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js,
                "returnByValue": True,
                "awaitPromise": True,
            },
        })

        # Wait for the response with matching id
        for _ in range(20):
            msg = ws.recv_msg()
            if msg.get("id") == 1:
                result = msg.get("result", {})
                if "error" in result:
                    raise RuntimeError(f"CDP error: {result['error']}")
                val = result.get("result", {})
                if val.get("type") == "undefined":
                    return None
                return val.get("value")
        raise TimeoutError("CDP eval timed out")
    finally:
        ws.close()


# ── DOM extraction ────────────────────────────────────────────────────────


DOM_EXTRACT_JS = """
(function() {
    var results = [];
    var selector = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="textbox"], [onclick], [tabindex]';
    var els = document.querySelectorAll(selector);
    els.forEach(function(el, i) {
        var rect = el.getBoundingClientRect();
        if (rect.width > 2 && rect.height > 2) {
            results.push({
                id: i + 1,
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || '',
                role: el.getAttribute('role') || '',
                name: el.getAttribute('name') || '',
                placeholder: el.getAttribute('placeholder') || '',
                text: (el.innerText || el.textContent || '').trim().substring(0, 100),
                href: (el.href || '').substring(0, 120),
                value: (el.value || '').substring(0, 50),
                ariaLabel: el.getAttribute('aria-label') || '',
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
                cx: Math.round(rect.x + rect.width / 2),
                cy: Math.round(rect.y + rect.height / 2)
            });
        }
    });
    return JSON.stringify(results.slice(0, 80));
})()
"""


def get_viewport_offset() -> dict:
    """
    Get the offset between DOM viewport coordinates and screen coordinates.
    This is the Chrome chrome height (tabs + toolbar + bookmarks bar).
    
    Uses a trick: compare window.screenX/screenY (screen position of viewport)
    with the actual window position.
    """
    js = """
    JSON.stringify({
        screenX: window.screenX,
        screenY: window.screenY,
        outerWidth: window.outerWidth,
        outerHeight: window.outerHeight,
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight
    })
    """
    raw = _eval_js(js)
    if raw:
        import json
        info = json.loads(raw)
        return {
            "x": info.get("screenX", 0),
            "y": info.get("screenY", 0),
            "chrome_height": info.get("outerHeight", 0) - info.get("innerHeight", 0),
        }
    return {"x": 0, "y": 0, "chrome_height": 0}


def dom_to_screen(dom_x: int, dom_y: int) -> tuple:
    """
    Convert DOM viewport coordinates to screen coordinates.
    Adds the viewport offset (Chrome chrome height + window position).
    """
    offset = get_viewport_offset()
    return (dom_x + offset["x"], dom_y + offset["y"])


def get_interactive_elements() -> list:
    """
    Extract interactive DOM elements with their bounding rects.

    Returns a list of dicts with: id, tag, type, text, cx, cy, w, h, etc.
    Coordinates are in CSS pixels relative to the viewport.
    """
    raw = _eval_js(DOM_EXTRACT_JS)
    if raw:
        return json.loads(raw)
    return []


def get_page_info() -> dict:
    """Get basic page info: title, URL, scroll position."""
    js = """
    JSON.stringify({
        title: document.title,
        url: window.location.href,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        viewportW: window.innerWidth,
        viewportH: window.innerHeight,
        elementCount: document.querySelectorAll('*').length
    })
    """
    raw = _eval_js(js)
    if raw:
        return json.loads(raw)
    return {}


def navigate(url: str, wait: float = 3.0):
    """Navigate to a URL via CDP."""
    import time
    _eval_js(f'window.location.href = "{url}"')
    time.sleep(wait)


def click_selector(selector: str):
    """Click an element by CSS selector via CDP."""
    _eval_js(f'''
        var el = document.querySelector("{selector}");
        if (el) {{ el.click(); "clicked"; }} else {{ "not found"; }}
    ''')


def fill_input(selector: str, text: str):
    """Fill an input by CSS selector via CDP."""
    escaped = text.replace('"', '\\"').replace("\\", "\\\\")
    _eval_js(f'''
        var el = document.querySelector("{selector}");
        if (el) {{
            el.value = "{escaped}";
            el.dispatchEvent(new Event("input", {{bubbles: true}}));
            el.dispatchEvent(new Event("change", {{bubbles: true}}));
            "filled";
        }} else {{ "not found"; }}
    ''')


def scroll_page(direction: str = "down", amount: int = 500):
    """Scroll the page via CDP."""
    if direction == "up":
        amount = -amount
    _eval_js(f"window.scrollBy(0, {amount})")


def is_available() -> bool:
    """Check if CDP is running."""
    try:
        pages = _list_pages()
        return len(pages) > 0
    except Exception:
        return False
