from __future__ import annotations

from ..import_report import ImportReportBuilder
from ..sparkrun_source import SparkRunSource
from .common import (
    FlagSpec,
    RuntimeCompileError,
    RuntimeProjection,
    decimal,
    environment,
    integer,
    one_of,
    options,
    tokens,
)

_FLAGS = {
    "--max-model-len": FlagSpec("--max-model-len", validate=integer(1, 10_000_000)),
    "--gpu-memory-utilization": FlagSpec("--gpu-memory-utilization", validate=decimal(0.01, 1.0)),
    "--tensor-parallel-size": FlagSpec("--tensor-parallel-size", validate=integer(1, 16)),
    "-tp": FlagSpec("--tensor-parallel-size", validate=integer(1, 16)),
    "--pipeline-parallel-size": FlagSpec("--pipeline-parallel-size", validate=integer(1, 16)),
    "--max-num-seqs": FlagSpec("--max-num-seqs", validate=integer(1, 65536)),
    "--quantization": FlagSpec("--quantization", validate=one_of("awq", "gptq", "fp8", "bitsandbytes")),
    "--dtype": FlagSpec("--dtype", validate=one_of("auto", "float16", "bfloat16", "float32")),
    "--kv-cache-dtype": FlagSpec("--kv-cache-dtype", validate=one_of("auto", "fp8", "fp8_e4m3", "fp8_e5m2")),
    "--served-model-name": FlagSpec("--served-model-name"),
    "--tool-call-parser": FlagSpec("--tool-call-parser"),
    "--enable-auto-tool-choice": FlagSpec("--enable-auto-tool-choice", takes_value=False),
    "--enable-prefix-caching": FlagSpec("--enable-prefix-caching", takes_value=False),
    "--trust-remote-code": FlagSpec("--trust-remote-code", takes_value=False, emit=False),
    "--host": FlagSpec("--host", emit=False, validate=one_of("0.0.0.0")),
    "--port": FlagSpec("--port", emit=False, validate=integer(1024, 65535)),
}


def compile_vllm(source: SparkRunSource, report: ImportReportBuilder) -> RuntimeProjection:
    del report
    command = tokens(source)
    if len(command) < 3 or command[:2] != ["vllm", "serve"] or command[2] != source.model:
        raise RuntimeCompileError("vLLM command must be 'vllm serve {model}'")
    arguments, parsed = options(command[3:], _FLAGS)
    capabilities = ("runtime.trust-remote-code.v1",) if parsed.get("--trust-remote-code") is True else ()
    tensor_parallel = int(str(parsed.get("--tensor-parallel-size", "1")))
    pipeline_parallel = int(str(parsed.get("--pipeline-parallel-size", "1")))
    return RuntimeProjection(
        family="vllm",
        arguments=arguments,
        environment=environment(source, frozenset({"NCCL_DEBUG", "HF_HUB_OFFLINE"})),
        endpoint={"host": str(parsed.get("--host", "0.0.0.0")), "port": int(str(parsed.get("--port", "8000"))), "health_path": "/v1/models"},
        transformed_paths=("/command",),
        security_capabilities=capabilities,
        topology_requirement="gang" if tensor_parallel * pipeline_parallel > 1 else "single",
    )
