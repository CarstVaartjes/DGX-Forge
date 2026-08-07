from __future__ import annotations

from ..import_report import ImportReportBuilder
from ..workload_run_source import WorkloadRunSource
from .common import (
    FlagSpec,
    RuntimeCompileError,
    RuntimeProjection,
    environment,
    integer,
    one_of,
    options,
    tokens,
)

_FLAGS = {
    "--model-path": FlagSpec("--model-path", emit=False),
    "--tp": FlagSpec("--tensor-parallel-size", validate=integer(1, 16)),
    "--tp-size": FlagSpec("--tensor-parallel-size", validate=integer(1, 16)),
    "--tensor-parallel-size": FlagSpec("--tensor-parallel-size", validate=integer(1, 16)),
    "--context-length": FlagSpec("--context-length", validate=integer(1, 10_000_000)),
    "--quantization": FlagSpec("--quantization", validate=one_of("awq", "gptq", "fp8", "bitsandbytes")),
    "--host": FlagSpec("--host", emit=False, validate=one_of("0.0.0.0")),
    "--port": FlagSpec("--port", emit=False, validate=integer(1024, 65535)),
}


def compile_sglang(source: WorkloadRunSource, report: ImportReportBuilder) -> RuntimeProjection:
    del report
    command = tokens(source)
    if command[:3] == ["python", "-m", "sglang.launch_server"]:
        remainder = command[3:]
    elif command[:1] == ["sglang.launch_server"]:
        remainder = command[1:]
    else:
        raise RuntimeCompileError("SGLang command executable is invalid")
    arguments, parsed = options(remainder, _FLAGS)
    if parsed.get("--model-path") != source.model:
        raise RuntimeCompileError("SGLang model path must equal the imported model")
    tensor_parallel = int(str(parsed.get("--tensor-parallel-size", "1")))
    return RuntimeProjection(
        family="sglang", arguments=arguments,
        environment=environment(source, frozenset({"NCCL_DEBUG", "HF_HUB_OFFLINE"})),
        endpoint={"host": str(parsed.get("--host", "0.0.0.0")), "port": int(str(parsed.get("--port", "8000"))), "health_path": "/v1/models"},
        transformed_paths=("/command",), topology_requirement="gang" if tensor_parallel > 1 else "single",
    )
