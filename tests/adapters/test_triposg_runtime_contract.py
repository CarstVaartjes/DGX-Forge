import subprocess
from pathlib import Path

import pytest

from adapters.creative.triposg.runtime import (
    InferRequestError,
    build_output_path,
    health_payload,
    parse_infer_request,
)


def test_infer_request_requires_an_image_path() -> None:
    with pytest.raises(InferRequestError, match="image_path"):
        parse_infer_request({"output_path": "mesh.glb"})


def test_infer_request_rejects_paths_outside_input_root(tmp_path: Path) -> None:
    with pytest.raises(InferRequestError, match="input root"):
        parse_infer_request({"image_path": "../secret.png"}, input_root=tmp_path)


def test_output_path_is_confined_to_output_root(tmp_path: Path) -> None:
    output = build_output_path(tmp_path, "nested/mesh.glb")
    assert output == (tmp_path / "nested" / "mesh.glb").resolve()
    assert output.parent.is_dir()


def test_output_path_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(InferRequestError, match="output root"):
        build_output_path(tmp_path, "../../outside.glb")


def test_health_payload_exposes_stable_model_identity() -> None:
    assert health_payload(ready=True) == {
        "status": "ok",
        "model": "triposg",
        "ready": True,
    }


def test_adapter_rejects_unknown_operation() -> None:
    adapter = Path(__file__).parents[2] / "adapters/creative/triposg/bin/triposg"
    result = subprocess.run(
        [str(adapter), "unknown"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 64
    assert "unknown operation" in result.stderr


def test_prepare_filters_upstream_numpy_pin_for_python312_arm64() -> None:
    script = (Path(__file__).parents[2] / "adapters/creative/triposg/bin/triposg").read_text()
    assert "grep -v -E '^(numpy==1.22.3|diso)$'" in script
    assert 'numpy==2.2.6' in script
    assert "--no-build-isolation diso" in script
