from __future__ import annotations

from ..import_report import ImportReportBuilder
from ..sparkrun_source import SparkRunSource
from .common import FlagSpec, RuntimeCompileError, RuntimeProjection, environment, integer, one_of, options, tokens

_FLAGS = {
    "--model": FlagSpec("--model", validate=lambda value: value.startswith("/models/") and value.endswith(".gguf")),
    "-m": FlagSpec("--model", validate=lambda value: value.startswith("/models/") and value.endswith(".gguf")),
    "--n-gpu-layers": FlagSpec("--n-gpu-layers", validate=integer(0, 999)),
    "-ngl": FlagSpec("--n-gpu-layers", validate=integer(0, 999)),
    "--ctx-size": FlagSpec("--context-size", validate=integer(1, 10_000_000)),
    "-c": FlagSpec("--context-size", validate=integer(1, 10_000_000)),
    "--parallel": FlagSpec("--parallel", validate=integer(1, 1024)),
    "--host": FlagSpec("--host", emit=False, validate=one_of("0.0.0.0")),
    "--port": FlagSpec("--port", emit=False, validate=integer(1024, 65535)),
}


def compile_llama_cpp(source: SparkRunSource, report: ImportReportBuilder) -> RuntimeProjection:
    del report
    command = tokens(source)
    if command[:1] not in (["llama-server"], ["server"]):
        raise RuntimeCompileError("llama.cpp command executable is invalid")
    arguments, parsed = options(command[1:], _FLAGS)
    if "--model" not in parsed:
        raise RuntimeCompileError("llama.cpp requires a GGUF model path")
    if (source.max_nodes or source.min_nodes or 1) > 1:
        raise RuntimeCompileError("multi-node llama.cpp requires an explicit RPC capability")
    return RuntimeProjection(
        family="llama.cpp", arguments=arguments,
        environment=environment(source, frozenset({"LLAMA_ARG_N_THREADS"})),
        endpoint={"host": str(parsed.get("--host", "0.0.0.0")), "port": int(str(parsed.get("--port", "8000"))), "health_path": "/v1/models"},
        transformed_paths=("/command",), topology_requirement="single",
    )
