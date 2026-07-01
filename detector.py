#!/usr/bin/env python3
"""
LocateAnything-3B visual grounding module.

Wraps mlx-vlm to provide:
  - detect_text:   find all text labels on screen → list of (text, bbox)
  - find_element:  locate a specific UI element by natural-language description
  - scan_elements: broad scan for interactive elements

All coordinates are returned in absolute screen pixels.
"""

import re
import sys
from typing import List, Dict, Optional, Tuple

import mlx.core as mx
from PIL import Image
from mlx_vlm import load
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import prepare_inputs

# ── Model path ────────────────────────────────────────────────────────────
# Check common local paths first, fall back to HuggingFace download
import os

_LOCAL_PATHS = [
    os.environ.get("LOCATE_ANYTHING_PATH", ""),
    os.path.expanduser("~/model-label/LocateAnything-3B-MLX"),
    os.path.expanduser("~/.cache/huggingface/hub/models--nvidia--LocateAnything-3B"),
]


def _find_model_path() -> str:
    for p in _LOCAL_PATHS:
        if p and os.path.isdir(p):
            return p
    return "nvidia/LocateAnything-3B"  # HF download fallback


MODEL_ID = _find_model_path()

# LocateAnything outputs coordinates normalised to [0, 1000]
COORD_SCALE = 1000

# ── Box / point parsing ───────────────────────────────────────────────────

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


# ── Locator ───────────────────────────────────────────────────────────────

class Locator:
    """Lazy-loaded LocateAnything-3B singleton via MLX."""

    _instance: Optional["Locator"] = None
    model = None
    processor = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load(self):
        """Load the model (first call only)."""
        if self.model is not None:
            return
        print(f"[detector] Loading {MODEL_ID} …", file=sys.stderr, flush=True)
        self.model, self.processor = load(MODEL_ID)
        print("[detector] Model ready.", file=sys.stderr, flush=True)

    # ── Low-level generation ──────────────────────────────────────────

    def _generate(self, image: Image.Image, prompt: str,
                  mode: str = "hybrid", max_tokens: int = 512) -> str:
        """Run PBD generation and return raw text output."""
        self.load()

        chat_prompt = apply_chat_template(
            self.processor,
            self.model.config,
            prompt,
            num_images=1,
        )

        inputs = prepare_inputs(
            self.processor,
            images=[image],
            prompts=chat_prompt,
        )
        input_ids = inputs.pop("input_ids")
        inputs.pop("attention_mask", None)

        # Use Parallel Box Decoding for speed
        tokens = self.model.pbd_generate(
            input_ids,
            generation_mode=mode,
            max_tokens=max_tokens,
            **inputs,
        )
        text = self.processor.decode(tokens, skip_special_tokens=False)
        return text

    # ── Public API ────────────────────────────────────────────────────

    def find_element(self, image: Image.Image, description: str,
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

    def detect_text(self, image: Image.Image) -> List[Dict]:
        """
        Detect all text regions on screen.

        Returns list of {"text": str, "x1","y1","x2","y2","cx","cy": bbox}.
        Note: LocateAnything returns coordinates, not the text content itself.
        Use this to build a spatial text map for the LLM.
        """
        prompt = "Detect all the text in box format."
        raw = self._generate(image, prompt, mode="hybrid", max_tokens=2048)
        w, h = image.size
        boxes = _parse_boxes(raw, w, h)
        return boxes

    def scan_interactive(self, image: Image.Image) -> List[Dict]:
        """
        Broad scan for interactive elements (buttons, links, inputs, icons).

        Returns list of bounding boxes.
        """
        prompt = (
            "Locate all the instances that matches the following description: "
            "button</c>link</c>input field</c>icon</c>checkbox</c>dropdown"
        )
        raw = self._generate(image, prompt, mode="hybrid", max_tokens=4096)
        w, h = image.size
        return _parse_boxes(raw, w, h)
