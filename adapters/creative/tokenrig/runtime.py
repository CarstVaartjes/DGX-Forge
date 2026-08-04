"""Safe request boundaries for the TokenRig/SkinTokens runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InferRequestError(ValueError):
    """Raised when a request would escape the model-owned adapter contract."""


@dataclass(frozen=True)
class InferRequest:
    input_path: Path
    output_path: str = "rigged.glb"
    use_transfer: bool = False
    use_postprocess: bool = False


def health_payload(*, ready: bool) -> dict[str, object]:
    return {"status": "ok", "model": "tokenrig", "ready": ready}


def _confined(root: Path, candidate: Path, label: str) -> Path:
    root = root.expanduser().resolve()
    candidate = candidate.expanduser()
    resolved = (root / candidate if not candidate.is_absolute() else candidate).resolve()
    if not resolved.is_relative_to(root):
        raise InferRequestError(f"{label} must remain inside the {label} root")
    return resolved


def parse_infer_request(
    payload: Mapping[str, Any], *, input_root: Path = Path("/srv/models/inputs/tokenrig-single")
) -> InferRequest:
    input_value = payload.get("input_path")
    if not isinstance(input_value, str) or not input_value.strip():
        raise InferRequestError("input_path is required")
    input_path = _confined(input_root, Path(input_value), "input root")
    if input_path.suffix.lower() not in {".glb", ".obj", ".fbx"}:
        raise InferRequestError("input_path must be a supported mesh")
    output_value = payload.get("output_path", "rigged.glb")
    if not isinstance(output_value, str) or not output_value.strip():
        raise InferRequestError("output_path must be a non-empty string")
    for field in ("use_transfer", "use_postprocess"):
        value = payload.get(field, False)
        if not isinstance(value, bool):
            raise InferRequestError(f"{field} must be boolean")
    return InferRequest(
        input_path=input_path,
        output_path=output_value,
        use_transfer=payload.get("use_transfer", False),
        use_postprocess=payload.get("use_postprocess", False),
    )


def build_output_path(output_root: Path, output_value: str) -> Path:
    if not output_value.strip() or Path(output_value).suffix.lower() != ".glb":
        raise InferRequestError("output_path must be a .glb file")
    resolved = _confined(output_root, Path(output_value), "output root")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved
