from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dgx_agent.packages.adapter import (
    AdapterArtifact,
    AdapterExecutionError,
    AdapterExecutor,
    AdapterInvocation,
    AdapterOperation,
    AdapterValidationError,
)

JOB_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
FENCE = "33333333-3333-4333-8333-333333333333"
RELEASE_DIGEST = "a" * 64
EVIDENCE_DIGEST = "b" * 64


class AdapterResultProcess:
    """Return one controlled result from the otherwise-real adapter boundary."""

    def __init__(self, *, operation: str, status: str) -> None:
        self.operation = operation
        self.status = status
        self.calls = 0

    def run(
        self,
        executable_fd: int,
        cwd_fd: int,
        stdin: bytes,
        timeout_seconds: int,
        output_limit_bytes: int,
        deadline,
    ) -> bytes:
        self.calls += 1
        request = json.loads(stdin)
        return json.dumps(
            {
                "schema_version": 1,
                "operation": self.operation,
                "status": self.status,
                "evidence_digest": EVIDENCE_DIGEST,
                "job_id": request["job_id"],
                "operation_id": request["operation_id"],
                "attempt": request["attempt"],
                "fence": request["fence"],
                "release_digest": request["release_digest"],
                "generation": request["generation"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


def _executor(
    tmp_path: Path, *, result_operation: str, status: str
) -> tuple[AdapterExecutor, AdapterResultProcess]:
    generation = tmp_path / "generation"
    generation.mkdir()
    executable = generation / "future-runtime-adapter"
    executable.write_bytes(b"#!/usr/bin/python3\n")
    executable.chmod(0o500)
    artifact = AdapterArtifact(
        executable,
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        executable.stat().st_size,
    )
    process = AdapterResultProcess(
        operation=result_operation,
        status=status,
    )
    return AdapterExecutor(
        artifact,
        generation,
        process=process,
    ), process


def _invocation() -> AdapterInvocation:
    return AdapterInvocation(
        JOB_ID,
        OPERATION_ID,
        1,
        FENCE,
        RELEASE_DIGEST,
        "generation-a",
        "spk_" + "1" * 32,
    )


@pytest.mark.parametrize(
    ("operation", "status"),
    (
        (AdapterOperation.VERIFY_RELEASE, "validated"),
        (AdapterOperation.HEALTH, "healthy"),
    ),
)
def test_validation_and_runtime_health_evidence_is_bound_to_its_exact_phase(
    tmp_path: Path,
    operation: AdapterOperation,
    status: str,
) -> None:
    executor, process = _executor(
        tmp_path,
        result_operation=operation.value,
        status=status,
    )

    evidence = executor.execute(
        operation,
        _invocation(),
        datetime.now(UTC) + timedelta(seconds=5),
    )

    assert evidence.operation is operation
    assert evidence.status == status
    assert evidence.release_digest == RELEASE_DIGEST
    assert evidence.generation == "generation-a"
    assert evidence.fence == FENCE
    assert process.calls == 1


def test_package_validation_result_cannot_be_replayed_as_runtime_health(
    tmp_path: Path,
) -> None:
    executor, process = _executor(
        tmp_path,
        result_operation=AdapterOperation.VERIFY_RELEASE.value,
        status="validated",
    )

    with pytest.raises(AdapterValidationError, match="operation binding"):
        executor.execute(
            AdapterOperation.HEALTH,
            _invocation(),
            datetime.now(UTC) + timedelta(seconds=5),
        )
    assert process.calls == 1


def test_elapsed_runtime_health_deadline_never_invokes_adapter(
    tmp_path: Path,
) -> None:
    executor, process = _executor(
        tmp_path,
        result_operation=AdapterOperation.HEALTH.value,
        status="healthy",
    )

    with pytest.raises(AdapterExecutionError, match="deadline"):
        executor.execute(
            AdapterOperation.HEALTH,
            _invocation(),
            datetime.now(UTC) - timedelta(seconds=1),
        )
    assert process.calls == 0
