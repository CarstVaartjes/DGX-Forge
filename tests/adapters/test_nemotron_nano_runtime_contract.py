import importlib.util
import sys
from pathlib import Path

import pytest

ADAPTER_ROOT = Path(__file__).parents[2] / "adapters/creative/nemotron-nano-omni-single"


def _load_adapter_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ADAPTER_ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runtime = _load_adapter_module("nemotron_nano_runtime_contract_runtime", "runtime.py")
sys.modules["runtime"] = runtime
server = _load_adapter_module("nemotron_nano_runtime_contract_server", "server.py")
sys.modules.pop("runtime", None)
InferRequestError = runtime.InferRequestError
build_output_path = runtime.build_output_path
parse_infer_request = runtime.parse_infer_request
parse_model_response = server.parse_model_response


def test_request_defaults_to_reasoning_off_and_validates_limits() -> None:
    request = parse_infer_request({"prompt": "hello"})
    assert request.enable_thinking is False
    assert request.max_tokens == 256

    with pytest.raises(InferRequestError, match="max_tokens"):
        parse_infer_request({"prompt": "hello", "max_tokens": 0})


def test_output_path_cannot_escape_model_root(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    assert build_output_path(output_root, "nested/result.json") == output_root / "nested/result.json"
    with pytest.raises(InferRequestError, match="output root"):
        build_output_path(output_root, "../outside.json")


def test_model_response_preserves_optional_reasoning_content() -> None:
    assert parse_model_response({"choices": [{"message": {"content": "answer"}}]}) == ("answer", None)
    assert parse_model_response(
        {"choices": [{"message": {"content": "answer", "reasoning_content": "thought"}}]}
    ) == ("answer", "thought")


def test_model_response_rejects_malformed_choices() -> None:
    with pytest.raises(InferRequestError, match="exactly one"):
        parse_model_response({"choices": []})


def test_adapter_script_is_model_owned_and_pinned() -> None:
    script = (ADAPTER_ROOT / "bin/nemotron-nano-omni-single").read_text()
    assert 'scratch_root="$models_root/runtime-cache/nemotron-nano-omni-single"' in script
    assert "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4" in script
    assert "ce1b118ae66ec705d02c241525192832eb045fd3" in script
    assert "VLLM_USE_FLASHINFER_MOE_FP4=1" in script
    assert "--reasoning-parser nano_v3" in script
