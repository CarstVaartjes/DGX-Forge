from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from dgx_agent_protocol import canonical_message
from dgx_control.agent_jobs import AgentJobService, StaleAgentAttempt
from dgx_control.agent_reconciliation import (
    AgentReconciliationService,
    accepted_result_digests,
    bind_reconciliation_result_consumer,
    compensation_order,
    ready_operation_ids,
)
from dgx_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
    Reconciliation,
    ReconciliationOperation,
    RoutePublication,
)
from dgx_control.orchestration import OperationNode
from dgx_control.route_runtime import ActivationMarker
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
BASE_COMMIT = "a" * 40


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _json_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _node(
    operation_id: str,
    kind: str,
    dependencies: tuple[str, ...] = (),
    *,
    node_id: str = NODE_A,
    workload_id: str = "model",
    compensation_kind: str | None = None,
    payload: dict[str, object] | None = None,
) -> OperationNode:
    payload = {} if payload is None else payload
    return OperationNode(
        operation_id,
        node_id,
        workload_id,
        kind,
        dependencies,
        compensation_kind,
        _digest(payload),
    )


def test_dependency_waves_trust_only_accepted_projection_rows() -> None:
    nodes = (
        _node("worker:start", "workload.start", compensation_kind="workload.stop"),
        _node(
            "entrypoint:start",
            "workload.start",
            ("worker:start",),
            node_id=NODE_B,
            compensation_kind="workload.stop",
        ),
        _node(
            "entrypoint:verify",
            "workload.verify",
            ("entrypoint:start",),
            node_id=NODE_B,
        ),
    )

    assert ready_operation_ids(nodes, {}) == ("worker:start",)
    assert ready_operation_ids(nodes, {"worker:start": "planned"}) == (
        "worker:start",
    )
    assert ready_operation_ids(nodes, {"worker:start": "succeeded"}) == ()
    assert ready_operation_ids(nodes, {"worker:start": "accepted"}) == (
        "entrypoint:start",
    )
    assert ready_operation_ids(
        nodes,
        {"worker:start": "accepted", "entrypoint:start": "accepted"},
    ) == ("entrypoint:verify",)


def test_ready_wave_is_deterministic_for_sixteen_independent_nodes() -> None:
    nodes = tuple(
        _node(
            f"worker-{index:02d}:prepare",
            "workload.prepare",
            node_id="spk_" + f"{index:032x}",
        )
        for index in reversed(range(16))
    )

    assert ready_operation_ids(nodes, {}) == tuple(
        f"worker-{index:02d}:prepare" for index in range(16)
    )


def test_compensation_reverses_graph_order_for_accepted_starts_only() -> None:
    nodes = (
        _node("worker:start", "workload.start", compensation_kind="workload.stop"),
        _node(
            "entrypoint:start",
            "workload.start",
            ("worker:start",),
            node_id=NODE_B,
            compensation_kind="workload.stop",
        ),
        _node("entrypoint:health", "workload.health", ("entrypoint:start",)),
    )

    assert compensation_order(
        nodes,
        {"worker:start": "accepted", "entrypoint:start": "accepted"},
    ) == ("entrypoint:start", "worker:start")
    assert compensation_order(nodes, {"worker:start": "succeeded"}) == ()


def test_release_evidence_is_bound_to_the_exact_request() -> None:
    payload = {
        "schema_version": 1,
        "target_name": "model",
        "oci_manifest_digest": "sha256:" + "9" * 64,
        "target_digest": "a" * 64,
        "provenance_digest": "b" * 64,
        "adapter_id": "spark-runtime-v1",
    }
    result = {
        "status": "ok",
        "evidence": {
            "status": "installed",
            "release_digest": "a" * 64,
            "manifest_digest": "sha256:" + "9" * 64,
            "adapter_id": "spark-runtime-v1",
        },
    }

    result_digest, evidence_digest = accepted_result_digests(
        "release.install", payload, result
    )

    assert result_digest == _digest(result)
    assert evidence_digest == _digest(result["evidence"])
    bad = dict(result)
    bad["evidence"] = dict(result["evidence"], adapter_id="other")
    with pytest.raises(ValueError, match="release evidence"):
        accepted_result_digests("release.install", payload, bad)


@pytest.mark.parametrize(
    ("kind", "extra", "action"),
    (
        ("workload.prepare", {"profile_digest": "c" * 64}, "prepare"),
        ("workload.start", {"preparation_digest": "d" * 64}, "start"),
        ("workload.stop", {}, "stop"),
        ("workload.health", {}, "health"),
        ("workload.verify", {"expected_digest": "e" * 64}, "verify"),
    ),
)
def test_workload_evidence_binds_action_identity_release_and_verify_digest(
    kind: str, extra: dict[str, object], action: str
) -> None:
    payload = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "spark-runtime-v1",
    } | extra
    result = {
        "status": "ok",
        "evidence": {
            "status": "healthy" if action == "health" else "completed",
            "action": action,
            "workload_id": "model",
            "release_digest": "a" * 64,
            "evidence_digest": "e" * 64,
        },
    }

    accepted_result_digests(kind, payload, result)

    for field, value in (
        ("action", "start" if action != "start" else "stop"),
        ("workload_id", "other"),
        ("release_digest", "f" * 64),
    ):
        bad = dict(result)
        bad["evidence"] = dict(result["evidence"], **{field: value})
        with pytest.raises(ValueError, match="workload evidence"):
            accepted_result_digests(kind, payload, bad)

    if kind == "workload.verify":
        bad = dict(result)
        bad["evidence"] = dict(result["evidence"], evidence_digest="f" * 64)
        with pytest.raises(ValueError, match="verify"):
            accepted_result_digests(kind, payload, bad)


def test_node_gate_requires_exact_zero_compute_evidence() -> None:
    payload = {"require_active_nvidia_compute_processes": 0}
    result = {
        "status": "ok",
        "evidence": {
            "dgx_forge": {
                "schema_version": 1,
                "accelerator": {"active_nvidia_compute_processes": 0},
            },
            "nvidia": {"tools": {}},
        },
    }

    accepted_result_digests("node.probe", payload, result)
    result["evidence"]["dgx_forge"]["accelerator"][
        "active_nvidia_compute_processes"
    ] = 1
    with pytest.raises(ValueError, match="compute gate"):
        accepted_result_digests("node.probe", payload, result)


@pytest.mark.parametrize(
    "result",
    (
        {},
        {"status": "failed", "error_code": "workload_failed"},
        {"status": "ok"},
        {"status": "ok", "evidence": []},
    ),
)
def test_only_canonical_success_evidence_can_be_accepted(result: object) -> None:
    with pytest.raises((TypeError, ValueError), match="result|evidence"):
        accepted_result_digests("workload.stop", {}, result)


class FakeAgentJobs:
    def __init__(self) -> None:
        self.notifications = 0

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
        stored = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest=_digest(payload),
            payload=dict(payload),
            base_commit=base_commit,
            state="queued",
            current_attempt=0,
            created_at=datetime(2026, 8, 5, tzinfo=UTC),
            updated_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        session.add(stored)
        session.flush()
        return stored

    def notify_available(self) -> None:
        self.notifications += 1


class FakePublisher:
    def __init__(self, *, fail_withdrawal: bool = False) -> None:
        self.fail_withdrawal = fail_withdrawal
        self.withdrawals = 0
        self.publications = []

    def withdraw(self, *, reconciliation_id, plan_digest, targets, reason):
        self.withdrawals += 1
        if self.fail_withdrawal:
            raise RuntimeError("withdrawal failed")
        return ActivationMarker(
            schema_version=1,
            generation=1,
            state="maintenance",
            reconciliation_id=reconciliation_id,
            plan_digest=plan_digest,
            evidence_set_digest="0" * 64,
            routes_sha256="1" * 64,
            litellm_sha256="2" * 64,
            issued_at="2026-08-05T00:00:00+00:00",
            expires_at="2026-08-05T00:05:00+00:00",
            directory="00000001-" + "3" * 64,
            manifest_sha256="3" * 64,
        )

    def publish(self, request):
        self.publications.append(request)
        return ActivationMarker(
            schema_version=1,
            generation=2,
            state="published",
            reconciliation_id=request.reconciliation_id,
            plan_digest=request.plan_digest,
            evidence_set_digest=request.evidence_set_digest,
            routes_sha256="4" * 64,
            litellm_sha256="5" * 64,
            issued_at="2026-08-05T00:00:00+00:00",
            expires_at="2026-08-05T00:01:00+00:00",
            directory="00000002-" + "6" * 64,
            manifest_sha256="6" * 64,
        )


def _execution_fixture(
    tmp_path,
    *,
    fail_withdrawal: bool = False,
    real_queue: bool = False,
    operation_kind: str = "workload.health",
    routes: dict[str, object] | None = None,
    attach_job: bool = True,
    clock=None,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'execution-{uuid.uuid4()}.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    payload: dict[str, object] = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "spark-runtime-v1",
    }
    if operation_kind == "workload.verify":
        payload["expected_digest"] = "e" * 64
    operation = {
        "operation_id": f"model:{NODE_A}:{operation_kind}",
        "node_id": NODE_A,
        "workload_id": "model",
        "kind": operation_kind,
        "dependencies": [],
        "compensation_kind": None,
        "payload_digest": _digest(payload),
    }
    graph = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "targets": [NODE_A],
        "nodes": [operation],
    }
    resolved = {
        "commit": BASE_COMMIT,
        "targets": [NODE_A],
        "placements": {},
        "routes": routes or {},
        "releases": {},
        "workload_groups": {},
        "input_digests": {"fleet": "f" * 64},
        "operation_graph": graph,
        "operation_payloads": {operation["operation_id"]: payload},
        "agent_protocol_range": [1, 1],
    }
    reconciliation_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    now = datetime(2026, 8, 5, tzinfo=UTC)
    clock = clock or (lambda: now)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
        session.add(
            AgentCertificate(
                serial="serial-a",
                node_id=NODE_A,
                not_before=now - timedelta(minutes=1),
                not_after=now + timedelta(hours=1),
                fingerprint="fingerprint-a",
            )
        )
        session.add(
            Reconciliation(
                id=reconciliation_id,
                base_commit=BASE_COMMIT,
                status="planned",
                summary={},
                graph=graph,
                graph_digest=_json_digest(graph),
                plan_digest=_json_digest(resolved),
                resolved_plan=resolved,
                current_phase="planned",
                route_withdrawal_generation=0,
                created_at=now,
            )
        )
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="reconcile",
                state="queued",
                actor="operator",
                base_commit=BASE_COMMIT,
                targets=[NODE_A],
                payload_digest=_digest({}),
                payload={},
                current_attempt=0,
                created_at=now,
                updated_at=now,
                reconciliation_id=None if attach_job else reconciliation_id,
            )
        )
    queue = (
        AgentJobService(sessions, clock=clock)
        if real_queue
        else FakeAgentJobs()
    )
    publisher = FakePublisher(fail_withdrawal=fail_withdrawal)
    service = AgentReconciliationService(
        sessions,
        agent_jobs=queue,
        publisher=publisher,
        endpoint_resolver=lambda _session, _node: ("192.0.2.10", now),
        clock=clock,
    )
    if attach_job:
        service.attach_job(reconciliation_id, job_id)
    if real_queue:
        queue.set_result_consumer(service.consume_result)
    return service, sessions, queue, publisher, reconciliation_id, job_id


def test_withdrawal_is_durable_before_any_agent_operation(tmp_path) -> None:
    service, sessions, queue, publisher, reconciliation_id, job_id = (
        _execution_fixture(tmp_path)
    )

    assert service.tick(reconciliation_id) is True
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        assert stored is not None and stored.current_phase == "withdrawal-pending"
        assert session.scalar(select(func.count()).select_from(AgentOperation)) == 0

    assert service.tick(reconciliation_id) is True
    assert publisher.withdrawals == 1
    assert service.tick(reconciliation_id) is True
    assert service.tick(reconciliation_id) is True

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        operations = list(session.scalars(select(AgentOperation)))
        projections = list(session.scalars(select(ReconciliationOperation)))
        assert stored is not None and stored.current_phase == "dispatching"
        assert job is not None and job.state == "running"
        assert len(operations) == len(projections) == 1
        assert projections[0].agent_operation_id == operations[0].id
        assert projections[0].state == "queued"
    assert queue.notifications == 1


def test_withdrawal_failure_inserts_zero_agent_operations(tmp_path) -> None:
    service, sessions, _queue, publisher, reconciliation_id, _job_id = (
        _execution_fixture(tmp_path, fail_withdrawal=True)
    )
    service.tick(reconciliation_id)

    with pytest.raises(RuntimeError, match="withdrawal"):
        service.tick(reconciliation_id)

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        assert stored is not None and stored.current_phase == "withdrawal-pending"
        assert session.scalar(select(func.count()).select_from(AgentOperation)) == 0
    assert publisher.withdrawals == 1


@pytest.mark.parametrize("target_state", ["inactive", "revoked", "missing"])
def test_withdrawal_precedes_inactive_target_rejection(
    tmp_path, target_state: str
) -> None:
    service, sessions, _queue, publisher, reconciliation_id, job_id = (
        _execution_fixture(tmp_path)
    )
    service.tick(reconciliation_id)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        if target_state == "missing":
            certificate = session.get(AgentCertificate, "serial-a")
            assert certificate is not None
            session.delete(certificate)
            session.flush()
            session.delete(node)
        elif target_state == "inactive":
            node.state = "retired"
        else:
            node.revoked_at = datetime(2026, 8, 5, tzinfo=UTC)

    assert service.tick(reconciliation_id) is True

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and stored.current_phase == "routes-withdrawn"
        assert job is not None and job.state == "running"
    assert publisher.withdrawals == 1


def test_production_linked_job_enters_running_without_attach(tmp_path) -> None:
    service, sessions, _queue, _publisher, reconciliation_id, job_id = (
        _execution_fixture(tmp_path, attach_job=False)
    )

    assert service.tick(reconciliation_id) is True

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and stored.current_phase == "withdrawal-pending"
        assert job is not None and job.state == "running"


def test_unsafe_mutation_expiry_projects_waiting_for_operator(tmp_path) -> None:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    current = [now]
    service, sessions, queue, _publisher, reconciliation_id, job_id = (
        _execution_fixture(
            tmp_path,
            real_queue=True,
            operation_kind="workload.start",
            clock=lambda: current[0],
        )
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    claim = queue.claim(NODE_A, "serial-a", 30)
    assert claim is not None

    current[0] += timedelta(seconds=30)
    assert queue.claim(NODE_A, "serial-a", 30) is None
    service.request_cancel(reconciliation_id, "operator cancelled after expiry")

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        projection = session.scalar(select(ReconciliationOperation))
        assert stored is not None
        assert stored.current_phase == "waiting-for-operator"
        assert stored.status == "failed"
        assert job is not None and job.state == "waiting-for-operator"
        assert operation is not None and operation.state == "waiting-for-operator"
        assert attempt is not None and attempt.state == "expired"
        assert projection is not None and projection.state == "waiting-for-operator"


def test_concurrent_ticks_enqueue_one_exact_operation(tmp_path) -> None:
    service, sessions, queue, _publisher, reconciliation_id, _job_id = (
        _execution_fixture(tmp_path)
    )
    for _ in range(3):
        service.tick(reconciliation_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: service.tick(reconciliation_id), range(4)))

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentOperation)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ReconciliationOperation))
            == 1
        )
    assert queue.notifications == 1


def _health_result(*, action: str = "health") -> dict[str, object]:
    return {
        "status": "ok",
        "evidence": {
            "status": "healthy",
            "action": action,
            "workload_id": "model",
            "release_digest": "a" * 64,
            "evidence_digest": "e" * 64,
        },
    }


def test_first_completed_wave_is_accepted_without_terminalizing_parent(tmp_path) -> None:
    service, sessions, queue, _publisher, reconciliation_id, job_id = (
        _execution_fixture(tmp_path, real_queue=True)
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    claim = queue.claim(NODE_A, "serial-a", 30)
    assert claim is not None

    queue.succeed(claim, _health_result())

    with sessions() as session:
        job = session.get(Job, job_id)
        projection = session.scalar(select(ReconciliationOperation))
        assert job is not None and job.state == "running"
        assert projection is not None and projection.state == "accepted"
        assert projection.result_digest == _digest(_health_result())
        assert projection.evidence_digest == _digest(_health_result()["evidence"])

    service.tick(reconciliation_id)
    service.tick(reconciliation_id)
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and stored.current_phase == "publication-pending"
        assert job is not None and job.state == "running"


def test_bad_evidence_rolls_back_agent_and_projection_result_atomically(tmp_path) -> None:
    service, sessions, queue, _publisher, reconciliation_id, _job_id = (
        _execution_fixture(tmp_path, real_queue=True)
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    claim = queue.claim(NODE_A, "serial-a", 30)
    assert claim is not None

    with pytest.raises(ValueError, match="workload evidence"):
        queue.succeed(claim, _health_result(action="start"))

    with sessions() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        projection = session.scalar(select(ReconciliationOperation))
        assert operation is not None and operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.result is None
        assert projection is not None and projection.state == "queued"


def test_complete_graph_publishes_exact_bundle_then_terminalizes_parent(tmp_path) -> None:
    quota = {"requests_per_minute": 20, "tokens_per_minute": 1000}
    route = {
        "workload_id": "model",
        "nodes": [NODE_A],
        "entrypoint_node_id": NODE_A,
        "scheme": "http",
        "port": 8000,
        "path": "/v1",
        "quota": quota,
        "quota_digest": _json_digest(quota),
    }
    service, sessions, queue, publisher, reconciliation_id, job_id = (
        _execution_fixture(
            tmp_path,
            real_queue=True,
            operation_kind="workload.verify",
            routes={"model": route},
        )
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    claim = queue.claim(NODE_A, "serial-a", 30)
    assert claim is not None
    queue.succeed(claim, _health_result(action="verify"))

    for _ in range(3):
        service.tick(reconciliation_id)

    assert len(publisher.publications) == 1
    request = publisher.publications[0]
    assert request.reconciliation_id == reconciliation_id
    assert set(request.endpoints) == {NODE_A}
    assert request.endpoints[NODE_A].operation_id == (
        f"model:{NODE_A}:workload.verify"
    )
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and stored.current_phase == "completed"
        assert stored.status == "succeeded"
        assert stored.completion_generation == 1
        assert job is not None and job.state == "succeeded"
        assert job.result == {
            "reconciliation_id": reconciliation_id,
            "plan_digest": stored.plan_digest,
            "bundle_digest": "6" * 64,
        }

    assert service.tick(reconciliation_id) is True
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and stored.current_phase == "accepting"
        assert job is not None and job.state == "running"
    assert publisher.withdrawals == 2

    service.tick(reconciliation_id)
    service.tick(reconciliation_id)
    assert len(publisher.publications) == 2
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        assert stored is not None and stored.current_phase == "completed"
        assert stored.completion_generation == 1


def _compensation_fixture(tmp_path, *, parallel_starts: bool = False):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'compensation.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 5, tzinfo=UTC)
    payload = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "spark-runtime-v1",
        "preparation_digest": "d" * 64,
    }
    worker_id = f"model:{NODE_A}:workload.start"
    entry_id = f"model:{NODE_B}:workload.start"
    operations = [
        {
            "operation_id": worker_id,
            "node_id": NODE_A,
            "workload_id": "model",
            "kind": "workload.start",
            "dependencies": [],
            "compensation_kind": "workload.stop",
            "payload_digest": _digest(payload),
        },
        {
            "operation_id": entry_id,
            "node_id": NODE_B,
            "workload_id": "model",
            "kind": "workload.start",
            "dependencies": [] if parallel_starts else [worker_id],
            "compensation_kind": "workload.stop",
            "payload_digest": _digest(payload),
        },
    ]
    graph = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "targets": [NODE_A, NODE_B],
        "nodes": operations,
    }
    resolved = {
        "commit": BASE_COMMIT,
        "targets": [NODE_A, NODE_B],
        "placements": {},
        "routes": {},
        "releases": {},
        "workload_groups": {},
        "input_digests": {"fleet": "f" * 64},
        "operation_graph": graph,
        "operation_payloads": {worker_id: payload, entry_id: payload},
        "agent_protocol_range": [1, 1],
    }
    reconciliation_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=now - timedelta(minutes=1),
                    not_after=now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
        session.add(
            Reconciliation(
                id=reconciliation_id,
                base_commit=BASE_COMMIT,
                status="planned",
                summary={},
                graph=graph,
                graph_digest=_json_digest(graph),
                plan_digest=_json_digest(resolved),
                resolved_plan=resolved,
                current_phase="planned",
                route_withdrawal_generation=0,
                created_at=now,
            )
        )
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="reconcile",
                state="queued",
                actor="operator",
                base_commit=BASE_COMMIT,
                targets=[NODE_A, NODE_B],
                payload_digest=_digest({}),
                payload={},
                current_attempt=0,
                created_at=now,
                updated_at=now,
            )
        )
    queue = AgentJobService(sessions, clock=lambda: now)
    service = AgentReconciliationService(
        sessions,
        agent_jobs=queue,
        publisher=FakePublisher(),
        endpoint_resolver=lambda _session, _node: ("192.0.2.10", now),
        clock=lambda: now,
    )
    service.attach_job(reconciliation_id, job_id)
    queue.set_result_consumer(service.consume_result)
    return service, sessions, queue, reconciliation_id, job_id


def _workload_result(action: str) -> dict[str, object]:
    return {
        "status": "ok",
        "evidence": {
            "status": "completed",
            "action": action,
            "workload_id": "model",
            "release_digest": "a" * 64,
            "evidence_digest": "e" * 64,
        },
    }


def test_partial_start_failure_compensates_accepted_starts_in_reverse_order(
    tmp_path,
) -> None:
    service, sessions, queue, reconciliation_id, job_id = _compensation_fixture(
        tmp_path
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    worker = queue.claim(NODE_A, "serial-a", 30)
    assert worker is not None and worker.operation.value == "workload.start"
    queue.succeed(worker, _workload_result("start"))
    service.tick(reconciliation_id)
    entrypoint = queue.claim(NODE_B, "serial-b", 30)
    assert entrypoint is not None and entrypoint.operation.value == "workload.start"

    queue.fail(entrypoint, "start-failed")

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and stored.current_phase == "compensating"
        assert job is not None and job.state == "running"

    service.tick(reconciliation_id)
    compensation = queue.claim(NODE_A, "serial-a", 30)
    assert compensation is not None
    assert compensation.operation.value == "workload.stop"
    assert compensation.payload == {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "spark-runtime-v1",
    }
    queue.succeed(compensation, _workload_result("stop"))
    service.tick(reconciliation_id)

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        compensations = list(
            session.scalars(
                select(ReconciliationOperation).where(
                    ReconciliationOperation.role == "compensation"
                )
            )
        )
        assert stored is not None and stored.current_phase == "failed"
        assert job is not None and job.state == "failed"
        assert len(compensations) == 1
        assert compensations[0].state == "compensated"
        assert compensations[0].graph_operation_id == (
            f"model:{NODE_A}:workload.start"
        )


def test_cancellation_after_mutation_enters_compensation(tmp_path) -> None:
    service, sessions, queue, reconciliation_id, job_id = _compensation_fixture(
        tmp_path
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    worker = queue.claim(NODE_A, "serial-a", 30)
    assert worker is not None
    queue.succeed(worker, _workload_result("start"))

    service.request_cancel(reconciliation_id, "operator cancelled")

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and stored.current_phase == "compensating"
        assert stored.status == "running"
        assert job is not None and job.state == "running"


def test_cancellation_quiesces_an_in_flight_mutation(tmp_path) -> None:
    service, sessions, queue, reconciliation_id, job_id = _compensation_fixture(
        tmp_path
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    claim = queue.claim(NODE_A, "serial-a", 30)
    assert claim is not None

    service.request_cancel(reconciliation_id, "operator cancelled")
    service.request_cancel(reconciliation_id, "operator cancelled again")

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == claim.operation_id
            )
        )
        assert stored is not None
        assert stored.current_phase == "waiting-for-operator"
        assert job is not None and job.state == "waiting-for-operator"
        assert operation is not None and operation.state == "waiting-for-operator"
        assert attempt is not None and attempt.state == "waiting-for-operator"
        assert projection is not None and projection.state == "waiting-for-operator"
    with pytest.raises(StaleAgentAttempt):
        queue.succeed(claim, _workload_result("start"))


def test_terminal_phase_rejects_late_primary_result_atomically(tmp_path) -> None:
    service, sessions, queue, _publisher, reconciliation_id, job_id = (
        _execution_fixture(tmp_path, real_queue=True)
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    claim = queue.claim(NODE_A, "serial-a", 30)
    assert claim is not None
    with sessions.begin() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and job is not None
        stored.current_phase = "failed"
        stored.status = "failed"
        job.state = "failed"

    with pytest.raises(ValueError, match="phase"):
        queue.succeed(claim, _health_result())

    with sessions() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        projection = session.scalar(select(ReconciliationOperation))
        assert operation is not None and operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.result is None
        assert projection is not None and projection.state == "queued"


def test_primary_failure_quiesces_running_sibling_mutation(tmp_path) -> None:
    service, sessions, queue, reconciliation_id, job_id = _compensation_fixture(
        tmp_path, parallel_starts=True
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    first = queue.claim(NODE_A, "serial-a", 30)
    sibling = queue.claim(NODE_B, "serial-b", 30)
    assert first is not None and sibling is not None

    queue.fail(first, "start-failed")

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        sibling_operation = session.get(AgentOperation, sibling.operation_id)
        sibling_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == sibling.operation_id
            )
        )
        sibling_projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == sibling.operation_id
            )
        )
        assert stored is not None
        assert stored.current_phase == "waiting-for-operator"
        assert job is not None and job.state == "waiting-for-operator"
        assert sibling_operation is not None
        assert sibling_operation.state == "waiting-for-operator"
        assert sibling_attempt is not None
        assert sibling_attempt.state == "waiting-for-operator"
        assert sibling_projection is not None
        assert sibling_projection.state == "waiting-for-operator"
    with pytest.raises(StaleAgentAttempt):
        queue.succeed(sibling, _workload_result("start"))


def test_completed_cancellation_withdraws_before_compensation(tmp_path) -> None:
    service, sessions, queue, reconciliation_id, job_id = _compensation_fixture(
        tmp_path
    )
    publisher = service._publisher
    for _ in range(4):
        service.tick(reconciliation_id)
    worker = queue.claim(NODE_A, "serial-a", 30)
    assert worker is not None
    queue.succeed(worker, _workload_result("start"))
    service.tick(reconciliation_id)
    entrypoint = queue.claim(NODE_B, "serial-b", 30)
    assert entrypoint is not None
    queue.succeed(entrypoint, _workload_result("start"))
    with sessions.begin() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        publication = session.get(RoutePublication, reconciliation_id)
        assert stored is not None and job is not None and publication is not None
        stored.current_phase = "completed"
        stored.status = "succeeded"
        job.state = "succeeded"
        publication.state = "completed"

    service.request_cancel(reconciliation_id, "operator cancelled")

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        publication = session.get(RoutePublication, reconciliation_id)
        assert stored is not None and stored.current_phase == "compensating"
        assert publication is not None and publication.state == "routes-withdrawn"
    assert publisher.withdrawals == 2


def test_uncertain_mutation_waits_for_operator_without_unlocking_dependents(
    tmp_path,
) -> None:
    service, sessions, queue, reconciliation_id, job_id = _compensation_fixture(
        tmp_path
    )
    for _ in range(4):
        service.tick(reconciliation_id)
    worker = queue.claim(NODE_A, "serial-a", 30)
    assert worker is not None

    queue.wait_for_operator(worker, "mutation-uncertain")

    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        operations = list(session.scalars(select(AgentOperation)))
        assert stored is not None
        assert stored.current_phase == "waiting-for-operator"
        assert job is not None and job.state == "waiting-for-operator"
        assert len(operations) == 1


def test_api_result_consumer_is_bound_once_to_durable_execution(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'binding.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    class Operations:
        consumer = None

        def set_result_consumer(self, consumer) -> None:
            assert self.consumer is None
            self.consumer = consumer

    class Presence:
        def latest_in_session(self, *_args, **_kwargs):
            raise AssertionError("result consumption does not resolve routes")

    operations = Operations()
    service = bind_reconciliation_result_consumer(
        sessions,
        operations=operations,
        presence=Presence(),
        clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert operations.consumer == service.consume_result
