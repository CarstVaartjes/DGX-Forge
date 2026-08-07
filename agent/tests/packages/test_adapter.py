from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_agent.packages.adapter import (
    AdapterArtifact,
    AdapterEvidence,
    AdapterExecutionError,
    AdapterExecutor,
    AdapterInvocation,
    AdapterOperation,
    AdapterValidationError,
)

JOB = "11111111-1111-4111-8111-111111111111"
OPERATION = "22222222-2222-4222-8222-222222222222"
FENCE = "33333333-3333-4333-8333-333333333333"
RELEASE = "a" * 64
EVIDENCE = "b" * 64


class RecordingProcess:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[tuple[int, int, bytes, int, int]] = []

    def run(
        self,
        executable_fd: int,
        cwd_fd: int,
        stdin: bytes,
        timeout_seconds: int,
        output_limit_bytes: int,
        _deadline,
    ) -> bytes:
        self.calls.append(
            (executable_fd, cwd_fd, stdin, timeout_seconds, output_limit_bytes)
        )
        return json.dumps(self.result, sort_keys=True, separators=(",", ":")).encode()


def _invocation() -> AdapterInvocation:
    return AdapterInvocation(
        JOB,
        OPERATION,
        2,
        FENCE,
        RELEASE,
        "gen-a",
        "spk_" + "1" * 32,
    )


def _result(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "operation": "health",
        "status": "healthy",
        "evidence_digest": EVIDENCE,
        "job_id": JOB,
        "operation_id": OPERATION,
        "attempt": 2,
        "fence": FENCE,
        "release_digest": RELEASE,
        "generation": "gen-a",
    }
    result.update(changes)
    return result


def _executor(tmp_path: Path, name: str, process: RecordingProcess) -> AdapterExecutor:
    generation = tmp_path / "generation"
    generation.mkdir(exist_ok=True)
    executable = generation / name
    executable.write_bytes(b"#!/usr/bin/python3\n")
    executable.chmod(0o500)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return AdapterExecutor(
        AdapterArtifact(executable, digest, executable.stat().st_size),
        generation,
        process=process,
    )


@pytest.mark.parametrize("name", ["future-mia-adapter", "unknown-ds4-next"])
def test_digest_selected_dynamic_adapter_executes_without_a_compiled_name(
    tmp_path: Path, name: str
) -> None:
    process = RecordingProcess(_result())
    executor = _executor(tmp_path, name, process)

    evidence = executor.execute(
        AdapterOperation.HEALTH,
        _invocation(),
        datetime.now(UTC) + timedelta(seconds=5),
    )

    assert evidence == AdapterEvidence(
        AdapterOperation.HEALTH, "healthy", RELEASE, "gen-a", FENCE, EVIDENCE
    )
    executable_fd, _, request, timeout, output_limit = process.calls[0]
    seals = fcntl.fcntl(executable_fd, fcntl.F_GET_SEALS)
    assert seals & fcntl.F_SEAL_WRITE
    assert json.loads(request) == {
        "schema_version": 1,
        "abi_version": 1,
        "operation": "health",
        "job_id": JOB,
        "operation_id": OPERATION,
        "attempt": 2,
        "fence": FENCE,
        "release_digest": RELEASE,
        "generation": "gen-a",
    }
    assert timeout == 60
    assert output_limit == 64 * 1024
    os.close(executable_fd)


def test_adapter_rejects_content_that_does_not_match_signed_digest(
    tmp_path: Path,
) -> None:
    process = RecordingProcess(_result())
    executor = _executor(tmp_path, "adapter", process)
    executor = AdapterExecutor(
        AdapterArtifact(executor.artifact.path, "0" * 64, executor.artifact.size),
        executor.generation_root,
        process=process,
    )

    with pytest.raises(AdapterValidationError, match="digest"):
        executor.execute(
            AdapterOperation.VERIFY,
            _invocation(),
            datetime.now(UTC) + timedelta(seconds=5),
        )
    assert process.calls == []


@pytest.mark.parametrize("operation", [member.value for member in AdapterOperation])
def test_adapter_abi_v1_exposes_the_complete_generic_operation_set(
    operation: str,
) -> None:
    assert AdapterOperation(operation).value == operation


@pytest.mark.parametrize(
    "changed",
    [
        {"fence": "44444444-4444-4444-8444-444444444444"},
        {"attempt": 3},
        {"release_digest": "c" * 64},
        {"generation": "gen-b"},
        {"unknown": "field"},
    ],
)
def test_adapter_rejects_malformed_or_cross_operation_results(
    tmp_path: Path, changed: dict[str, object]
) -> None:
    executor = _executor(tmp_path, "adapter", RecordingProcess(_result(**changed)))

    with pytest.raises(AdapterValidationError, match="result|binding"):
        executor.execute(
            AdapterOperation.HEALTH,
            _invocation(),
            datetime.now(UTC) + timedelta(seconds=5),
        )


def test_adapter_rejects_elapsed_deadline_before_opening_content(
    tmp_path: Path,
) -> None:
    process = RecordingProcess(_result())
    executor = _executor(tmp_path, "adapter", process)

    with pytest.raises(AdapterExecutionError, match="deadline"):
        executor.execute(
            AdapterOperation.PREPARE,
            _invocation(),
            datetime.now(UTC) - timedelta(seconds=1),
        )
    assert process.calls == []


def test_adapter_rejects_result_replayed_across_lifecycle_operations(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path,
        "adapter",
        RecordingProcess(_result(operation="verify-release")),
    )

    with pytest.raises(AdapterValidationError, match="operation binding"):
        executor.execute(
            AdapterOperation.HEALTH,
            _invocation(),
            datetime.now(UTC) + timedelta(seconds=5),
        )
