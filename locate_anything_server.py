#!/usr/bin/env python3
"""
locate_anything_server.py — OpenAI-compatible API for LocateAnything-3B.

Serves the visual grounding model as a /v1/chat/completions endpoint.
Input: standard OpenAI vision format (text + image_url base64).
Output: chat completion with raw model text containing <box> tokens.

Single-tenant: returns 429 if busy (model is generating).

Usage:
    python locate_anything_server.py [--port 1239] [--host 0.0.0.0]

Endpoints:
    POST /v1/chat/completions   — OpenAI-compatible (vision)
    GET  /v1/models             — list available models
    GET  /health                — health check
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("locate-anything")

MODEL_DIR = os.environ.get(
    "LOCATE_ANYTHING_PATH",
    os.path.expanduser("~/model-label/LocateAnything-3B-MLX"),
)
MODEL_ID = "locate-anything-3b"

# ── Model singleton (loaded once) ──────────────────────────────────────────

_model = None
_processor = None
_model_loaded = False
_busy = False


def _load_model():
    """Load model + processor from MLX. Called once at startup."""
    global _model, _processor, _model_loaded
    if _model_loaded:
        return
    from mlx_vlm import load
    logger.info("Loading LocateAnything-3B from %s …", MODEL_DIR)
    t0 = time.time()
    _model, _processor = load(MODEL_DIR)
    _model_loaded = True
    logger.info("Model loaded in %.1fs", time.time() - t0)


def _generate(image, prompt: str, mode: str = "hybrid",
              max_tokens: int = 512) -> str:
    """Run PBD generation and return raw text."""
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import prepare_inputs

    chat_prompt = apply_chat_template(
        _processor, _model.config, prompt, num_images=1,
    )
    inputs = prepare_inputs(_processor, images=[image], prompts=chat_prompt)
    input_ids = inputs.pop("input_ids")
    inputs.pop("attention_mask", None)

    tokens = _model.pbd_generate(
        input_ids, generation_mode=mode, max_tokens=max_tokens, **inputs,
    )
    return _processor.decode(tokens, skip_special_tokens=False)


# ── Image decoding ─────────────────────────────────────────────────────────

def _decode_image(image_data: str):
    """Decode base64 image data URL or raw base64 to PIL Image."""
    from PIL import Image
    if image_data.startswith("data:"):
        _, b64 = image_data.split(",", 1)
    else:
        b64 = image_data
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw))


def _extract_content(messages):
    """Extract text prompt and image from OpenAI-format messages."""
    prompt_parts = []
    image = None
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            prompt_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                ptype = part.get("type", "")
                if ptype == "text":
                    prompt_parts.append(part.get("text", ""))
                elif ptype == "image_url":
                    img_obj = part.get("image_url", {})
                    url = img_obj.get("url", "") if isinstance(img_obj, dict) else str(img_obj)
                    if url:
                        try:
                            image = _decode_image(url)
                        except Exception as e:
                            logger.warning("Failed to decode image: %s", e)
    return "\n".join(prompt_parts), image


# ── ASGI app (raw, no FastAPI dependency for body parsing) ─────────────────

class App:
    """Minimal ASGI app for LocateAnything-3B."""

    def __init__(self):
        self.routes = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        method = scope["method"]

        # Read body
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)

        if path == "/health" and method == "GET":
            await self._json_response(send, 200, {
                "status": "ok", "model_loaded": _model_loaded, "busy": _busy,
            })
        elif path == "/v1/models" and method == "GET":
            await self._json_response(send, 200, {
                "object": "list",
                "data": [{
                    "id": MODEL_ID,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "locate-anything",
                }],
            })
        elif path == "/v1/chat/completions" and method == "POST":
            await self._handle_completion(send, body)
        elif path == "/v1/completions" and method == "POST":
            await self._json_response(send, 400, {
                "error": {"message": "Use /v1/chat/completions with image_url", "type": "invalid_request_error"}
            })
        else:
            await self._json_response(send, 404, {"error": "not found"})

    async def _json_response(self, send, status, data, extra_headers=None):
        body = json.dumps(data).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _handle_completion(self, send, raw_body):
        global _busy

        if _busy:
            await self._json_response(send, 429, {
                "error": {
                    "message": "Model is busy. Please retry.",
                    "type": "rate_limit_error",
                }
            }, extra_headers=[(b"retry-after", b"5")])
            return

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            await self._json_response(send, 400, {"error": "invalid JSON"})
            return

        messages = body.get("messages", [])
        prompt, image = _extract_content(messages)

        if not prompt:
            await self._json_response(send, 400, {
                "error": {"message": "No text prompt", "type": "invalid_request_error"}
            })
            return
        if image is None:
            await self._json_response(send, 400, {
                "error": {"message": "No image provided. This model requires image_url.", "type": "invalid_request_error"}
            })
            return

        _busy = True
        try:
            t0 = time.time()
            raw_text = await asyncio.to_thread(_generate, image, prompt)
            elapsed = time.time() - t0
            logger.info("Generated in %.2fs: %s", elapsed, raw_text[:120])
        except Exception as e:
            logger.error("Generation failed: %s", e, exc_info=True)
            _busy = False
            await self._json_response(send, 500, {
                "error": {"message": f"Generation failed: {e}", "type": "server_error"}
            })
            return
        finally:
            _busy = False

        completion = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": raw_text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
        }
        await self._json_response(send, 200, completion)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LocateAnything-3B server")
    parser.add_argument("--port", type=int, default=1239)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    _load_model()

    import uvicorn
    logger.info("Starting server on %s:%d", args.host, args.port)
    uvicorn.run(App(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
