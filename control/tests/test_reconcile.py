from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from dgx_control.git_policy import Eligibility
from dgx_control.models import Base, Reconciliation
from dgx_control.orchestration import (
    OperationGraph,
    OperationNode,
    ReconciliationOrchestrator,
)
from dgx_control.reconcile import (
    CompatibilityDefinitions,
    IneligibleCommit,
    Reconciler,
    RepositoryDefinitions,
    resolved_reconciliation_plan,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


class Policy:
    def __init__(self): self.allowed = True
    def eligible(self, commit): return Eligibility(commit, self.allowed, () if self.allowed else ("check revoked",))


class Routes:
    def __init__(self): self.state = {}; self.published = []
    def withdraw(self, targets):
        for target in targets: self.state[target] = "maintenance"
    def publish_atomically(self, routes):
        self.published.append(routes)
        for target in routes: self.state[target] = "published"


class Controller:
    def __init__(self, fail=False): self.calls = []; self.fail = fail
    def apply(self, plan):
        self.calls.append(("apply", plan.targets))
        if self.fail: raise RuntimeError("start failed")
    def verify(self, plan): self.calls.append(("verify", plan.targets)); return True


class Leases:
    def __init__(self): self.acquired = []
    @contextmanager
    def acquire(self, targets):
        self.acquired.append(targets); yield


def definitions(_commit):
    return {"targets": ["spk_b", "spk_a"], "placements": {"entry": ["spk_a"]}, "routes": {"spk_a": "entry"}, "releases": {"spk_a": "sha256:abc"}, "input_digests": {"fleet": "f" * 64}}


def compatibility():
    return CompatibilityDefinitions(definitions)


def test_reconcile_rechecks_commit_eligibility_before_mutation() -> None:
    policy, routes, controller, leases = Policy(), Routes(), Controller(), Leases()
    reconciler = Reconciler(policy, compatibility(), routes, controller, leases)
    plan = reconciler.plan("a" * 40)
    policy.allowed = False
    with pytest.raises(IneligibleCommit, match="check revoked"):
        reconciler.execute(plan)
    assert controller.calls == [] and routes.state == {} and leases.acquired == []


def test_failed_reconcile_leaves_affected_routes_withdrawn() -> None:
    policy, routes, controller, leases = Policy(), Routes(), Controller(fail=True), Leases()
    reconciler = Reconciler(policy, compatibility(), routes, controller, leases)
    result = reconciler.execute(reconciler.plan("a" * 40))
    assert result.status == "failed"
    assert routes.state == {"spk_a": "maintenance", "spk_b": "maintenance"}
    assert routes.published == []
    assert leases.acquired == [("spk_a", "spk_b")]


def test_reconcile_does_not_mask_unexpected_programming_error() -> None:
    class BrokenController(Controller):
        def apply(self, plan):
            raise AssertionError("programming defect")

    reconciler = Reconciler(Policy(), compatibility(), Routes(), BrokenController(), Leases())

    with pytest.raises(AssertionError, match="programming defect"):
        reconciler.execute(reconciler.plan("a" * 40))


def test_successful_plan_is_deterministic_and_publishes_atomically() -> None:
    policy, routes, controller, leases = Policy(), Routes(), Controller(), Leases()
    reconciler = Reconciler(policy, compatibility(), routes, controller, leases)
    first = reconciler.plan("a" * 40)
    second = reconciler.plan("a" * 40)
    assert first == second and first.targets == ("spk_a", "spk_b")
    result = reconciler.execute(first)
    assert result.status == "succeeded"
    assert routes.published == [{"spk_a": "entry"}]


class Jobs:
    def __init__(self): self.call = None
    def enqueue(self, *args, **kwargs):
        self.call = (args, kwargs)
        return type("Job", (), {"id": "job", "state": "queued"})()


def test_enqueue_pins_plan_commit_and_digest() -> None:
    jobs = Jobs()
    reconciler = Reconciler(Policy(), compatibility(), Routes(), Controller(), Leases(), jobs=jobs)
    plan = reconciler.plan("a" * 40)
    result = reconciler.enqueue(plan.digest, "operator", "request")
    assert result == {"job_id": "job", "state": "queued", "base_commit": "a" * 40}
    assert jobs.call[0][2] == "a" * 40
    assert jobs.call[0][4] == {
        "input_digests": {"fleet": "f" * 64},
        "placements": {"entry": ["spk_a"]},
        "plan_digest": plan.digest,
        "releases": {"spk_a": "sha256:abc"},
        "routes": {"spk_a": "entry"},
        "workload_groups": {},
    }


def test_repository_definitions_reads_commit_pinned_document() -> None:
    class Repository:
        def read_document(self, commit, path):
            assert commit == "a" * 40
            assert path == "inventory/reconciliation.json"
            return type("Document", (), {"parsed": definitions(commit)})()

    assert RepositoryDefinitions(Repository())("a" * 40) == definitions("a" * 40)


def test_static_reconciliation_requires_explicit_compatibility_adapter() -> None:
    with pytest.raises(TypeError, match="planner"):
        Reconciler(Policy(), definitions)


def test_reconciliation_mapping_shapes_raise_type_error() -> None:
    class InvalidRepository:
        def read_document(self, commit, path):
            return type("Document", (), {"parsed": []})()

    with pytest.raises(TypeError, match="JSON object"):
        RepositoryDefinitions(InvalidRepository())("a" * 40)

    reconciler = Reconciler(
        Policy(),
        CompatibilityDefinitions(
            lambda _commit: definitions(_commit) | {"placements": []}
        ),
    )
    with pytest.raises(TypeError, match="placements"):
        reconciler.plan("a" * 40)


def test_planning_only_reconciler_cannot_execute_in_api_process() -> None:
    reconciler = Reconciler(Policy(), compatibility(), jobs=Jobs())
    plan = reconciler.plan("a" * 40)
    with pytest.raises(RuntimeError, match="worker"):
        reconciler.execute(plan)


class DesiredPlanner:
    def resolve(self, commit, profile_id, observations):
        assert profile_id == "inference"
        assert tuple(observations) == ("durable",)
        node_id = "spk_" + "1" * 32
        operation = OperationNode(
            "model:probe",
            node_id,
            "model",
            "node.probe",
            (),
            None,
            "b" * 64,
        )
        graph = OperationGraph(
            "pending",
            commit,
            (node_id,),
            (operation,),
            "c" * 64,
        )
        return resolved_reconciliation_plan(
            commit=commit,
            targets=(node_id,),
            placements={"model": (node_id,)},
            routes={},
            releases={},
            workload_groups={},
            input_digests={"fleet": "f" * 64},
            operation_graph=graph,
            operation_payloads={"model:probe": {}},
            agent_protocol_range=(1, 1),
        )


def test_resolved_plan_digest_survives_process_restart(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    clock = lambda: datetime(2026, 8, 5, tzinfo=UTC)
    first = Reconciler(
        Policy(),
        DesiredPlanner(),
        jobs=Jobs(),
        observations=lambda: ("durable",),
        orchestrator=ReconciliationOrchestrator(sessions, clock=clock),
    )

    planned = first.plan("a" * 40, "inference")
    assert planned.operation_graph.reconciliation_id != "pending"
    with sessions() as session:
        stored = session.scalar(
            select(Reconciliation).where(
                Reconciliation.plan_digest == planned.digest
            )
        )
        assert stored is not None
        assert stored.resolved_plan["operation_graph"] == stored.graph

    jobs = Jobs()
    restarted = Reconciler(
        Policy(),
        DesiredPlanner(),
        jobs=jobs,
        observations=lambda: ("durable",),
        orchestrator=ReconciliationOrchestrator(sessions, clock=clock),
    )
    result = restarted.enqueue(planned.digest, "operator", "request")

    assert result["reconciliation_id"] == planned.operation_graph.reconciliation_id
    assert jobs.call[0][4]["reconciliation_id"] == planned.operation_graph.reconciliation_id
