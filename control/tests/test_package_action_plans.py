from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dgx_control.agent_jobs import AgentJobService
from dgx_control.db import build_engine, session_factory
from dgx_control.models import AgentNode, Base, Observation, PackageActionPlan
from dgx_control.models import AgentOperation as StoredAgentOperation
from dgx_control.package_services import ProductionPackageProjectionService
from dgx_control.repository import RepositoryService
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


def test_package_action_plan_persists_bounded_digest_bound_request(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    digest = "a" * 64
    with sessions.begin() as session:
        session.add(
            PackageActionPlan(
                plan_digest=digest,
                action="package.remove",
                subject="synthetic-canary",
                request={"release_digest": "b" * 64, "node_ids": ["spk_" + "1" * 32]},
                state="planned",
                expires_at=now + timedelta(minutes=15),
                created_at=now,
                updated_at=now,
            )
        )
    with sessions() as session:
        stored = session.get(PackageActionPlan, digest)
        assert stored is not None
        assert stored.request["node_ids"] == ["spk_" + "1" * 32]


def test_package_action_plan_rejects_unknown_action(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            PackageActionPlan(
                plan_digest="a" * 64,
                action="shell.exec",
                subject="x",
                request={"x": 1},
                state="planned",
                expires_at=now,
                created_at=now,
                updated_at=now,
            )
        )


def test_projection_service_reuses_exact_preview_and_rejects_changed_apply(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)

    class Repository:
        def head(self):
            return "a" * 40

    service = ProductionPackageProjectionService(Repository(), sessions)
    first = service.create_action_plan(
        "package.remove", "synthetic-canary", {"node_ids": ["spk_" + "1" * 32]}
    )
    second = service.create_action_plan(
        "package.remove", "synthetic-canary", {"node_ids": ["spk_" + "1" * 32]}
    )
    assert first == second
    request = service.consume_action_plan(first, "package.remove", "synthetic-canary")
    assert request["node_ids"] == ["spk_" + "1" * 32]
    with pytest.raises(ValueError, match="plan action or subject"):
        service.consume_action_plan(first, "package.gc", "synthetic-canary")


def test_removal_apply_queues_typed_worker_operations_and_replays(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))

        def notify_available(self):
            return None

    root = RepositoryService(Path(__file__).resolve().parents[2])
    service = ProductionPackageProjectionService(root, sessions, agent_jobs=Jobs())
    preview = service.removal_preview(
        "ds4-deepseek-single",
        "sha256:" + "a" * 64,
        ("spk_" + "1" * 32,),
    )
    result = service.remove(preview["digest"], "admin", "request-1")
    assert result["state"] == "planned"
    assert queued[0][0] == "spk_" + "1" * 32
    assert queued[0][1] == "package.remove"
    assert queued[0][2]["release_digest"] == "a" * 64
    assert service.remove(preview["digest"], "admin", "request-1") == result


def test_gc_preview_and_apply_fan_out_per_spark_without_stopping_workloads(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "2" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            Observation(
                node_id=node_id,
                kind="health",
                payload={"storage": {"total_bytes": 1000, "free_bytes": 100, "reclaimable_bytes": 400}},
                observed_at=now,
            )
        )
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))

        def notify_available(self):
            return None

    root = RepositoryService(Path(__file__).resolve().parents[2])
    service = ProductionPackageProjectionService(
        root, sessions, agent_jobs=Jobs()
    )
    preview = service.gc_preview()
    assert preview["reclaim_bytes"] == 400
    result = service.gc(preview["digest"], "admin", "request-gc")
    assert result["state"] == "planned"
    assert queued == [(node_id, "package.gc", {"schema_version": 1, "dry_run": False, "target_bytes": 400})]


def test_removal_uses_real_agent_job_boundary(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "3" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
    jobs = AgentJobService(sessions, clock=lambda: now)
    service = ProductionPackageProjectionService(
        RepositoryService(Path(__file__).resolve().parents[2]), sessions, agent_jobs=jobs
    )
    preview = service.removal_preview(
        "ds4-deepseek-single", "sha256:" + "c" * 64, (node_id,)
    )
    result = service.remove(preview["digest"], "admin", "request-real")
    assert result["state"] == "planned"
    with sessions() as session:
        operation = session.scalar(select(StoredAgentOperation))
        assert operation is not None
        assert operation.kind == "package.remove"
        assert operation.node_id == node_id


def test_rollout_preview_and_apply_use_digest_plan_and_existing_orchestrator(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "4" * 32
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))
            return type("Stored", (), {"id": "operation-id"})()

        def notify_available(self):
            return None

    root = RepositoryService(Path(__file__).resolve().parents[2])
    service = ProductionPackageProjectionService(
        root,
        sessions,
        fleet=lambda: {
            "nodes": [
                {
                    "id": node_id,
                    "healthy": True,
                    "agent_state": "active",
                    "agent_online": True,
                    "memory_available_bytes": 2_000_000_000_000,
                    "disk_available_bytes": 2_000_000_000_000,
                    "labels": {},
                    "capabilities": ["package-abi-v1"],
                    "architecture": "arm64",
                    "operating_system": "linux",
                }
            ]
        },
        agent_jobs=Jobs(),
        package_trust=lambda _release, _lock, _commit: True,
    )
    preview = service.rollout_preview("ds4-deepseek-single")
    assert preview["state"] == "ready"
    result = service.rollout(
        "ds4-deepseek-single", preview["digest"], "admin", "request-rollout"
    )
    assert result["state"] in {"planned", "running"}
    assert any(item[1] == "package.prepare" for item in queued)


def test_repair_preview_and_apply_queues_package_repair(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "5" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            Observation(
                node_id=node_id,
                kind="health",
                payload={"status": "healthy", "storage": {"total_bytes": 1000, "free_bytes": 900}},
                observed_at=now,
            )
        )
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))

        def notify_available(self):
            return None

    service = ProductionPackageProjectionService(
        RepositoryService(Path(__file__).resolve().parents[2]), sessions, agent_jobs=Jobs()
    )
    preview = service.repair_preview("ds4-deepseek-single")
    result = service.repair("ds4-deepseek-single", preview["digest"], "admin", "request-repair")
    assert result["state"] == "planned"
    assert queued[0][1] == "package.repair"
