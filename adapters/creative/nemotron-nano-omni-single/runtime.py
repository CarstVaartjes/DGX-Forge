"""Request and filesystem boundaries for the Nemotron Nano runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class InferRequestError(ValueError):
    """Raised when a request would escape the model-owned runtime."""


@dataclass(frozen=True)
class InferRequest:
    prompt: str
    output_path: str = "response.json"
    max_tokens: int = 256
    seed: int | None = 0
    enable_thinking: bool = False


def parse_infer_request(payload: Mapping[str, Any]) -> InferRequest:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise InferRequestError("prompt is required")
    max_tokens = payload.get("max_tokens", 256)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 4096:
        raise InferRequestError("max_tokens must be an integer from 1 to 4096")
    seed = payload.get("seed", 0)
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
        raise InferRequestError("seed must be a non-negative integer or null")
    output_path = payload.get("output_path", "response.json")
    if not isinstance(output_path, str) or not output_path.strip():
        raise InferRequestError("output_path must be a non-empty string")
    enable_thinking = payload.get("enable_thinking", False)
    if not isinstance(enable_thinking, bool):
        raise InferRequestError("enable_thinking must be boolean")
    return InferRequest(
        prompt=prompt,
        output_path=output_path,
        max_tokens=max_tokens,
        seed=seed,
        enable_thinking=enable_thinking,
    )


def build_output_path(output_root: Path, output_value: str) -> Path:
    if not output_value.strip() or Path(output_value).suffix.lower() != ".json":
        raise InferRequestError("output_path must be a .json file")
    root = output_root.expanduser().resolve()
    resolved = (root / Path(output_value)).resolve()
    if not resolved.is_relative_to(root):
        raise InferRequestError("output_path must remain inside the output root")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def health_payload(*, ready: bool) -> dict[str, object]:
    return {"status": "ok", "model": "nemotron-nano-omni", "ready": ready}
