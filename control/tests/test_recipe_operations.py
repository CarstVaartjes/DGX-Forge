from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dgx_agent_protocol import canonical_message
from dgx_control.artifact_sizes import ArtifactSize, StaticArtifactSizeResolver
from dgx_control.catalog_service import CatalogService, RecipeDraftInput
from dgx_control.install_admission import InstallAdmissionService
from dgx_control.inventory_repository import InventoryRepository, InventorySnapshotInput
from dgx_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentPresence,
    Base,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from dgx_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
)
from dgx_control.run_admission import RunAdmissionService
from dgx_control.topology import Placement
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


class RecordingQueue:
    def __init__(self) -> None:
        self.available = 0

    def enqueue_in_session(
        self,
        session,
        parent_job_id,
        node_id,
        operation,
        base_commit,
        payload,
        *,
        operation_id,
    ):
        row = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest="f" * 64,
            payload=dict(payload),
            base_commit=base_commit,
            state="queued",
            current_attempt=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(row)
        return row

    def notify_available(self) -> None:
        self.available += 1


NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def start_evidence(payload: dict[str, object]) -> dict[str, object]:
    identity = {
        "recipe_revision_id": payload["recipe_revision_id"],
        "recipe_content_sha256": payload["recipe_content_sha256"],
        "image_digest": "a" * 64,
        "artifact_set_digest": "b" * 64,
        "model_identity": "Qwen/Qwen3-30B-A3B-Instruct-2507@0123456789abcdef0123456789abcdef01234567",
        "rank": payload["rank"],
        "world_size": payload["world_size"],
        "endpoint": f"http://{payload['endpoint_address']}:{payload['port']}",
        "memory_reservation_bytes": payload["reserved_memory_bytes"],
        "ready": True,
    }
    return {
        **identity,
        "evidence_digest": hashlib.sha256(canonical_message(identity)).hexdigest(),
    }


def setup_services(tmp_path: Path, *, nodes: int = 1):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_ids = tuple("spk_" + f"{index + 1:032x}" for index in range(nodes))
    with sessions.begin() as session:
        for index, node_id in enumerate(node_ids):
            serial = f"serial-{index}"
            session.add(AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                capabilities=["runtime.vonk.v1", "recipe.operations.v1"],
            ))
            session.add(AgentCertificate(
                serial=serial,
                node_id=node_id,
                fingerprint=f"fingerprint-{index}",
                not_before=NOW,
                not_after=datetime(2027, 8, 7, 12, tzinfo=UTC),
            ))
            session.add(AgentPresence(
                node_id=node_id,
                certificate_serial=serial,
                certificate_fingerprint=f"fingerprint-{index}",
                management_address=f"192.168.1.{211 + index}",
                observed_at=NOW,
            ))
    inventory = InventoryRepository(sessions, clock=lambda: NOW)
    capabilities = ("runtime.vonk.v1", "recipe.operations.v1") + (
        ("fabric.tcp.mbps.1000",) if nodes > 1 else ()
    )
    for index, node_id in enumerate(node_ids):
        inventory.record(
            InventorySnapshotInput(
                node_id,
                NOW,
                10_000,
                8_000,
                10_000,
                8_000,
                10_000,
                8_000,
                1,
                False,
                capabilities,
                fabric_address=(f"192.168.100.{index + 2}" if nodes > 1 else None),
                fabric_bandwidth_mbps=(1000 if nodes > 1 else None),
            )
        )
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    document["resources"]["per_node"].update(
        {
            "download_bytes": 100,
            "installed_bytes": 120,
            "staging_bytes": 20,
            "resident_memory_bytes": 200,
            "activation_memory_bytes": 25,
        }
    )
    if nodes > 1:
        document["topology"] = {
            "kind": "gang",
            "min_nodes": nodes,
            "max_nodes": nodes,
            "tested_node_counts": [nodes],
            "fabric": {"transport": "tcp", "minimum_bandwidth_mbps": 1},
            "ranks": [
                {"rank": rank, "role": "entrypoint" if rank == 0 else "worker"}
                for rank in range(nodes)
            ],
        }
    catalog = CatalogService(sessions, clock=lambda: NOW)
    draft = catalog.create_recipe("admin", RecipeDraftInput("qwen3-vllm", document))
    revision = catalog.resolve(draft.recipe_id, 1, "admin")
    sizes = StaticArtifactSizeResolver(
        (
            ArtifactSize(document["runtime"]["image"], "1" * 64, 30),
            ArtifactSize(
                "Qwen/Qwen3-30B-A3B-Instruct-2507@"
                "0123456789abcdef0123456789abcdef01234567",
                "2" * 64,
                70,
            ),
        )
    )
    install = InstallAdmissionService(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    )
    run = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    queue = RecordingQueue()
    service = RecipeOperationService(
        sessions,
        install_admission=install,
        run_admission=run,
        agent_jobs=queue,
        clock=lambda: NOW,
    )
    return sessions, service, queue, revision.id, node_ids


def test_install_is_digest_bound_idempotent_and_gang_complete(tmp_path: Path) -> None:
    sessions, service, queue, revision_id, nodes = setup_services(tmp_path, nodes=2)
    plan = service.preview_install(revision_id, nodes)
    operation = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
    )
    repeated = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
    )

    assert repeated == operation
    assert operation.kind == "recipe.install"
    assert operation.state == "running"
    assert queue.available == 1
    with sessions() as session:
        jobs = list(session.scalars(select(Job)))
        child_operations = list(session.scalars(select(AgentOperation)))
        assert len(jobs) == 1
        assert {item.kind for item in child_operations} == {"recipe.install"}
        assert all("shell" not in json.dumps(item.payload).lower() for item in child_operations)

    service.record_node_result(operation.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120})
    assert service.get(operation.id).state == "running"
    service.record_node_result(operation.id, nodes[1], succeeded=True, evidence={"installed_bytes": 120})
    assert service.get(operation.id).state == "succeeded"
    with sessions() as session:
        assert session.get(RecipeInstallation, operation.owner_id).state == "installed"


def test_partial_install_fails_as_a_group_and_can_retry(tmp_path: Path) -> None:
    _sessions, service, _queue, revision_id, nodes = setup_services(tmp_path, nodes=2)
    plan = service.preview_install(revision_id, nodes)
    first = service.install(plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36)
    service.record_node_result(first.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120})
    service.record_node_result(first.id, nodes[1], succeeded=False, evidence={"code": "pull.failed"})

    assert service.get(first.id).state == "failed"
    assert service.get(first.id).result["successful_nodes"] == [nodes[0]]
    retry = service.retry(first.id, actor="admin", request_id="3" * 36)
    assert retry.id != first.id
    assert retry.owner_id == first.owner_id


def test_start_stop_and_uninstall_preserve_capacity_safely(tmp_path: Path) -> None:
    sessions, service, _queue, revision_id, nodes = setup_services(tmp_path)
    install_plan = service.preview_install(revision_id, nodes)
    install = service.install(install_plan, plan_digest=install_plan.plan_digest, actor="admin", request_id="4" * 36)
    service.record_node_result(install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120})

    run_plan = service.preview_run(
        install.owner_id, (Placement(nodes[0], 0, "entrypoint"),)
    )
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="qwen",
        actor="admin",
        request_id="5" * 36,
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert child is not None
        assert child.payload["endpoint_address"] == "192.168.1.211"
        assert child.payload["world_size"] == 1
        assert child.payload["master_address"] is None
        evidence = start_evidence(child.payload)
    with pytest.raises(RecipeOperationConflict, match="active run"):
        service.uninstall(install.owner_id, actor="admin", request_id="6" * 36)

    service.record_node_result(
        start.id,
        nodes[0],
        succeeded=True,
        evidence=evidence,
    )
    assert service.get(start.id).state == "succeeded"
    stop = service.stop(start.owner_id, actor="admin", request_id="7" * 36)
    assert service.get(stop.id).state == "running"
    service.record_node_result(stop.id, nodes[0], succeeded=True, evidence={"stopped": True})
    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        reservations = list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == run.id,
                    ResourceReservation.state == "active",
                )
            )
        )
        assert run.state == "stopped"
        assert reservations == []

    uninstall = service.uninstall(install.owner_id, actor="admin", request_id="8" * 36)
    service.record_node_result(uninstall.id, nodes[0], succeeded=True, evidence={"removed": True})
    with sessions() as session:
        installation = session.get(RecipeInstallation, install.owner_id)
        assert installation.state == "uninstalled"


def test_multinode_start_is_bound_to_authenticated_fabric_rendezvous(tmp_path: Path) -> None:
    sessions, service, _queue, revision_id, nodes = setup_services(tmp_path, nodes=2)
    install_plan = service.preview_install(revision_id, nodes)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="d" * 36,
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(
        install.owner_id,
        (
            Placement(nodes[0], 0, "entrypoint"),
            Placement(nodes[1], 1, "worker"),
        ),
    )
    assert run_plan.allowed is True
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="qwen-gang",
        actor="admin",
        request_id="e" * 36,
    )

    with sessions() as session:
        children = list(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == start.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert [child.payload["local_address"] for child in children] == [
            "192.168.100.2",
            "192.168.100.3",
        ]
        assert {child.payload["master_address"] for child in children} == {
            "192.168.100.2"
        }
        assert {child.payload["master_port"] for child in children} == {29500}
        assert {child.payload["world_size"] for child in children} == {2}


def test_changed_plan_or_reused_request_key_is_rejected(tmp_path: Path) -> None:
    _sessions, service, _queue, revision_id, nodes = setup_services(tmp_path)
    plan = service.preview_install(revision_id, nodes)
    with pytest.raises(RecipeOperationConflict, match="plan digest"):
        service.install(plan, plan_digest="0" * 64, actor="admin", request_id="9" * 36)
    service.install(plan, plan_digest=plan.plan_digest, actor="admin", request_id="a" * 36)
    with pytest.raises(RecipeOperationConflict, match="request key"):
        service.stop("f" * 36, actor="admin", request_id="a" * 36)
