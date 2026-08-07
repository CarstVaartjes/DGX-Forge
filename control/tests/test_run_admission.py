import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dgx_control.inventory_repository import InventoryRepository, InventorySnapshotInput
from dgx_control.models import (
    AgentNode,
    Base,
    InstallationNode,
    LocalRecipe,
    LocalRecipeRevision,
    RecipeInstallation,
    ResourceReservation,
)
from dgx_control.run_admission import RunAdmissionService, RunPlanConflict
from dgx_control.topology import Placement
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def setup(tmp_path, *, free_memory=300, capabilities=("runtime.vonk.v1",), port_reserved=False):
    engine = create_engine(f"sqlite:///{tmp_path/'run.sqlite'}"); Base.metadata.create_all(engine); sessions = sessionmaker(engine, expire_on_commit=False); now = datetime(2026,8,7,12,tzinfo=UTC); node="spk_"+"1"*32
    document=json.loads((Path(__file__).parent/"fixtures/global/recipe-v1-minimal.json").read_text()); document["resources"]["per_node"].update({"resident_memory_bytes":200,"activation_memory_bytes":25})
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node,state="active",architecture="linux-arm64",capabilities=list(capabilities)))
        recipe=LocalRecipe(slug="qwen",title="Qwen",description="Qwen",source_kind="local",created_by="admin",created_at=now,updated_at=now);session.add(recipe);session.flush()
        revision=LocalRecipeRevision(recipe_id=recipe.id,revision_number=1,lifecycle="resolved",schema_version=1,document=document,content_sha256="a"*64,created_by="admin",created_at=now);session.add(revision);session.flush()
        installation=RecipeInstallation(recipe_revision_id=revision.id,plan_digest="b"*64,plan={},state="installed",actor="admin",created_at=now,updated_at=now);session.add(installation);session.flush()
        session.add(InstallationNode(installation_id=installation.id,node_id=node,state="installed",required_bytes=1,installed_bytes=1,updated_at=now))
        if port_reserved: session.add(ResourceReservation(node_id=node,kind="port",resource_key="8000",amount_bytes=0,owner_kind="run",owner_id="1"*36,state="active",plan_digest="c"*64,created_at=now))
    InventoryRepository(sessions,clock=lambda:now).record(InventorySnapshotInput(node,now,1000,500,1000,free_memory,1000,free_memory,1,False,tuple(capabilities)))
    return sessions,now,node,installation.id


def test_run_plan_accounts_for_memory_floor_and_persists_reservations(tmp_path) -> None:
    sessions,now,node,installation=setup(tmp_path,free_memory=300)
    service=RunAdmissionService(sessions,inventory_max_age=300,memory_floor_bytes=50)
    plan=service.plan_run(installation,(Placement(node,0,"entrypoint"),),now=now)
    assert plan.allowed is True and plan.nodes[0].required_memory_bytes==225 and plan.nodes[0].free_after_bytes==75
    run_id=service.accept_run(plan,alias="qwen",actor="admin",now=now)
    assert run_id


def test_memory_capability_and_port_conflicts_are_explained(tmp_path) -> None:
    sessions,now,node,installation=setup(tmp_path,free_memory=260,capabilities=("runtime.sglang.v1",),port_reserved=True)
    plan=RunAdmissionService(sessions,inventory_max_age=300,memory_floor_bytes=50).plan_run(installation,(Placement(node,0,"entrypoint"),),now=now)
    codes={reason.code for reason in plan.nodes[0].blockers}
    assert {"run.insufficient_memory","topology.runtime_capability_missing","run.port_occupied"} <= codes


def test_accept_rechecks_memory_reservations_while_holding_node_lock(tmp_path) -> None:
    sessions, now, node, installation = setup(tmp_path, free_memory=300)
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    plan = service.plan_run(
        installation, (Placement(node, 0, "entrypoint"),), now=now
    )
    with sessions.begin() as session:
        session.add_all(
            [
                ResourceReservation(
                    node_id=node,
                    kind=kind,
                    resource_key="concurrent",
                    amount_bytes=50,
                    owner_kind="run",
                    owner_id="2" * 36,
                    state="active",
                    plan_digest="d" * 64,
                    created_at=now,
                )
                for kind in ("host-memory", "gpu-memory")
            ]
        )
    service.plan_run = lambda *args, **kwargs: plan

    with pytest.raises(RunPlanConflict, match="memory capacity changed"):
        service.accept_run(plan, alias="qwen", actor="admin", now=now)
