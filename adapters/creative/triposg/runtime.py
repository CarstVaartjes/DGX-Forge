"""Safe request boundaries for the TripoSG image-to-mesh runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class InferRequestError(ValueError):
    """Raised when an inference request would escape the adapter contract."""


@dataclass(frozen=True)
class InferRequest:
    image_path: Path
    output_path: str = "mesh.glb"
    faces: int | None = None


def health_payload(*, ready: bool) -> dict[str, object]:
    """Return the stable identity payload used by the loopback health probe."""
    return {"status": "ok", "model": "triposg", "ready": ready}


def _confined(root: Path, candidate: Path, label: str) -> Path:
    root = root.expanduser().resolve()
    candidate = candidate.expanduser()
    resolved = (root / candidate if not candidate.is_absolute() else candidate).resolve()
    if not resolved.is_relative_to(root):
        raise InferRequestError(f"{label} must remain inside the {label} root")
    return resolved


def parse_infer_request(
    payload: Mapping[str, Any], *, input_root: Path = Path("/srv/models/inputs/triposg")
) -> InferRequest:
    """Validate a JSON-like inference payload without touching model state."""
    image_value = payload.get("image_path")
    if not isinstance(image_value, str) or not image_value.strip():
        raise InferRequestError("image_path is required")
    image_path = _confined(input_root, Path(image_value), "input root")
    if image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise InferRequestError("image_path must be a supported image")
    output_value = payload.get("output_path", "mesh.glb")
    if not isinstance(output_value, str) or not output_value.strip():
        raise InferRequestError("output_path must be a non-empty string")
    faces = payload.get("faces")
    if faces is not None and (isinstance(faces, bool) or not isinstance(faces, int) or faces <= 0):
        raise InferRequestError("faces must be a positive integer")
    return InferRequest(image_path=image_path, output_path=output_value, faces=faces)


def build_output_path(output_root: Path, output_value: str) -> Path:
    """Resolve and create a mesh output path confined to *output_root*."""
    if not output_value.strip() or Path(output_value).suffix.lower() != ".glb":
        raise InferRequestError("output_path must be a .glb file")
    resolved = _confined(output_root, Path(output_value), "output root")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved
