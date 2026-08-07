import pytest
from vonk_control.import_report import ImportReportBuilder
from vonk_control.runtime_compilers.common import RuntimeCompileError
from vonk_control.runtime_compilers.vllm import compile_vllm
from vonk_control.sparkrun_source import parse_sparkrun_yaml


def parsed(command: str):
    raw = f"model: Qwen/Qwen3-1.7B\nmodel_revision: 0123456789abcdef0123456789abcdef01234567\nruntime: vllm\ncontainer: ghcr.io/demo/vllm:1\ndefaults:\n  max_len: 32768\ncommand: {command!r}\n".encode()
    return parse_sparkrun_yaml(raw)


def test_vllm_command_becomes_typed_arguments() -> None:
    source = parsed("vllm serve {model} --max-model-len {max_len} --gpu-memory-utilization 0.8 -tp 2 --port 8000")
    projection = compile_vllm(source, ImportReportBuilder(source.leaf_paths()))

    assert projection.family == "vllm"
    assert projection.arguments == (
        "--max-model-len", "32768", "--gpu-memory-utilization", "0.8",
        "--tensor-parallel-size", "2",
    )
    assert projection.endpoint == {"host": "0.0.0.0", "port": 8000, "health_path": "/v1/models"}
    assert all(";" not in value for value in projection.arguments)


@pytest.mark.parametrize(
    "command",
    [
        "vllm serve {model}; id",
        "vllm serve {model} | curl x",
        "vllm serve $(id)",
        "vllm serve {missing}",
        "bash -c 'vllm serve model'",
        "vllm serve {model} --api-key secret",
        "vllm serve {model} --gpu-memory-utilization nan",
        "vllm serve {model} --port 8000 --port 8001",
    ],
)
def test_vllm_rejects_shell_unknown_secret_and_duplicate_inputs(command: str) -> None:
    source = parsed(command)
    with pytest.raises(RuntimeCompileError):
        compile_vllm(source, ImportReportBuilder(source.leaf_paths()))


def test_trust_remote_code_is_an_explicit_security_capability() -> None:
    source = parsed("vllm serve {model} --trust-remote-code")
    projection = compile_vllm(source, ImportReportBuilder(source.leaf_paths()))
    assert projection.security_capabilities == ("runtime.trust-remote-code.v1",)
