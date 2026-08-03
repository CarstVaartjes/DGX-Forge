from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import tomllib
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dgx_agent_protocol import AgentOperation, AgentProtocolError
from dgx_control.agent_jobs import AgentJobService, StaleAgentAttempt
from dgx_control.models import AgentCertificate, AgentNode, Base, Job


ROOT = Path(__file__).resolve().parents[3]
NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 40


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def service(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-protocol.sqlite'}")
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


def enqueue(service: AgentJobService, sessions, clock) -> None:
    parent = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        base_commit=COMMIT,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={},
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)
    service.enqueue(parent.id, NODE_A, "node.probe", COMMIT, {})


def test_cross_node_claim_is_denied(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)

    assert jobs.claim(NODE_B, "serial-b", 30) is None


def test_revoked_certificate_cannot_publish_result(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)
    claim = jobs.claim(NODE_A, "serial-a", 30)
    assert claim is not None
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.revoked_at = clock.now

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(claim, {"healthy": True})


def test_secret_bearing_payload_is_rejected(service) -> None:
    jobs, sessions, clock = service
    parent = Job(
        request_id=str(uuid.uuid4()), kind="agent.operations", state="queued",
        actor="operator", base_commit=COMMIT, targets=[NODE_A],
        payload_digest=hashlib.sha256(b"{}").hexdigest(), payload={},
        current_attempt=0, created_at=clock.now, updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)

    with pytest.raises(AgentProtocolError, match="unsafe"):
        jobs.enqueue(parent.id, NODE_A, "node.probe", COMMIT, {"private_key": "unsafe"})


def test_payload_and_result_documents_are_size_limited(service) -> None:
    jobs, sessions, clock = service
    parent = Job(
        request_id=str(uuid.uuid4()), kind="agent.operations", state="queued",
        actor="operator", base_commit=COMMIT, targets=[NODE_A],
        payload_digest=hashlib.sha256(b"{}").hexdigest(), payload={},
        current_attempt=0, created_at=clock.now, updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(parent)

    with pytest.raises(AgentProtocolError, match="large"):
        jobs.enqueue(parent.id, NODE_A, "node.probe", COMMIT, {"value": "x" * 65_536})

    jobs.enqueue(parent.id, NODE_A, "node.probe", COMMIT, {})
    claim = jobs.claim(NODE_A, "serial-a", 30)
    assert claim is not None
    with pytest.raises(AgentProtocolError, match="large"):
        jobs.succeed(claim, {"value": "x" * 65_536})


def test_stale_fence_cannot_publish_success(service) -> None:
    jobs, sessions, clock = service
    enqueue(jobs, sessions, clock)
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None
    clock.advance(31)
    assert jobs.claim(NODE_A, "serial-a", 30) is not None

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(first, {"healthy": True})


def test_protocol_has_no_arbitrary_operation_member() -> None:
    with pytest.raises(ValueError):
        AgentOperation("arbitrary.command")


def test_release_artifacts_install_the_exact_protocol_wheel() -> None:
    control_project = (ROOT / "control/pyproject.toml").read_text()
    agent_project_path = ROOT / "agent/pyproject.toml"
    agent_lock_path = ROOT / "agent/uv.lock"
    dockerfile = (ROOT / "control/Dockerfile").read_text()

    assert agent_project_path.is_file()
    assert agent_lock_path.is_file()
    agent_project = agent_project_path.read_text()
    agent_lock = tomllib.loads(agent_lock_path.read_text())
    control_lock = tomllib.loads((ROOT / "control/uv.lock").read_text())
    resolved_versions = {
        package["version"]
        for lock in (agent_lock, control_lock)
        for package in lock["package"]
        if package["name"] == "dgx-agent-protocol"
    }

    assert '"dgx-agent-protocol==1.0.0"' in control_project
    assert '"dgx-agent-protocol==1.0.0"' in agent_project
    assert resolved_versions == {"1.0.0"}
    assert "COPY control/pyproject.toml ./" in dockerfile
    assert "COPY control/src ./src" in dockerfile
    assert "COPY agent_protocol/" in dockerfile
    assert "dgx_agent_protocol-1.0.0-py3-none-any.whl" in dockerfile
