from pathlib import Path
import subprocess

import pytest

from adapters.creative.tokenrig.runtime import (
    InferRequestError,
    build_output_path,
    health_payload,
    parse_infer_request,
)


def test_request_requires_a_glb_input() -> None:
    with pytest.raises(InferRequestError, match="input_path"):
        parse_infer_request({"output_path": "rigged.glb"})
    with pytest.raises(InferRequestError, match="supported mesh"):
        parse_infer_request({"input_path": "mesh.png"})


def test_request_rejects_input_escape(tmp_path: Path) -> None:
    with pytest.raises(InferRequestError, match="input root"):
        parse_infer_request({"input_path": "../secret.glb"}, input_root=tmp_path)


def test_output_path_is_confined_to_model_root(tmp_path: Path) -> None:
    output = build_output_path(tmp_path, "nested/rigged.glb")
    assert output == (tmp_path / "nested" / "rigged.glb").resolve()
    assert output.parent.is_dir()


def test_output_path_rejects_non_glb_and_escape(tmp_path: Path) -> None:
    with pytest.raises(InferRequestError, match=".glb"):
        build_output_path(tmp_path, "rigged.obj")
    with pytest.raises(InferRequestError, match="output root"):
        build_output_path(tmp_path, "../../outside.glb")


def test_health_payload_exposes_stable_identity() -> None:
    assert health_payload(ready=True) == {
        "status": "ok",
        "model": "tokenrig",
        "ready": True,
    }


def test_adapter_rejects_unknown_operation() -> None:
    adapter = Path(__file__).parents[2] / "adapters/creative/tokenrig/bin/tokenrig"
    result = subprocess.run([str(adapter), "unknown"], capture_output=True, text=True)
    assert result.returncode == 64
    assert "unknown operation" in result.stderr


def test_adapter_declares_model_specific_runtime_paths() -> None:
    script = (Path(__file__).parents[2] / "adapters/creative/tokenrig/bin/tokenrig").read_text()
    assert 'scratch_root="$models_root/runtime-cache/tokenrig-single"' in script
    assert 'input_root="$models_root/inputs/tokenrig-single"' in script
    assert 'output_root="$models_root/outputs/tokenrig-single"' in script
