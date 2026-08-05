from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from dgx_control.models import Base, Reconciliation
from dgx_control.orchestration import ReconciliationOrchestrator
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
BASE_COMMIT = "a" * 40


def distributed_plan() -> dict[str, object]:
    return {
        "base_commit": BASE_COMMIT,
        "targets": [NODE_B, NODE_A],
        "route_withdrawal_generation": 3,
        "operations": [
            {
                "operation_id": "worker:stop",
                "node_id": NODE_B,
                "workload_id": "model-a",
                "kind": "workload.stop",
                "dependencies": ["head:stop"],
                "compensation_kind": None,
                "payload_digest": "4" * 64,
            },
            {
                "operation_id": "head:start",
                "node_id": NODE_A,
                "workload_id": "model-a",
                "kind": "workload.start",
                "dependencies": ["worker:start"],
                "compensation_kind": "workload.stop",
                "payload_digest": "2" * 64,
            },
            {
                "operation_id": "worker:start",
                "node_id": NODE_B,
                "workload_id": "model-a",
                "kind": "workload.start",
                "dependencies": [],
                "compensation_kind": "workload.stop",
                "payload_digest": "1" * 64,
            },
            {
                "operation_id": "head:stop",
                "node_id": NODE_A,
                "workload_id": "model-a",
                "kind": "workload.stop",
                "dependencies": ["head:start"],
                "compensation_kind": None,
                "payload_digest": "3" * 64,
            },
        ],
    }


@pytest.fixture
def planner(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orchestration.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    orchestrator = ReconciliationOrchestrator(
        sessions,
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )
    return orchestrator, sessions


def test_graph_is_dependency_ordered_and_digest_stable(planner) -> None:
    orchestrator, _ = planner

    graph = orchestrator.plan(distributed_plan())
    repeated = orchestrator.plan(distributed_plan())

    assert graph.dependencies("head:start") == ("worker:start",)
    assert graph.dependencies("worker:stop") == ("head:stop",)
    assert tuple(node.operation_id for node in graph.nodes) == (
        "worker:start",
        "head:start",
        "head:stop",
        "worker:stop",
    )
    assert graph.targets == (NODE_A, NODE_B)
    assert graph.digest == repeated.digest == (
        "def0e95a03404d2efb9ad6ab53ce2dbc8040d6cc0d40c337a533783b0cad3317"
    )
    assert graph.document == repeated.document


def test_independent_nodes_are_sorted_by_canonical_operation_id(planner) -> None:
    orchestrator, _ = planner
    document = distributed_plan()
    document["operations"] = [
        {
            "operation_id": "z:probe",
            "node_id": NODE_B,
            "workload_id": "model-a",
            "kind": "node.probe",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": "6" * 64,
        },
        {
            "operation_id": "a:probe",
            "node_id": NODE_A,
            "workload_id": "model-a",
            "kind": "node.probe",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": "5" * 64,
        },
    ]

    graph = orchestrator.plan(document)

    assert tuple(node.operation_id for node in graph.nodes) == (
        "a:probe",
        "z:probe",
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("cycle", "cycle"),
        ("unknown-target", "target"),
        ("duplicate-operation", "duplicate"),
        ("unknown-dependency", "dependency"),
        ("cross-workload", "workload"),
        ("unsupported-kind", "operation kind"),
        ("unsupported-compensation", "compensation"),
    ],
)
def test_invalid_graphs_are_rejected_without_persistence(
    planner, mutation: str, message: str
) -> None:
    orchestrator, sessions = planner
    document = distributed_plan()
    operations = document["operations"]
    assert isinstance(operations, list)
    by_id = {item["operation_id"]: item for item in operations}
    if mutation == "cycle":
        by_id["worker:start"]["dependencies"] = ["worker:stop"]
    elif mutation == "unknown-target":
        by_id["worker:start"]["node_id"] = "spk_" + "c" * 32
    elif mutation == "duplicate-operation":
        operations.append(deepcopy(by_id["worker:start"]))
    elif mutation == "unknown-dependency":
        by_id["head:start"]["dependencies"] = ["missing:start"]
    elif mutation == "cross-workload":
        by_id["head:start"]["workload_id"] = "model-b"
    elif mutation == "unsupported-kind":
        by_id["worker:start"]["kind"] = "system.exec"
    else:
        by_id["worker:start"]["compensation_kind"] = "system.exec"

    with pytest.raises(ValueError, match=message):
        orchestrator.plan(document)

    with sessions() as session:
        assert session.scalars(select(Reconciliation)).all() == []


def test_plan_persists_immutable_canonical_graph_and_progress_fields(planner) -> None:
    orchestrator, sessions = planner

    graph = orchestrator.plan(distributed_plan())

    with sessions() as session:
        stored = session.get(Reconciliation, graph.reconciliation_id)
        assert stored is not None
        assert stored.base_commit == BASE_COMMIT
        assert stored.graph == graph.document
        assert stored.graph_digest == graph.digest
        assert stored.status == "planned"
        assert stored.current_phase == "planned"
        assert stored.route_withdrawal_generation == 3
        assert stored.terminal_reason is None
        assert stored.summary == {
            "operation_count": 4,
            "target_count": 2,
        }


def test_advance_and_cancel_change_only_mutable_reconciliation_state(planner) -> None:
    orchestrator, sessions = planner
    graph = orchestrator.plan(distributed_plan())
    original_document = deepcopy(graph.document)

    orchestrator.advance(
        graph.reconciliation_id,
        "routes-withdrawn",
        route_withdrawal_generation=4,
    )
    orchestrator.advance(graph.reconciliation_id, "dispatching")

    with sessions() as session:
        stored = session.get(Reconciliation, graph.reconciliation_id)
        assert stored is not None
        assert stored.status == "running"
        assert stored.current_phase == "dispatching"
        assert stored.route_withdrawal_generation == 4
        assert stored.graph == original_document
        assert stored.graph_digest == graph.digest

    with pytest.raises(ValueError, match="transition"):
        orchestrator.advance(graph.reconciliation_id, "completed")

    orchestrator.cancel(graph.reconciliation_id, "operator cancelled rollout")

    with sessions() as session:
        stored = session.get(Reconciliation, graph.reconciliation_id)
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.current_phase == "cancelled"
        assert stored.terminal_reason == "operator cancelled rollout"
        assert stored.graph == original_document
        assert stored.graph_digest == graph.digest

    with pytest.raises(ValueError, match="terminal"):
        orchestrator.advance(graph.reconciliation_id, "accepting")
