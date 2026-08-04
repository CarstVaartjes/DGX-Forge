"""Small, dependency-light helpers for the Qwen3-VL OpenAI request boundary."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from runtime import InferRequest, InferRequestError, health_payload


def build_chat_payload(request: InferRequest, *, model: str) -> dict[str, Any]:
    """Build a vLLM multimodal request without exposing a host filesystem path."""
    media_type = mimetypes.guess_type(request.image_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(request.image_path.read_bytes()).decode("ascii")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                    },
                    {"type": "text", "text": request.prompt},
                ],
            }
        ],
        "max_tokens": request.max_tokens,
        "temperature": 0,
    }
    if request.seed is not None:
        payload["seed"] = request.seed
    return payload


def parse_model_response(payload: Mapping[str, Any]) -> str:
    """Extract one textual vLLM choice and reject malformed responses."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        raise InferRequestError("response choices must contain exactly one item")
    message = choices[0].get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
        raise InferRequestError("response choice content is required")
    return message["content"]


def _request_json(url: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    request = Request(url, method="POST" if payload is not None else "GET")
    if payload is not None:
        request.add_header("content-type", "application/json")
        body = json.dumps(payload).encode("utf-8")
    else:
        body = None
    with urlopen(request, data=body, timeout=1800) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise InferRequestError("runtime response must be a JSON object")
    return value


def _health(base_url: str, model: str) -> None:
    models = _request_json(f"{base_url}/v1/models")
    entries = models.get("data")
    if not isinstance(entries, list) or not any(
        isinstance(entry, Mapping) and entry.get("id") == model for entry in entries
    ):
        raise InferRequestError("vLLM model identity does not match Qwen3-VL")
    print(json.dumps(health_payload(ready=True), separators=(",", ":")))


def _infer(base_url: str, model: str, input_root: Path) -> None:
    raw = json.load(__import__("sys").stdin)
    if not isinstance(raw, Mapping):
        raise InferRequestError("inference input must be a JSON object")
    request = __import__("runtime").parse_infer_request(raw, input_root=input_root)
    response = _request_json(f"{base_url}/v1/chat/completions", build_chat_payload(request, model=model))
    text = parse_model_response(response)
    output = {
        "model": model,
        "text": text,
        "response": response,
    }
    output_path = __import__("runtime").build_output_path(
        Path(os.environ.get("QWEN3_OUTPUT_ROOT", "/srv/models/outputs/qwen3-vl-8b-single")),
        request.output_path,
    )
    output_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"model": model, "output_path": str(output_path), "text": text}, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("health", "infer"))
    parser.add_argument("--base-url", default="http://127.0.0.1:9106")
    parser.add_argument("--model", default="Qwen/Qwen3-VL-8B-Instruct")
    args = parser.parse_args()
    if args.operation == "health":
        _health(args.base_url, args.model)
    else:
        _infer(
            args.base_url,
            args.model,
            Path(os.environ.get("QWEN3_INPUT_ROOT", "/srv/models/inputs/qwen3-vl-8b-single")),
        )
    return 0


__all__ = ["build_chat_payload", "health_payload", "parse_model_response"]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InferRequestError, OSError, ValueError) as error:
        raise SystemExit(f"qwen3-vl runtime error: {error}") from error
