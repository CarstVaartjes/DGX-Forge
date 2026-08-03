"""Request and filesystem boundaries for the Qwen3-VL runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class InferRequestError(ValueError):
    """Raised when a request would escape the model-owned runtime."""


@dataclass(frozen=True)
class InferRequest:
    image_path: Path
    prompt: str
    output_path: str = "response.json"
    max_tokens: int = 256
    seed: int | None = 0


def _confined(root: Path, candidate: Path, label: str) -> Path:
    root = root.expanduser().resolve()
    candidate = candidate.expanduser()
    resolved = (root / candidate if not candidate.is_absolute() else candidate).resolve()
    if not resolved.is_relative_to(root):
        raise InferRequestError(f"{label} must remain inside the {label} root")
    return resolved


def parse_infer_request(
    payload: Mapping[str, Any],
    *,
    input_root: Path = Path("/srv/models/inputs/qwen3-vl-8b-single"),
) -> InferRequest:
    image_value = payload.get("image_path")
    if not isinstance(image_value, str) or not image_value.strip():
        raise InferRequestError("image_path is required")
    image_path = _confined(input_root, Path(image_value), "input root")
    if image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise InferRequestError("image_path must be a supported image")
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
    return InferRequest(
        image_path=image_path,
        prompt=prompt,
        output_path=output_path,
        max_tokens=max_tokens,
        seed=seed,
    )


def build_output_path(output_root: Path, output_value: str) -> Path:
    if not output_value.strip() or Path(output_value).suffix.lower() != ".json":
        raise InferRequestError("output_path must be a .json file")
    resolved = _confined(output_root, Path(output_value), "output root")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def health_payload(*, ready: bool) -> dict[str, object]:
    return {"status": "ok", "model": "qwen3-vl-8b-instruct", "ready": ready}
