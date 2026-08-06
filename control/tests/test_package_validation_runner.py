from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from dgx_agent_protocol import AgentOperation, canonical_message
from dgx_control.db import build_engine, session_factory
from dgx_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperationAttempt,
    Base,
    Job,
    PackageValidationRun,
)
from dgx_control.models import (
    AgentOperation as StoredAgentOperation,
)
from dgx_control.package_validation_runner import PackageValidationRunner


def _request() -> dict[str, object]:
    deployment = {
        "schema_version": 1,
        "deployment_id": "future-stack-canary",
        "family_id": "future-stack",
        "release_digest": "a" * 64,
        "selector": {"node_count": 1, "required_labels": {}, "preferred_node_ids": []},
        "secrets": {},
        "ports": {"inference": 8000},
        "arguments": [],
        "routing": {"alias": "future-stack", "port": "inference"},
        "resources": {"memory_bytes": 1, "storage_bytes": 1, "gpu_count": 1},
    }
    deployment_config_digest = hashlib.sha256(
        canonical_message(deployment) + b"\n"
    ).hexdigest()
    payload = {
        "schema_version": 1,
        "candidate_id": "candidate-1",
        "release_digest": "a" * 64,
        "compatibility_digest": "b" * 64,
        "deployment_id": "future-stack-canary",
        "deployment_digest": deployment_config_digest,
        "deployment": deployment,
        "deployment_config_digest": deployment_config_digest,
    }
    return {
        "candidate_id": "candidate-1",
        "validation_id": "00000000-0000-4000-8000-000000000001",
        "release_digest": "a" * 64,
        "base_commit": "d" * 40,
        "required_evidence": ["checksum", "provenance"],
        "node_ids": ["spk_" + "1" * 32],
        "operations": [
            {"kind": AgentOperation.PACKAGE_PREPARE.value, "payload": payload},
            {"kind": AgentOperation.PACKAGE_HEALTH.value, "payload": payload},
        ],
    }


class RecordingAgentJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, dict[str, object]]] = []

    def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
        del session, job_id, base_commit
        self.enqueued.append((node_id, operation, dict(payload)))
        return type("Stored", (), {"id": operation_id})()

    def notify_available(self) -> None:
        return None


def test_runner_stages_agent_operations_and_returns_running(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'validation-runner.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime(2026, 8, 6, tzinfo=UTC)
    node_id = "spk_" + "1" * 32
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=["package-abi-v1"]))
        session.add(
            PackageValidationRun(
                id="00000000-0000-4000-8000-000000000001",
                candidate_id="candidate-1",
                resolution_id="00000000-0000-4000-8000-000000000002",
                validation_kind="artifact",
                release_digest="a" * 64,
                policy_digest="b" * 64,
                fleet_digest="b" * 64,
                state="running",
                attempt=1,
                actor="admin",
                progress={"completed": 0, "failed": 0, "running": 2, "total": 2},
                created_at=now,
                updated_at=now,
            )
        )
    jobs = RecordingAgentJobs()
    runner = PackageValidationRunner(sessions, jobs, clock=lambda: now)

    invalid_release = _request()
    invalid_release["release_digest"] = "c" * 64
    with pytest.raises(ValueError, match="release identity"):
        runner(invalid_release)

    result = runner(_request())

    assert result["status"] == "running"
    assert [item[1] for item in jobs.enqueued] == [
        AgentOperation.PACKAGE_PREPARE.value,
        AgentOperation.PACKAGE_HEALTH.value,
    ]
    with sessions() as session:
        job = session.query(Job).filter_by(kind="package.validation").one()
        run = session.get(PackageValidationRun, "00000000-0000-4000-8000-000000000001")
        assert run is not None
        assert run.progress["job_id"] == job.id
        assert run.progress["required_evidence"] == ["checksum", "provenance"]


def test_runner_projects_nested_agent_evidence_and_rejects_missing_items(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'validation-tick.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime(2026, 8, 6, tzinfo=UTC)
    node_id = "spk_" + "2" * 32
    job_id = "00000000-0000-4000-8000-000000000011"
    operation_ids = [
        "00000000-0000-4000-8000-000000000012",
        "00000000-0000-4000-8000-000000000013",
    ]
    validation_id = "00000000-0000-4000-8000-000000000014"
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            AgentCertificate(
                serial="validation-serial",
                node_id=node_id,
                not_before=now,
                not_after=now,
                fingerprint="validation-fingerprint",
            )
        )
        session.add(
            Job(
                id=job_id,
                request_id=validation_id,
                kind="package.validation",
                state="running",
                actor="admin",
                base_commit="d" * 40,
                targets=[node_id],
                payload_digest="e" * 64,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        for index, operation_id in enumerate(operation_ids):
            session.add(
                StoredAgentOperation(
                    id=operation_id,
                    parent_job_id=job_id,
                    node_id=node_id,
                    kind=(
                        AgentOperation.PACKAGE_PREPARE.value
                        if index == 0
                        else AgentOperation.PACKAGE_HEALTH.value
                    ),
                    payload_digest="f" * 64,
                    payload={},
                    base_commit="d" * 40,
                    state="succeeded",
                    current_attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AgentOperationAttempt(
                    id=f"00000000-0000-4000-8000-00000000001{index + 5}",
                    operation_id=operation_id,
                    attempt=1,
                    fence=f"00000000-0000-4000-8000-00000000002{index + 5}",
                    lease_deadline=now,
                    agent_certificate_serial="validation-serial",
                    state="succeeded",
                    result={
                        "evidence": (
                            {"checksum": "a" * 64}
                            if index == 0
                            else {"provenance": "b" * 64}
                        )
                    },
                )
            )
        session.add(
            PackageValidationRun(
                id=validation_id,
                candidate_id="candidate-1",
                resolution_id="00000000-0000-4000-8000-000000000015",
                validation_kind="artifact",
                release_digest="a" * 64,
                policy_digest="b" * 64,
                fleet_digest="c" * 64,
                state="running",
                attempt=1,
                actor="admin",
                progress={
                    "operation_ids": operation_ids,
                    "required_evidence": ["checksum", "provenance"],
                    "running": 2,
                    "completed": 0,
                    "failed": 0,
                    "total": 2,
                },
                created_at=now,
                updated_at=now,
            )
        )
    runner = PackageValidationRunner(sessions, RecordingAgentJobs(), clock=lambda: now)

    assert runner.tick() is True
    with sessions() as session:
        run = session.get(PackageValidationRun, validation_id)
        assert run is not None
        assert run.state == "passed"
        assert run.evidence == {"checksum": "a" * 64, "provenance": "b" * 64}
