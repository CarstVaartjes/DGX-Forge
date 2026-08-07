import pytest
from vonk_control.import_report import ImportReportBuilder
from vonk_control.runtime_compilers.common import RuntimeCompileError
from vonk_control.runtime_compilers.llama_cpp import compile_llama_cpp
from vonk_control.workload_run_source import parse_workload_run_yaml


def source(command: str, *, nodes: int = 1):
    return parse_workload_run_yaml(f"model: bartowski/Qwen-GGUF\nmodel_revision: 0123456789abcdef0123456789abcdef01234567\nruntime: llama.cpp\nmin_nodes: {nodes}\nmax_nodes: {nodes}\ncommand: {command!r}\n".encode())


def test_llama_cpp_maps_gguf_server_flags() -> None:
    parsed = source("llama-server --model /models/qwen.gguf --n-gpu-layers 99 --ctx-size 32768 --parallel 4 --port 8000")
    projection = compile_llama_cpp(parsed, ImportReportBuilder(parsed.leaf_paths()))
    assert projection.arguments == ("--model", "/models/qwen.gguf", "--n-gpu-layers", "99", "--context-size", "32768", "--parallel", "4")
    assert projection.topology_requirement == "single"


def test_generic_multinode_llama_cpp_is_blocked_without_rpc_capability() -> None:
    parsed = source("llama-server --model /models/qwen.gguf", nodes=2)
    with pytest.raises(RuntimeCompileError):
        compile_llama_cpp(parsed, ImportReportBuilder(parsed.leaf_paths()))
