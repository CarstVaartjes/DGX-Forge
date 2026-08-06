import pytest

from dgx_control.import_report import ImportReportBuilder
from dgx_control.runtime_compilers.common import RuntimeCompileError
from dgx_control.runtime_compilers.sglang import compile_sglang
from dgx_control.sparkrun_source import parse_sparkrun_yaml


def source(command: str):
    return parse_sparkrun_yaml(f"model: deepseek-ai/DeepSeek-V3\nruntime: sglang\ncommand: {command!r}\n".encode())


def test_sglang_normalizes_server_and_distributed_fields() -> None:
    parsed = source("python -m sglang.launch_server --model-path {model} --tp-size 2 --context-length 32768 --port 8080")
    projection = compile_sglang(parsed, ImportReportBuilder(parsed.leaf_paths()))
    assert projection.arguments == ("--tensor-parallel-size", "2", "--context-length", "32768")
    assert projection.endpoint["port"] == 8080
    assert projection.topology_requirement == "gang"


def test_sglang_rejects_recipe_supplied_distributed_addresses() -> None:
    parsed = source("python -m sglang.launch_server --model-path {model} --dist-init-addr 10.0.0.1:1234")
    with pytest.raises(RuntimeCompileError):
        compile_sglang(parsed, ImportReportBuilder(parsed.leaf_paths()))
