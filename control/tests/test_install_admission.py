import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dgx_control.artifact_sizes import ArtifactSize, StaticArtifactSizeResolver
from dgx_control.catalog_service import CatalogService, RecipeDraftInput
from dgx_control.install_admission import InstallAdmissionService
from dgx_control.inventory_repository import InventoryRepository, InventorySnapshotInput
from dgx_control.models import (
    AgentNode,
    Base,
    NodeArtifact,
    RecipeInstallation,
    ResourceReservation,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def setup(tmp_path, *, nodes=1, free=200, read_only=False, observed_age=0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path/'install.sqlite'}"); Base.metadata.create_all(engine); sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC); node_ids = tuple("spk_"+f"{i+1:032x}" for i in range(nodes))
    inventory = InventoryRepository(sessions, clock=lambda: now)
    with sessions.begin() as session:
        session.add_all([AgentNode(node_id=node, state="active", architecture="linux-arm64", capabilities=["runtime.vllm.v1"]) for node in node_ids])
    for node in node_ids:
        inventory.record(InventorySnapshotInput(node, now-timedelta(seconds=observed_age), 1000, free, 1000, 800, 1000, 800, 1, read_only, ("runtime.vllm.v1",)))
    document = json.loads((Path(__file__).parent/"fixtures/global/recipe-v1-minimal.json").read_text()); resources = document["resources"]["per_node"]
    if nodes > 1:
        document["topology"] = {"kind": "gang", "min_nodes": nodes, "max_nodes": nodes, "tested_node_counts": [nodes], "fabric": {"transport": "tcp", "minimum_bandwidth_mbps": 1000}, "ranks": [{"rank": rank, "role": "entrypoint" if rank == 0 else "worker"} for rank in range(nodes)]}
    resources.update({"download_bytes": 100, "installed_bytes": 120, "staging_bytes": 20, "resident_memory_bytes": 200, "activation_memory_bytes": 25})
    catalog = CatalogService(sessions, clock=lambda: now); draft = catalog.create_recipe("admin", RecipeDraftInput("qwen3-vllm", document)); resolved = catalog.resolve(draft.recipe_id, 1, "admin")
    sizes = StaticArtifactSizeResolver((ArtifactSize(document["runtime"]["image"], "1"*64, 30), ArtifactSize("Qwen/Qwen3-30B-A3B-Instruct-2507@0123456789abcdef0123456789abcdef01234567", "2"*64, 70)))
    return sessions, now, node_ids, resolved.id, sizes


def test_exact_fit_and_safety_floor_are_explained(tmp_path) -> None:
    sessions, now, nodes, revision, sizes = setup(tmp_path, free=150)
    service = InstallAdmissionService(sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10)
    plan = service.plan_install(revision, nodes, now=now)
    assert plan.allowed is True
    assert plan.nodes[0].required_bytes == 140
    assert plan.nodes[0].free_after_bytes == 10

    service = InstallAdmissionService(sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=11)
    blocked = service.plan_install(revision, nodes, now=now)
    assert blocked.allowed is False
    assert blocked.nodes[0].blockers[0].code == "install.insufficient_disk"


def test_verified_existing_artifacts_reduce_disk_and_download(tmp_path) -> None:
    sessions, now, nodes, revision, sizes = setup(tmp_path, free=80)
    with sessions.begin() as session:
        session.add(NodeArtifact(node_id=nodes[0], kind="model", digest="2"*64, source="Qwen/Qwen3-30B-A3B-Instruct-2507@0123456789abcdef0123456789abcdef01234567", size_bytes=70, state="verified", ref_count=0, verified_at=now, updated_at=now))
    plan = InstallAdmissionService(sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10).plan_install(revision, nodes, now=now)
    assert plan.allowed is True
    assert plan.nodes[0].reused_bytes == 70
    assert plan.nodes[0].required_bytes == 70


def test_accepted_plan_persists_exact_identity_and_disk_reservation(tmp_path) -> None:
    sessions, now, nodes, revision, sizes = setup(tmp_path, free=200)
    service = InstallAdmissionService(sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10)
    plan = service.plan_install(revision, nodes, now=now)
    installation_id = service.accept_install(plan, actor="admin", now=now)
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        reservation = session.scalar(select(ResourceReservation).where(ResourceReservation.owner_id == installation_id))
        assert installation.plan_digest == plan.plan_digest
        assert reservation.amount_bytes == plan.nodes[0].required_bytes


def test_stale_read_only_and_partial_multinode_are_blocking(tmp_path) -> None:
    sessions, now, nodes, revision, sizes = setup(tmp_path, nodes=2, free=200, observed_age=301)
    stale = InstallAdmissionService(sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10).plan_install(revision, nodes, now=now)
    assert stale.allowed is False and all(any(item.code == "install.stale_inventory" for item in node.blockers) for node in stale.nodes)

    sessions, now, nodes, revision, sizes = setup(tmp_path/"other", nodes=2, free=200)
    inventory = InventoryRepository(sessions, clock=lambda: now)
    inventory.record(InventorySnapshotInput(nodes[1], now+timedelta(seconds=1), 1000, 50, 1000, 800, 1000, 800, 1, False, ("runtime.vllm.v1",)))
    partial = InstallAdmissionService(sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10).plan_install(revision, nodes, now=now+timedelta(seconds=1))
    assert partial.allowed is False
    assert partial.nodes[0].allowed is True and partial.nodes[1].allowed is False
