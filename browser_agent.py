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
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from detector import Locator
import executor


def emit(data: dict):
    print(json.dumps(data, ensure_ascii=False))


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


def _get_locator() -> Locator:
    global _LOCATOR
    if _LOCATOR is None:
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
    """Click at screen coordinates."""
    executor.click(args.x, args.y)
    result = {"action": "click", "x": args.x, "y": args.y}
    post_ocr(args, result)
    emit(result)


def cmd_type(args):
    """Type at screen coordinates (fallback when no selector)."""
    executor.type_at(args.x, args.y, args.text)
    result = {"action": "type", "x": args.x, "y": args.y, "text": args.text}
    post_ocr(args, result)
    emit(result)


def cmd_find(args):
    """Locate element visually via LocateAnything."""
    locator = _get_locator()
    image = executor.screenshot()
    print("Searching for: %r" % args.description, file=sys.stderr)
    coords = locator.find_element(image, args.description, as_point=True)
    if coords is None:
        print("Point mode failed, retrying with box mode...", file=sys.stderr)
        coords = locator.find_element(image, args.description, as_point=False)
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
    """Verify all components."""
    results = {"components": {}, "ready": True}

    # Puppeteer bridge
    if executor.puppeteer_available():
        results["components"]["puppeteer"] = "OK (connected to Chrome)"
    else:
        results["components"]["puppeteer"] = "UNREACHABLE"
        results["ready"] = False

    # pyautogui daemon
    if executor.test_input():
        results["components"]["pyautogui daemon"] = "OK"
    else:
        results["components"]["pyautogui daemon"] = "FAILED"
        results["ready"] = False

    # Python packages
    import importlib
    for pkg in ["mss", "pyautogui", "PIL", "mlx_vlm"]:
        spec = importlib.util.find_spec(pkg)
        results["components"][pkg] = "OK" if spec else "MISSING"
        if not spec:
            results["ready"] = False

    # LocateAnything model
    la_path = os.path.expanduser("~/model-label/LocateAnything-3B-MLX")
    if os.path.isdir(la_path):
        results["components"]["LocateAnything-3B"] = "ready (%s)" % la_path
    else:
        results["components"]["LocateAnything-3B"] = "not found"

    emit(results)
    if not results["ready"]:
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
