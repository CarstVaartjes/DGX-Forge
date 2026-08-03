from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "adapters/creative/qwen3-vl-8b-single"))

from runtime import (  # noqa: E402
    InferRequestError,
    build_output_path,
    health_payload,
    parse_infer_request,
)
from server import build_chat_payload, parse_model_response  # noqa: E402


def test_infer_request_requires_image_and_prompt() -> None:
    with pytest.raises(InferRequestError, match="image_path"):
        parse_infer_request({"prompt": "classify"})
    with pytest.raises(InferRequestError, match="prompt"):
        parse_infer_request({"image_path": "fixture.png"})


def test_infer_request_confines_image_and_validates_bounds(tmp_path: Path) -> None:
    request = parse_infer_request(
        {"image_path": "nested/fixture.png", "prompt": "rank defects", "max_tokens": 128},
        input_root=tmp_path,
    )
    assert request.image_path == (tmp_path / "nested/fixture.png").resolve()
    with pytest.raises(InferRequestError, match="input root"):
        parse_infer_request({"image_path": "../secret.png", "prompt": "classify"}, input_root=tmp_path)
    with pytest.raises(InferRequestError, match="max_tokens"):
        parse_infer_request({"image_path": "fixture.png", "prompt": "classify", "max_tokens": 0}, input_root=tmp_path)


def test_output_path_is_confined_and_json_only(tmp_path: Path) -> None:
    output = build_output_path(tmp_path, "nested/response.json")
    assert output == (tmp_path / "nested/response.json").resolve()
    assert output.parent.is_dir()
    with pytest.raises(InferRequestError, match="output root"):
        build_output_path(tmp_path, "../../outside.json")
    with pytest.raises(InferRequestError, match=".json"):
        build_output_path(tmp_path, "response.txt")


def test_health_payload_exposes_stable_model_identity() -> None:
    assert health_payload(ready=True) == {
        "status": "ok",
        "model": "qwen3-vl-8b-instruct",
        "ready": True,
    }


def test_chat_payload_uses_model_owned_data_url(tmp_path: Path) -> None:
    image = tmp_path / "fixture.png"
    image.write_bytes(b"png-fixture")
    request = parse_infer_request(
        {"image_path": image.name, "prompt": "classify defects", "seed": 7},
        input_root=tmp_path,
    )
    payload = build_chat_payload(request, model="Qwen/Qwen3-VL-8B-Instruct")
    assert payload["model"] == "Qwen/Qwen3-VL-8B-Instruct"
    assert payload["seed"] == 7
    image_url = payload["messages"][0]["content"][0]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert "fixture.png" not in image_url


def test_model_response_requires_one_text_choice() -> None:
    assert parse_model_response({"choices": [{"message": {"content": "ok"}}]}) == "ok"
    with pytest.raises(InferRequestError, match="choices"):
        parse_model_response({"choices": []})
    with pytest.raises(InferRequestError, match="content"):
        parse_model_response({"choices": [{"message": {"content": 1}}]})


def test_adapter_rejects_unknown_operation() -> None:
    adapter = Path(__file__).parents[2] / "adapters/creative/qwen3-vl-8b-single/bin/qwen3-vl-8b-single"
    result = subprocess.run([str(adapter), "unknown"], capture_output=True, text=True)
    assert result.returncode == 64
    assert "unknown operation" in result.stderr


def test_adapter_pins_official_source_and_model_owned_runtime() -> None:
    adapter = Path(__file__).parents[2] / "adapters/creative/qwen3-vl-8b-single/bin/qwen3-vl-8b-single"
    script = adapter.read_text(encoding="utf-8")
    assert "96588727e44c78b25ba03ea03b8e12f7e64fd0da" in script
    assert "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b" in script
    assert '"vllm==0.22.0"' in script
    assert '"qwen-vl-utils==0.0.14"' in script
    assert 'scratch_root="$models_root/runtime-cache/qwen3-vl-8b-single"' in script
    assert 'venv="$scratch_root/venv"' in script
    assert "spark-model-adapter" not in script
