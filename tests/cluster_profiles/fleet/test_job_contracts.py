from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cluster_profiles.fleet.job_contracts import (
    DurableJob,
    InvalidJobTransition,
    JobId,
    StaleAttempt,
)

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _queued_job() -> DurableJob:
    return DurableJob.enqueue(
        job_id=JobId.parse("job_00000000000000000000000000000001"),
        kind="install-node",
        created_at=NOW,
    )


def test_job_attempt_fences_stale_worker() -> None:
    active = _queued_job().claim(
        worker_id="worker-a",
        now=NOW,
        lease_seconds=30,
    )
    replacement = active.expire_and_requeue(
        now=NOW + timedelta(seconds=31)
    ).claim(
        worker_id="worker-b",
        now=NOW + timedelta(seconds=31),
        lease_seconds=30,
    )

    with pytest.raises(StaleAttempt):
        replacement.complete(
            attempt=active.attempt,
            now=NOW + timedelta(seconds=32),
        )
    completed = replacement.complete(
        attempt=replacement.attempt,
        now=NOW + timedelta(seconds=32),
    )
    assert completed.state == "succeeded"


def test_unexpired_job_cannot_be_requeued() -> None:
    active = _queued_job().claim("worker-a", now=NOW, lease_seconds=30)

    with pytest.raises(InvalidJobTransition, match="lease has not expired"):
        active.expire_and_requeue(now=NOW + timedelta(seconds=29))


def test_heartbeat_extends_only_matching_running_attempt() -> None:
    active = _queued_job().claim("worker-a", now=NOW, lease_seconds=30)

    heartbeat = active.heartbeat(
        attempt=active.attempt,
        worker_id="worker-a",
        now=NOW + timedelta(seconds=10),
        lease_seconds=45,
    )

    assert heartbeat.lease_deadline == NOW + timedelta(seconds=55)
    with pytest.raises(StaleAttempt):
        heartbeat.heartbeat(
            attempt=active.attempt,
            worker_id="worker-b",
            now=NOW + timedelta(seconds=11),
            lease_seconds=45,
        )


def test_terminal_job_cannot_be_claimed_or_changed() -> None:
    active = _queued_job().claim("worker-a", now=NOW, lease_seconds=30)
    completed = active.complete(attempt=active.attempt, now=NOW)

    with pytest.raises(InvalidJobTransition):
        completed.claim("worker-b", now=NOW, lease_seconds=30)
    with pytest.raises(InvalidJobTransition):
        completed.fail(attempt=completed.attempt, reason="late", now=NOW)


def test_job_failure_redacts_sensitive_authorization() -> None:
    active = _queued_job().claim("worker-a", now=NOW, lease_seconds=30)

    failed = active.fail(
        attempt=active.attempt,
        reason="Authorization: Bearer very-secret-token",
        now=NOW + timedelta(seconds=1),
    )

    assert failed.state == "failed"
    assert "very-secret-token" not in failed.failure_reason
    assert "[REDACTED]" in failed.failure_reason


@pytest.mark.parametrize("value", ["", "job_1", "node1", "job_" + "G" * 32])
def test_job_id_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError, match="job id"):
        JobId.parse(value)
