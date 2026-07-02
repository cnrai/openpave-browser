#!/usr/bin/env python3
"""
browser_agent.py — Thin tool layer for vision-driven browser control.

Architecture:
  - Puppeteer bridge: type/click by CSS selector, navigate, DOM extraction
  - pyautogui daemon: OS-level clicks at screen coords, system key combos
  - LocateAnything: visual grounding when DOM selectors aren't available

Commands:
  screenshot                      Capture page screenshot
  dom [--text-only]               Extract interactive elements (with selectors)
  navigate <url> [--wait S]       Navigate to URL
  dom_type <selector> <text>      Type text into element by CSS selector
  dom_click <selector>            Click element by CSS selector
  type <X> <Y> <text>             Type at screen coords (pyautogui fallback)
  click <X> <Y>                   Click at screen coords
  find <description>              LocateAnything → coordinates
  key <combo>                     Press key combo
  scroll <up|down>                Scroll page
  eval <javascript>               Execute JS in page
  url                             Get current URL + title
  focus                           Focus Chrome window
  wait [seconds]                  Sleep
  check                           Verify all components
"""

import argparse
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import executor  # noqa: E402


def _strip_empty(obj):
    """Recursively remove null, "", [], and {} from nested structures.

    Preserves meaningful falsy values: 0, False, 0.0.
    """
    if isinstance(obj, dict):
        return {
            k: v
            for k, v in ((k, _strip_empty(v)) for k, v in obj.items())
            if v is not None and v != "" and v != [] and v != {}
        }
    if isinstance(obj, list):
        return [_strip_empty(item) for item in obj]
    return obj


def emit(data: dict):
    cleaned = _strip_empty(data)
    print(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")))


_OCR_ENGINE = None


def _get_ocr():
    """Lazy-init OCR engine (rapidocr_onnxruntime / PP-OCRv4)."""
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_ENGINE = RapidOCR()
        return _OCR_ENGINE
    except Exception:
        return None


def _ocr_image(path):
    """Run OCR on an image file, return (text, confidence)."""
    engine = _get_ocr()
    if engine is None:
        return None
    result, elapse = engine(path)
    if not result:
        return ""
    lines = []
    confs = []
    for box, text, conf in result:
        lines.append(text)
        confs.append(conf)
    mean_conf = sum(confs) / len(confs) if confs else 0
    return {"text": "\n".join(lines), "meanConfidence": round(mean_conf, 3),
            "lines": len(lines)}


def post_ocr(args, result: dict):
    """If --ocr is set: wait for network idle, screenshot, run OCR, add to result."""
    if not getattr(args, "ocr", False):
        return
    try:
        executor.wait_network_idle(
            idle_time=getattr(args, "ocr_wait", 1.5),
            timeout=getattr(args, "ocr_timeout", 15.0),
        )
        path, w, h = executor.auto_screenshot()
        result["screenshot"] = path
        result["screenshot_size"] = {"width": w, "height": h}
        ocr_result = _ocr_image(path)
        if ocr_result is not None:
            result["ocr"] = ocr_result
        else:
            result["ocr_error"] = "OCR engine not available (install rapidocr-onnxruntime)"
    except Exception as e:
        result["screenshot_error"] = str(e)


# ── Locator singleton ─────────────────────────────────────────────────────

_LOCATOR = None


def _get_locator():
    global _LOCATOR
    if _LOCATOR is None:
        from detector import Locator
        _LOCATOR = Locator()
        _LOCATOR.load()
    return _LOCATOR


# ── Commands ──────────────────────────────────────────────────────────────


def cmd_screenshot(args):
    """Capture page screenshot."""
    if args.full:
        image = executor.screenshot_full()
    else:
        image = executor.screenshot()
    out_path = args.output or "/tmp/browser-use-screenshot.png"
    image.save(out_path)
    result = {
        "action": "screenshot",
        "path": out_path,
        "size": {"width": image.size[0], "height": image.size[1]},
    }
    if getattr(args, "ocr", False):
        ocr_result = _ocr_image(out_path)
        if ocr_result is not None:
            result["ocr"] = ocr_result
    emit(result)


def cmd_dom(args):
    """Extract interactive DOM elements with CSS selectors."""
    data = executor.get_dom()
    elements = data.get("elements", [])

    if args.text_only:
        lines = [
            "Page: %s (%s)" % (data.get("title", "?"), data.get("url", "?")),
            "Viewport: %sx%s" % (
                data.get("viewportW", "?"), data.get("viewportH", "?")),
            "Scroll: %d,%d" % (
                data.get("scrollX", 0), data.get("scrollY", 0)),
            "Elements: %d" % len(elements),
            "",
        ]
        for el in elements:
            parts = ["[%d] <%s>" % (el["id"], el["tag"])]
            if el.get("type"):
                parts.append("type=%s" % el["type"])
            if el.get("text"):
                parts.append('"%s"' % el["text"][:60])
            if el.get("placeholder"):
                parts.append('ph="%s"' % el["placeholder"][:30])
            if el.get("ariaLabel"):
                parts.append('aria="%s"' % el["ariaLabel"][:30])
            parts.append("sel=%s" % el.get("selector", "?")[:60])
            parts.append("(%d,%d)" % (el["cx"], el["cy"]))
            lines.append(" ".join(parts))
        result = {"action": "dom", "format": "text", "lines": lines,
              "page": {"title": data.get("title"),
                       "url": data.get("url")},
              "count": len(elements)}
    else:
        result = {"action": "dom", "page": {
                "title": data.get("title"),
                "url": data.get("url"),
                "viewport": "%sx%s" % (
                    data.get("viewportW"), data.get("viewportH"))},
              "elements": elements,
              "count": len(elements)}
    post_ocr(args, result)
    emit(result)


def cmd_navigate(args):
    """Navigate to URL via Puppeteer."""
    url = args.url
    if not url.startswith("http"):
        url = "https://" + url
    r = executor.navigate(url, timeout=int(args.wait * 1000))
    result = {"action": "navigate", "url": r.get("url", url),
          "title": r.get("title", "")}
    post_ocr(args, result)
    emit(result)


def cmd_dom_type(args):
    """Type text into element by CSS selector."""
    executor.type_selector(args.selector, args.text)
    result = {"action": "dom_type", "selector": args.selector, "text": args.text}
    post_ocr(args, result)
    emit(result)


def cmd_dom_click(args):
    """Click element by CSS selector."""
    executor.click_selector(args.selector)
    result = {"action": "dom_click", "selector": args.selector}
    post_ocr(args, result)
    emit(result)


def cmd_click(args):
    """Click at viewport coordinates (Puppeteer, from find/screenshot)."""
    executor.click(args.x, args.y)
    result = {"action": "click", "x": args.x, "y": args.y}
    post_ocr(args, result)
    emit(result)


def cmd_type(args):
    """Type at viewport coordinates (Puppeteer, from find/screenshot)."""
    executor.type_at(args.x, args.y, args.text)
    result = {"action": "type", "x": args.x, "y": args.y, "text": args.text}
    post_ocr(args, result)
    emit(result)


def cmd_find(args):
    """Locate element visually via LocateAnything."""
    locator = _get_locator()
    image = executor.screenshot()
    print("Searching for: %r (mode: %s)" % (args.description, locator.mode), file=sys.stderr)
    try:
        coords = locator.find_element(image, args.description, as_point=True)
    except RuntimeError as e:
        emit({"action": "find", "found": False,
              "description": args.description,
              "error": str(e)})
        sys.exit(1)
    if coords is None:
        print("Point mode failed, retrying with box mode...", file=sys.stderr)
        try:
            coords = locator.find_element(image, args.description, as_point=False)
        except RuntimeError as e:
            emit({"action": "find", "found": False,
                  "description": args.description,
                  "error": str(e)})
            sys.exit(1)
    if coords is None:
        emit({"action": "find", "found": False,
              "description": args.description})
        sys.exit(1)
    result = {
        "action": "find", "found": True,
        "description": args.description,
        "x": coords["x"], "y": coords["y"],
    }
    if "x1" in coords:
        result["bbox"] = {"x1": coords["x1"], "y1": coords["y1"],
                          "x2": coords["x2"], "y2": coords["y2"]}
    post_ocr(args, result)
    emit(result)


def cmd_key(args):
    """Press key combo."""
    executor.press_key(args.combo)
    result = {"action": "key", "combo": args.combo}
    post_ocr(args, result)
    emit(result)


def cmd_scroll(args):
    """Scroll up or down."""
    executor.scroll(args.direction, args.amount * 300)
    result = {"action": "scroll", "direction": args.direction,
          "amount": args.amount}
    post_ocr(args, result)
    emit(result)


def cmd_eval(args):
    """Execute JavaScript in page."""
    result_val = executor.eval_js(args.code)
    result = {"action": "eval", "result": result_val}
    post_ocr(args, result)
    emit(result)


def cmd_url(args):
    """Get current URL and title."""
    r = executor.get_url()
    result = {"action": "url", "url": r.get("url", ""),
              "title": r.get("title", "")}
    post_ocr(args, result)
    emit(result)


def cmd_wait(args):
    """Sleep."""
    executor.wait(args.seconds)
    emit({"action": "wait", "seconds": args.seconds})


def cmd_focus(args):
    """Focus Chrome."""
    executor.focus_chrome()
    emit({"action": "focus", "target": "chrome"})


def cmd_check(args):
    """Verify all components and print actionable setup instructions."""


def cmd_force_cleanup(args):
    """Force kill browser and clean up stale lock files."""
    result = executor.force_cleanup()
    emit({"action": "force_cleanup", **result})
    import importlib
    import importlib.util
    import shutil
    import urllib.request

    components = {}
    hints = []
    ready = True

    is_remote = bool(os.environ.get("BROWSER_USE_HOST"))
    mode = "remote (%s)" % os.environ.get("BROWSER_USE_HOST") if is_remote else "local"

    # ── Chrome / Chromium ─────────────────────────────────────────────────────────
    # Not a hard requirement — if no Chrome on :9222, the bridge launches
    # bundled Chromium automatically (visible window for co-browsing).
    chrome_ok = False
    chrome_detail = ""
    try:
        resp = urllib.request.urlopen(
            "http://localhost:9222/json/version", timeout=3)
        info = json.loads(resp.read())
        browser = info.get("Browser", "unknown")
        chrome_detail = "%s at localhost:9222 (will attach)" % browser
        chrome_ok = True
    except Exception:
        chrome_detail = "not running — bundled Chromium will launch automatically"
    components["chrome"] = {
        "status": "ok" if chrome_ok else "auto",
        "detail": chrome_detail,
        "required_for": "all commands (attaches to existing or launches bundled)",
    }

    # ── Node.js (required for Puppeteer bridge) ─────────────────────────────
    node_ok = False
    node_detail = ""
    node_bin = shutil.which("node") or os.path.expanduser("~/tools/node/bin/node")
    try:
        r = subprocess.run([node_bin, "--version"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            node_detail = r.stdout.strip()
            node_ok = True
        else:
            node_detail = "node binary found but --version failed"
    except Exception as e:
        node_detail = str(e)[:80]
    components["node"] = {
        "status": "ok" if node_ok else "missing",
        "detail": node_detail,
        "required_for": "navigate, dom, dom_click, dom_type, eval, click, type, scroll, key",
    }
    if not node_ok:
        ready = False
        hints.append(
            "INSTALL NODE.JS: Node.js 16+ is required for the Puppeteer bridge.\n"
            "  Install from https://nodejs.org/ or: brew install node")

    # ── puppeteer npm package ────────────────────────────────────────────────
    pp_ok = False
    pp_detail = ""
    if node_ok:
        node_dir = os.path.join(SCRIPT_DIR, "node")
        try:
            r = subprocess.run(
                [node_bin, "-e",
                 "require('puppeteer'); console.log('ok')"],
                capture_output=True, text=True, timeout=5,
                cwd=node_dir)
            if r.returncode == 0 and "ok" in r.stdout:
                pp_ok = True
                pp_detail = "puppeteer loaded (bundled Chromium available)"
            else:
                pp_detail = (r.stderr or r.stdout).strip()[:80]
        except Exception as e:
            pp_detail = str(e)[:80]
    else:
        pp_detail = "skipped (node missing)"
    components["puppeteer"] = {
        "status": "ok" if pp_ok else "missing",
        "detail": pp_detail,
        "required_for": "all browser commands (includes bundled Chromium)",
    }
    if node_ok and not pp_ok:
        ready = False
        hints.append(
            "INSTALL PUPPETEER: Run in the skill directory:\n"
            "  cd %s && npm install" % SCRIPT_DIR)

    # ── Python packages ─────────────────────────────────────────────────────
    core_missing = []
    for pkg, label, required_for in [
        ("mss", "mss (screenshots)", "screenshot"),
        ("pyautogui", "pyautogui (mouse/keyboard)", "click, type, key, scroll (fallback)"),
        ("PIL", "Pillow (image handling)", "screenshot, OCR"),
    ]:
        spec = importlib.util.find_spec(pkg)
        is_ok = spec is not None
        components[label] = {
            "status": "ok" if is_ok else "missing",
            "required_for": required_for,
        }
        if not is_ok:
            core_missing.append(pkg)

    if core_missing:
        ready = False
        hints.append(
            "INSTALL PYTHON DEPS: Run in the skill directory:\n"
            "  pip3 install -r requirements.txt")

    # OCR (optional but on by default)
    ocr_spec = importlib.util.find_spec("rapidocr_onnxruntime")
    components["rapidocr-onnxruntime (OCR engine)"] = {
        "status": "ok" if ocr_spec else "missing",
        "required_for": "OCR (on by default)",
    }
    if not ocr_spec:
        hints.append(
            "INSTALL OCR (optional, recommended): OCR is on by default.\n"
            "  pip3 install rapidocr-onnxruntime")

    # ── LocateAnything (for `find` command) ─────────────────────────────────
    la_api_url = os.environ.get("LOCATE_ANYTHING_API_URL", "")
    la_local_paths = [
        os.environ.get("LOCATE_ANYTHING_PATH", ""),
        os.path.expanduser("~/model-label/LocateAnything-3B-MLX"),
        os.path.expanduser("~/.cache/huggingface/hub/models--nvidia--LocateAnything-3B"),
    ]
    la_local_found = any(p and os.path.isdir(p) for p in la_local_paths)
    mlx_spec = importlib.util.find_spec("mlx_vlm")

    # Read JWT fresh (rotates every ~15 min)
    la_api_key = os.environ.get("LOCATE_ANYTHING_API_KEY", "")
    if not la_api_key:
        for cred_path in [
            os.path.expanduser("~/.pave/membership-credentials.json"),
            os.path.expanduser("~/.pave/epm-token.json"),
        ]:
            try:
                with open(cred_path) as f:
                    cred = json.load(f)
                la_api_key = cred.get("access_token", "")
                if la_api_key:
                    break
            except (FileNotFoundError, json.JSONDecodeError):
                continue

    # Prefer local mode if model exists (matches detector.py _detect_mode)
    if la_local_found and mlx_spec:
        components["LocateAnything-3B"] = {
            "status": "ok",
            "mode": "local",
            "detail": "model + mlx_vlm ready",
            "required_for": "find (visual grounding by description)",
        }
    elif la_api_url:
        # API mode — verify by sending a minimal chat completions
        # request (POST with max_tokens=1). The EPM doesn't expose /models.
        import urllib.request
        import urllib.error
        api_ok = False
        api_detail = ""
        try:
            test_payload = json.dumps({
                "model": os.environ.get("LOCATE_ANYTHING_MODEL", "locate"),
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }).encode("utf-8")
            req = urllib.request.Request(la_api_url, data=test_payload,
                                        method="POST")
            req.add_header("Content-Type", "application/json")
            if la_api_key:
                req.add_header("Authorization", "Bearer " + la_api_key)
            resp = urllib.request.urlopen(req, timeout=15)
            api_ok = True
            api_detail = "connected, JWT valid"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                api_ok = True
                api_detail = "connected (busy, 429 — expected when model is in use)"
            else:
                api_detail = "HTTP %d: %s" % (e.code, e.read().decode("utf-8", errors="replace")[:60])
        except Exception as e:
            api_detail = str(e)[:80]
        components["LocateAnything-3B"] = {
            "status": "ok" if api_ok else "error",
            "mode": "api",
            "detail": api_detail or la_api_url[:60],
            "required_for": "find (visual grounding by description)",
        }
        if not api_ok:
            hints.append(
                "LOCATEANYTHING API: Cannot reach %s\n"
                "  JWT is read dynamically from ~/.pave/membership-credentials.json.\n"
                "  Ensure you're logged in: pave login\n"
                "  To use a local model instead: set LOCATE_ANYTHING_LOCAL=1" % la_api_url[:60])
    else:
        components["LocateAnything-3B"] = {
            "status": "not_configured",
            "mode": "none",
            "detail": "No API URL or local model found",
            "required_for": "find (visual grounding by description)",
        }
        hints.append(
            "LOCATEANYTHING (for 'find' command): Not configured.\n"
            "  Option A (hosted, no download): Add this to ~/.pave/tokens.yaml:\n"
            "    LOCATE_ANYTHING_API_URL: \"https://epm.openpave.ai/pave/v1/chat/completions\"\n"
            "  Then run 'pave login' (provides the JWT automatically).\n"
            "  Option B (local model): pip3 install mlx-vlm && huggingface-cli download nvidia/LocateAnything-3B")

    # ── Remote mode checks ──────────────────────────────────────────────────
    if is_remote:
        host = os.environ.get("BROWSER_USE_HOST")
        user = os.environ.get("BROWSER_USE_USER", os.environ.get("USER", ""))
        ssh_ok = False
        ssh_detail = ""
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
                 "%s@%s" % (user, host), "echo", "ok"],
                capture_output=True, text=True, timeout=8)
            if r.returncode == 0 and "ok" in r.stdout:
                ssh_ok = True
                ssh_detail = "SSH to %s@%s works" % (user, host)
            else:
                ssh_detail = (r.stderr or r.stdout)[:80]
        except Exception as e:
            ssh_detail = str(e)[:80]
        components["ssh"] = {
            "status": "ok" if ssh_ok else "error",
            "detail": ssh_detail,
            "required_for": "remote mode (BROWSER_USE_HOST is set)",
        }
        if not ssh_ok:
            ready = False
            hints.append(
                "SSH ACCESS: Set up passwordless SSH to %s@%s:\n"
                "  ssh-copy-id %s@%s" % (user, host, user, host))

    emit({
        "status": "ready" if ready else "not_ready",
        "mode": mode,
        "components": components,
        "setup_hints": hints if hints else ["All components ready!"],
    })
    if not ready:
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────


def add_ocr_args(p):
    """Add OCR args to a subparser. OCR is ON by default; use --no-ocr to disable."""
    p.add_argument("--ocr", dest="ocr", action="store_true",
                   help="Enable OCR (default: on)")
    p.add_argument("--no-ocr", dest="ocr", action="store_false",
                   help="Disable OCR")
    p.set_defaults(ocr=True)
    p.add_argument("--ocr-wait", type=float, default=1.5,
                   help="Network idle settle time in seconds (default 1.5)")
    p.add_argument("--ocr-timeout", type=float, default=15.0,
                   help="Max wait for network idle in seconds (default 15)")


def main():
    parser = argparse.ArgumentParser(
        description="Vision-driven browser control (Puppeteer + LocateAnything)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # screenshot
    p = sub.add_parser("screenshot", help="Capture page screenshot")
    p.add_argument("--output", default=None)
    p.add_argument("--full", action="store_true", help="Full page capture")
    p.add_argument("--ocr", dest="ocr", action="store_true",
                   help="Run OCR on screenshot (default: on)")
    p.add_argument("--no-ocr", dest="ocr", action="store_false",
                   help="Skip OCR")
    p.set_defaults(ocr=True)
    p.set_defaults(func=cmd_screenshot)

    # dom
    p = sub.add_parser("dom", help="Extract interactive DOM elements")
    p.add_argument("--text-only", action="store_true",
                   help="Simplified text output with selectors")
    add_ocr_args(p)
    p.set_defaults(func=cmd_dom)

    # navigate
    p = sub.add_parser("navigate", help="Navigate to URL")
    p.add_argument("url", help="URL to navigate to")
    p.add_argument("--wait", type=float, default=5.0,
                   help="Timeout in seconds (default 5)")
    add_ocr_args(p)
    p.set_defaults(func=cmd_navigate)

    # dom_type
    p = sub.add_parser("dom_type", help="Type text into element by CSS selector")
    p.add_argument("selector", help="CSS selector (e.g. textarea[name=q])")
    p.add_argument("text", help="Text to type")
    add_ocr_args(p)
    p.set_defaults(func=cmd_dom_type)

    # dom_click
    p = sub.add_parser("dom_click", help="Click element by CSS selector")
    p.add_argument("selector", help="CSS selector")
    add_ocr_args(p)
    p.set_defaults(func=cmd_dom_click)

    # click
    p = sub.add_parser("click", help="Click at screen coordinates")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    add_ocr_args(p)
    p.set_defaults(func=cmd_click)

    # type
    p = sub.add_parser("type", help="Type at screen coords (pyautogui)")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("text", help="Text to type")
    add_ocr_args(p)
    p.set_defaults(func=cmd_type)

    # find
    p = sub.add_parser("find", help="Locate element via LocateAnything")
    p.add_argument("description", help="Natural-language description")
    add_ocr_args(p)
    p.set_defaults(func=cmd_find)

    # key
    p = sub.add_parser("key", help="Press key combo")
    p.add_argument("combo", help='e.g. "enter", "cmd+t", "escape"')
    add_ocr_args(p)
    p.set_defaults(func=cmd_key)

    # scroll
    p = sub.add_parser("scroll", help="Scroll page")
    p.add_argument("direction", choices=["up", "down"])
    p.add_argument("--amount", type=int, default=3, help="Scroll steps (default 3)")
    add_ocr_args(p)
    p.set_defaults(func=cmd_scroll)

    # eval
    p = sub.add_parser("eval", help="Execute JavaScript in page")
    p.add_argument("code", help="JavaScript code")
    add_ocr_args(p)
    p.set_defaults(func=cmd_eval)

    # url
    p = sub.add_parser("url", help="Get current URL and title")
    add_ocr_args(p)
    p.set_defaults(func=cmd_url)

    # wait
    p = sub.add_parser("wait", help="Sleep")
    p.add_argument("seconds", type=float, nargs="?", default=2.0)
    p.set_defaults(func=cmd_wait)

    # focus
    p = sub.add_parser("focus", help="Focus Chrome window")
    p.set_defaults(func=cmd_focus)

    # check
    p = sub.add_parser("check", help="Verify all components")
    p.set_defaults(func=cmd_check)

    # force_cleanup
    p = sub.add_parser("force_cleanup", help="Kill stale browser and clean up lock files")
    p.set_defaults(func=cmd_force_cleanup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
