from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from dgx_control.agent_jobs import AgentJobService, StaleAgentAttempt
from dgx_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 40


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-jobs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
            session.add(AgentCertificate(
                serial=serial,
                node_id=node_id,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=1),
                fingerprint=f"fingerprint-{serial}",
            ))
    return AgentJobService(sessions, clock=clock), sessions, clock


def parent(sessions, clock) -> Job:
    job = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        base_commit=COMMIT,
        targets=[NODE_A, NODE_B],
        payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={},
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job


def job_state(sessions, job_id: str) -> Job:
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_agent_can_claim_only_its_node_operation(service) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})

    assert jobs.claim(NODE_B, "serial-b", 30) is None
    claim = jobs.claim(NODE_A, "serial-a", 30)

    assert claim is not None
    assert claim.operation_id == operation.id
    assert claim.node_id == NODE_A


def test_concurrent_agents_cannot_claim_the_same_operation(service) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: jobs.claim(NODE_A, "serial-a", 30), range(4)))

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].operation_id == operation.id


def test_long_poll_wakes_on_enqueue_and_times_out_without_per_client_state(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(jobs.claim, NODE_A, "serial-a", 30, 1.0)
        time.sleep(0.05)
        operation = jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
        claim = waiting.result(timeout=1)
    elapsed = time.monotonic() - started

    assert claim is not None and claim.operation_id == operation.id
    assert elapsed < 0.8

    timeout_started = time.monotonic()
    assert jobs.claim(NODE_B, "serial-b", 30, 0.08) is None
    timeout_elapsed = time.monotonic() - timeout_started
    assert 0.06 <= timeout_elapsed < 0.5


def test_expired_attempt_cannot_publish_success(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=31)
    second = jobs.claim(NODE_A, "serial-a", 30)
    assert second is not None

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(first, {"healthy": True})
    jobs.succeed(second, {"healthy": True})


def test_revoked_expired_or_node_mismatched_certificate_cannot_claim(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    with sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").revoked_at = clock.now  # type: ignore[union-attr]

    assert jobs.claim(NODE_A, "serial-a", 30) is None
    assert jobs.claim(NODE_A, "serial-b", 30) is None

    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.revoked_at = None
        certificate.not_after = clock.now

    assert jobs.claim(NODE_A, "serial-a", 30) is None


def test_enqueue_rejects_noncanonical_protocol_payload(service) -> None:
    jobs, sessions, clock = service

    with pytest.raises(ValueError, match="unsafe|protocol"):
        jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {"command": "uname"})
    with pytest.raises(ValueError, match="large|protocol"):
        jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {"value": "x" * 70_000})


@pytest.mark.parametrize("terminal_state", ("succeeded", "failed", "waiting-for-operator", "expired"))
def test_sqlite_enqueue_rejects_terminal_parent(service, terminal_state: str) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).state = terminal_state  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="terminal"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})


def test_sqlite_enqueue_enforces_parent_commit_and_target(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)

    with pytest.raises(ValueError, match="base commit"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", "b" * 40, {})
    with sessions.begin() as session:
        stored_parent = session.get(Job, parent_job.id)
        assert stored_parent is not None
        stored_parent.targets = [NODE_A]
    with pytest.raises(ValueError, match="target"):
        jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})


def test_sqlite_enqueue_rejects_retired_node_before_parent_mutation(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.state = "retired"
        node.revoked_at = clock.now

    with pytest.raises(ValueError, match="active"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})

    assert job_state(sessions, parent_job.id).state == "queued"


def test_heartbeat_persists_canonical_progress_and_renews_lease(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = jobs.claim(NODE_A, "serial-a", 30)
    assert claim is not None

    progress = jobs.heartbeat(claim, {"phase": "checking"}, 60)

    assert progress.deadline > claim.deadline
    assert dict(progress.progress) == {"phase": "checking"}


def test_heartbeat_never_shortens_a_longer_existing_lease(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = jobs.claim(NODE_A, "serial-a", 120)
    assert claim is not None
    clock.advance(seconds=10)

    progress = jobs.heartbeat(claim, {"phase": "checking"}, 30)

    assert progress.deadline >= claim.deadline


@pytest.mark.parametrize("agent_action", ("heartbeat", "result"))
def test_retired_identity_cannot_mutate_active_attempt_or_record_contact(
    service, agent_action: str
) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {}
    )
    claim = jobs.claim(NODE_A, "serial-a", 30, protocol_version=2)
    assert claim is not None
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        certificate = session.get(AgentCertificate, "serial-a")
        assert node is not None and certificate is not None
        node.state = "retired"
        node.revoked_at = clock.now
        node.last_seen_at = None
        certificate.state = "revoked"
        certificate.revoked_at = clock.now

    with pytest.raises(StaleAgentAttempt):
        if agent_action == "heartbeat":
            jobs.heartbeat(claim, {"phase": "checking"}, 60)
        else:
            jobs.succeed(claim, {"healthy": True})

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        stored_operation = session.get(AgentOperation, operation.id)
        attempt = session.scalar(select(AgentOperationAttempt).where(
            AgentOperationAttempt.operation_id == operation.id,
            AgentOperationAttempt.attempt == claim.attempt,
        ))
        assert node is not None and node.last_seen_at is None
        assert node.protocol_version == 2
        assert stored_operation is not None and stored_operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.progress is None and attempt.result is None


def test_public_fence_string_interface_renews_and_completes(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = jobs.claim(NODE_A, "serial-a", 30)
    assert claim is not None

    progress = jobs.heartbeat(claim.fence, {"phase": "checking"}, 60)
    jobs.succeed(progress.fence, {"healthy": True})

    with pytest.raises(StaleAgentAttempt):
        jobs.fail(str(uuid.uuid4()), "unknown fence")


def test_structured_fence_cannot_update_a_different_operation(service) -> None:
    jobs, sessions, clock = service
    first_operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    second_operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None
    other_operation = second_operation if first.operation_id == first_operation.id else first_operation
    forged = type(first)(**{**first.__dict__, "operation_id": other_operation.id})
    with pytest.raises(StaleAgentAttempt):
        jobs.heartbeat(forged, {"phase": "forged"}, 30)
    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(forged, {"healthy": True})
    assert first.operation_id != other_operation.id


def test_attempt_expiring_exactly_at_claim_time_is_reclaimable(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=30)
    second = jobs.claim(NODE_A, "serial-a", 30)

    assert second is not None
    assert second.fence != first.fence


def test_parent_job_becomes_succeeded_only_after_every_operation_succeeds(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None
    jobs.succeed(first, {"healthy": True})
    assert job_state(sessions, parent_job.id).state == "queued"

    second = jobs.claim(NODE_B, "serial-b", 30)
    assert second is not None
    jobs.succeed(second, {"healthy": True})

    assert job_state(sessions, parent_job.id).state == "succeeded"


def test_parent_job_fails_when_all_operations_are_terminal_and_one_failed(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    failed = jobs.claim(NODE_A, "serial-a", 30)
    assert failed is not None
    jobs.fail(failed, "token=sensitive " + "x" * 2_000)
    assert job_state(sessions, parent_job.id).state == "queued"

    succeeded = jobs.claim(NODE_B, "serial-b", 30)
    assert succeeded is not None
    jobs.succeed(succeeded, {"healthy": True})

    aggregate = job_state(sessions, parent_job.id)
    assert aggregate.state == "failed"
    assert aggregate.status_reason is not None
    assert "sensitive" not in aggregate.status_reason
    assert len(aggregate.status_reason) <= 1024


def test_parent_job_waits_when_all_operations_terminal_without_failures(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    waiting = jobs.claim(NODE_A, "serial-a", 30)
    assert waiting is not None
    jobs.wait_for_operator(waiting, "confirm displayed fingerprint")

    succeeded = jobs.claim(NODE_B, "serial-b", 30)
    assert succeeded is not None
    jobs.succeed(succeeded, {"healthy": True})

    assert job_state(sessions, parent_job.id).state == "waiting-for-operator"
