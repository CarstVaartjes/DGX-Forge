from __future__ import annotations

from ..import_report import ImportReportBuilder
from ..workload_run_source import WorkloadRunSource
from .common import RuntimeCompileError, RuntimeProjection
from .llama_cpp import compile_llama_cpp
from .sglang import compile_sglang
from .vllm import compile_vllm


def compile_runtime(source: WorkloadRunSource, report: ImportReportBuilder) -> RuntimeProjection:
    if source.runtime == "vllm":
        return compile_vllm(source, report)
    if source.runtime == "sglang":
        return compile_sglang(source, report)
    if source.runtime in {"llama.cpp", "llama-cpp"}:
        return compile_llama_cpp(source, report)
    raise RuntimeCompileError(f"unsupported WorkloadRun runtime: {source.runtime}")


__all__ = ["RuntimeCompileError", "RuntimeProjection", "compile_runtime"]
