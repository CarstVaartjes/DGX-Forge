"""Small OpenAI-compatible client boundary for the Nemotron Nano service."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.request import Request, urlopen

from runtime import InferRequestError, build_output_path, health_payload, parse_infer_request


def _request_json(url: str, payload: Mapping[str, object] | None = None) -> dict[str, object]:
    request = Request(url, method="POST" if payload is not None else "GET")
    body = None
    if payload is not None:
        request.add_header("content-type", "application/json")
        body = json.dumps(payload).encode("utf-8")
    with urlopen(request, data=body, timeout=1800) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise InferRequestError("runtime response must be a JSON object")
    return result


def parse_model_response(payload: Mapping[str, object]) -> tuple[str, str | None]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        raise InferRequestError("response choices must contain exactly one item")
    message = choices[0].get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
        raise InferRequestError("response choice content is required")
    reasoning = message.get("reasoning_content")
    return message["content"], reasoning if isinstance(reasoning, str) else None


def _health(base_url: str, model: str) -> None:
    models = _request_json(f"{base_url}/v1/models")
    entries = models.get("data")
    if not isinstance(entries, list) or not any(
        isinstance(entry, Mapping) and entry.get("id") == model for entry in entries
    ):
        raise InferRequestError("vLLM model identity does not match Nemotron Nano")
    print(json.dumps(health_payload(ready=True), separators=(",", ":")))


def _infer(base_url: str, model: str, output_root: Path) -> None:
    raw = json.load(sys.stdin)
    if not isinstance(raw, Mapping):
        raise InferRequestError("inference input must be a JSON object")
    request = parse_infer_request(raw)
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": request.prompt}],
        "max_tokens": request.max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": request.enable_thinking},
    }
    if request.seed is not None:
        payload["seed"] = request.seed
    response = _request_json(f"{base_url}/v1/chat/completions", payload)
    text, reasoning = parse_model_response(response)
    output_path = build_output_path(output_root, request.output_path)
    output = {"model": model, "text": text, "reasoning": reasoning, "response": response}
    output_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"model": model, "output_path": str(output_path), "text": text}, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("health", "infer"))
    parser.add_argument("--base-url", default="http://127.0.0.1:9101")
    parser.add_argument("--model", default="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4")
    args = parser.parse_args()
    if args.operation == "health":
        _health(args.base_url, args.model)
    else:
        _infer(
            args.base_url,
            args.model,
            Path(os.environ.get("NEMOTRON_NANO_OUTPUT_ROOT", "/srv/models/outputs/nemotron-nano-omni-single")),
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InferRequestError, OSError, ValueError) as error:
        raise SystemExit(f"nemotron-nano runtime error: {error}") from error
