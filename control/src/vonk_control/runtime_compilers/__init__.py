from __future__ import annotations

from ..import_report import ImportReportBuilder
from ..sparkrun_source import SparkRunSource
from .common import RuntimeCompileError, RuntimeProjection
from .llama_cpp import compile_llama_cpp
from .sglang import compile_sglang
from .vllm import compile_vllm


def compile_runtime(source: SparkRunSource, report: ImportReportBuilder) -> RuntimeProjection:
    if source.runtime == "vllm":
        return compile_vllm(source, report)
    if source.runtime == "sglang":
        return compile_sglang(source, report)
    if source.runtime in {"llama.cpp", "llama-cpp"}:
        return compile_llama_cpp(source, report)
    raise RuntimeCompileError(f"unsupported SparkRun runtime: {source.runtime}")


__all__ = ["RuntimeCompileError", "RuntimeProjection", "compile_runtime"]
