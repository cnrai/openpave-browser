#!/usr/bin/env python3
"""
LocateAnything-3B visual grounding module.

Two modes:
  - API mode (default): sends screenshots to a hosted LocateAnything API
    via OpenAI-compatible chat completions. No local MLX dependency.
  - Local mode: loads the model directly via mlx_vlm.

Mode is determined by environment variables:
  - LOCATE_ANYTHING_API_URL set → API mode
  - LOCATE_ANYTHING_LOCAL=1     → local mode (force)
  - Auto: API mode if no local model found

API mode config (from env or ~/.pave/tokens.yaml):
  LOCATE_ANYTHING_API_URL  — e.g. https://epm.openpave.ai/pave/v1/chat/completions
  LOCATE_ANYTHING_API_KEY  — JWT token (auto-loaded from membership-credentials.json)
  LOCATE_ANYTHING_MODEL    — model name (default: locate-anything-3b)
"""

import base64
import io
import json
import os
import re
import sys
from typing import List, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

# ── Configuration ──���───────────────────────────────────────────────────────

def _get_env(name, default=None):
    return os.environ.get(name, default)


def _load_jwt():
    """Load access_token from PAVE membership credentials (read fresh each time)."""
    for path in [
        os.path.expanduser("~/.pave/membership-credentials.json"),
        os.path.expanduser("~/.pave/epm-token.json"),
    ]:
        try:
            with open(path) as f:
                data = json.load(f)
            token = data.get("access_token")
            if token:
                return token
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            continue
    return None


def _detect_mode():
    """Detect whether to use API or local mode."""
    if _get_env("LOCATE_ANYTHING_LOCAL") == "1":
        return "local"
    if _get_env("LOCATE_ANYTHING_API_URL"):
        return "api"
    # Auto-detect: check if local model exists
    local_paths = [
        _get_env("LOCATE_ANYTHING_PATH", ""),
        os.path.expanduser("~/model-label/LocateAnything-3B-MLX"),
        os.path.expanduser("~/.cache/huggingface/hub/models--nvidia--LocateAnything-3B"),
    ]
    has_local = any(p and os.path.isdir(p) for p in local_paths)
    if has_local:
        return "local"
    # Default to API mode (graceful — will error at call time if unconfigured)
    return "api"


MODE = _detect_mode()

# API mode settings
API_URL = _get_env("LOCATE_ANYTHING_API_URL", "")
API_MODEL = _get_env("LOCATE_ANYTHING_MODEL", "locate")

# Local mode settings
_LOCAL_PATHS = [
    _get_env("LOCATE_ANYTHING_PATH", ""),
    os.path.expanduser("~/model-label/LocateAnything-3B-MLX"),
    os.path.expanduser("~/.cache/huggingface/hub/models--nvidia--LocateAnything-3B"),
]


def _find_model_path() -> str:
    for p in _LOCAL_PATHS:
        if p and os.path.isdir(p):
            return p
    return "nvidia/LocateAnything-3B"  # HF download fallback


# ── Box / point parsing (shared) ───────────────────────────────────────────

COORD_SCALE = 1000

_BOX_RE = re.compile(r"<box><(\d+)><(\d+)><(\d+)><(\d+)></box>")
_POINT_RE = re.compile(r"<box><(\d+)><(\d+)></box>")


def _parse_boxes(raw: str, w: int, h: int) -> List[Dict]:
    """Parse <box><x1><y1><x2><y2></box> tokens into pixel dicts."""
    boxes = []
    for m in _BOX_RE.finditer(raw):
        x1, y1, x2, y2 = [int(g) for g in m.groups()]
        boxes.append({
            "x1": int(x1 / COORD_SCALE * w),
            "y1": int(y1 / COORD_SCALE * h),
            "x2": int(x2 / COORD_SCALE * w),
            "y2": int(y2 / COORD_SCALE * h),
            "cx": int((x1 + x2) / 2 / COORD_SCALE * w),
            "cy": int((y1 + y2) / 2 / COORD_SCALE * h),
        })
    return boxes


def _parse_points(raw: str, w: int, h: int) -> List[Dict]:
    """Parse <box><x><y></box> tokens into pixel dicts."""
    pts = []
    for m in _POINT_RE.finditer(raw):
        x, y = int(m.group(1)), int(m.group(2))
        pts.append({
            "x": int(x / COORD_SCALE * w),
            "y": int(y / COORD_SCALE * h),
        })
    return pts


# ── Image encoding for API mode ────────────────────────────────────────────

def _encode_image(image: "Image.Image") -> str:
    """Encode PIL Image as base64 data URI."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


# ── Locator ────────────────────────────────────────────────────────────────


class Locator:
    """Visual grounding via LocateAnything-3B.

    Supports both API mode (hosted model) and local mode (mlx_vlm).
    """

    _instance: Optional["Locator"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self.mode = MODE
        self.model = None
        self.processor = None

    def load(self):
        """Load model (local mode only). API mode is stateless."""
        if self.mode == "api":
            return
        if self.model is not None:
            return
        from mlx_vlm import load
        model_id = _find_model_path()
        print(f"[detector] Loading {model_id} …", file=sys.stderr, flush=True)
        self.model, self.processor = load(model_id)
        print("[detector] Model ready.", file=sys.stderr, flush=True)

    # ── API mode generation ───────────────────────────────────────────

def _generate_api(self, image: "Image.Image", prompt: str) -> str:
        """Send image + prompt to API, return raw text response.
        Reads JWT fresh on each call (PAVE rotates it every ~15 min).
        """
        import urllib.request
        import urllib.error

        if not API_URL:
            raise RuntimeError(
                "LOCATE_ANYTHING_API_URL not configured. "
                "Set it in ~/.pave/tokens.yaml or environment."
            )

        # Read JWT: prefer env var (set by index.js from PAVE credentials),
        # fall back to reading the credential file directly (local mode).
        # PAVE manages the token — the skill never refreshes it independently.
        api_key = _get_env("LOCATE_ANYTHING_API_KEY") or _load_jwt() or ""

        if not api_key:
            raise RuntimeError(
                "No API key available. PAVE JWT not found in "
                "LOCATE_ANYTHING_API_KEY env var or ~/.pave/membership-credentials.json. "
                "Ensure PAVE is authenticated."
            )

        img_b64 = _encode_image(image)

        payload = {
            "model": API_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": img_b64}},
                ],
            }],
            "max_tokens": 512,
        }

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RuntimeError("LocateAnything service is busy (429). Please retry.")
            body = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"API error {e.code}: {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Cannot reach LocateAnything API: {e}")

        # Extract text from OpenAI-format response
        try:
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise RuntimeError(f"Unexpected API response: {str(result)[:200]}")

        return text

    # ── Local mode generation ─────────────────────────────────────────

    def _generate_local(self, image: "Image.Image", prompt: str,
                        mode: str = "hybrid", max_tokens: int = 512) -> str:
        """Run PBD generation locally via mlx_vlm."""
        from mlx_vlm.prompt_utils import apply_chat_template
        from mlx_vlm.utils import prepare_inputs

        self.load()

        chat_prompt = apply_chat_template(
            self.processor, self.model.config, prompt, num_images=1,
        )
        inputs = prepare_inputs(
            self.processor, images=[image], prompts=chat_prompt,
        )
        input_ids = inputs.pop("input_ids")
        inputs.pop("attention_mask", None)

        tokens = self.model.pbd_generate(
            input_ids, generation_mode=mode, max_tokens=max_tokens, **inputs,
        )
        return self.processor.decode(tokens, skip_special_tokens=False)

    # ── Unified generation ────────────────────────────────────────────

    def _generate(self, image: "Image.Image", prompt: str,
                  mode: str = "hybrid", max_tokens: int = 512) -> str:
        """Route to API or local generation."""
        if self.mode == "api":
            return self._generate_api(image, prompt)
        return self._generate_local(image, prompt, mode, max_tokens)

    # ── Public API (unchanged interface) ──────────────────────────────

    def find_element(self, image: "Image.Image", description: str,
                     as_point: bool = True) -> Optional[Dict]:
        """
        Locate a specific UI element by natural-language description.

        Returns {"x": px, "y": px, "x1","y1","x2","y2": bbox} or None.
        """
        prompt = f"Point to: {description}"
        if not as_point:
            prompt = f"Locate the region that matches the following description: {description}."

        raw = self._generate(image, prompt, mode="hybrid", max_tokens=256)
        w, h = image.size

        if as_point:
            pts = _parse_points(raw, w, h)
            if pts:
                return pts[0]

        boxes = _parse_boxes(raw, w, h)
        if boxes:
            b = boxes[0]
            b["x"] = b["cx"]
            b["y"] = b["cy"]
            return b

        return None

    def detect_text(self, image: "Image.Image") -> List[Dict]:
        """Detect all text regions on screen."""
        prompt = "Detect all the text in box format."
        raw = self._generate(image, prompt, mode="hybrid", max_tokens=2048)
        w, h = image.size
        return _parse_boxes(raw, w, h)

    def scan_interactive(self, image: "Image.Image") -> List[Dict]:
        """Broad scan for interactive elements."""
        prompt = (
            "Locate all the instances that matches the following description: "
            "button</c>link</c>input field</c>icon</c>checkbox</c>dropdown"
        )
        raw = self._generate(image, prompt, mode="hybrid", max_tokens=4096)
        w, h = image.size
        return _parse_boxes(raw, w, h)
