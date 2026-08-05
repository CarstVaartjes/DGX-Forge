from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dgx_agent_protocol import AgentResult, canonical_message
from dgx_control.agent_jobs import AgentJobService, StaleAgentAttempt
from dgx_control.agent_reconciliation import AgentReconciliationService
from dgx_control.auth import AgentIdentity, AgentSource
from dgx_control.enrollment import EnrollmentService
from dgx_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentPresence,
    Base,
    Job,
    Reconciliation,
    ReconciliationOperation,
    RoutePublication,
)
from dgx_control.pki import CertificateAuthority, IssuedCertificate
from dgx_control.presence import AgentPresenceService, ManagementAddressPolicy
from dgx_control.route_runtime import AtomicRouteBundlePublisher
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
BASE_COMMIT = "a" * 40
NOW = datetime(2026, 8, 5, tzinfo=UTC)


class RevokingAuthority(CertificateAuthority):
    def issue_node(
        self, node_id: str, csr_pem: bytes, now: datetime
    ) -> IssuedCertificate:
        raise NotImplementedError

    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        raise NotImplementedError

    def revocation_bundle(self, now: datetime) -> bytes:
        return b""

    def revoke_node(self, serial: str, now: datetime) -> None:
        return None


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _json_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_result() -> dict[str, object]:
    return {
        "status": "ok",
        "evidence": {
            "status": "healthy",
            "action": "verify",
            "workload_id": "model",
            "release_digest": "a" * 64,
            "evidence_digest": "e" * 64,
        },
    }


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required for mandatory PostgreSQL races")
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-p",
            "127.0.0.1::5432",
            "postgres:16",
        ],
        text=True,
    ).strip()
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container,
            ],
            text=True,
        ).strip()
        engine = create_engine(
            f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"
        )
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError):
                time.sleep(0.1)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(
            ["docker", "stop", container], check=False, capture_output=True
        )


def _source(address: str) -> AgentSource:
    return AgentSource(
        AgentIdentity(NODE_A, "serial-a", "fingerprint-a", True),
        address,
    )


def _system(postgres_engine: Engine, route_root: Path, *, clock=lambda: NOW):
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    operation_id = f"model:{NODE_A}:workload.verify"
    payload = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "spark-runtime-v1",
        "expected_digest": "e" * 64,
    }
    operation = {
        "operation_id": operation_id,
        "node_id": NODE_A,
        "workload_id": "model",
        "kind": "workload.verify",
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
    quota = {"requests_per_minute": 20, "tokens_per_minute": 1000}
    routes = {
        "model": {
            "workload_id": "model",
            "nodes": [NODE_A],
            "entrypoint_node_id": NODE_A,
            "scheme": "http",
            "port": 8000,
            "path": "/v1",
            "quota": quota,
            "quota_digest": hashlib.sha256(
                (json.dumps(quota, sort_keys=True, separators=(",", ":")) + "\n").encode()
            ).hexdigest(),
        }
    }
    resolved = {
        "commit": BASE_COMMIT,
        "targets": [NODE_A],
        "placements": {},
        "routes": routes,
        "releases": {},
        "workload_groups": {},
        "input_digests": {"fleet": "f" * 64},
        "operation_graph": graph,
        "operation_payloads": {operation_id: payload},
        "agent_protocol_range": [1, 1],
    }
    reconciliation_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
        session.flush()
        session.add(
            AgentCertificate(
                serial="serial-a",
                node_id=NODE_A,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(hours=1),
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
                created_at=NOW,
            )
        )
        session.flush()
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
                created_at=NOW,
                updated_at=NOW,
                reconciliation_id=reconciliation_id,
            )
        )
    policy = ManagementAddressPolicy.parse("10.0.0.0/24")
    presence = AgentPresenceService(sessions, policy, clock=clock)
    presence.observe(_source("10.0.0.42"))
    operations = AgentJobService(sessions, clock=clock)
    publisher = AtomicRouteBundlePublisher(
        route_root,
        management_policy=policy,
        clock=clock,
        maximum_lease_seconds=300,
    )
    reconciliations = AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=publisher,
        endpoint_resolver=lambda session, node_id: (
            presence.latest_in_session(
                session, node_id, maximum_age_seconds=300
            ).address,
            presence.latest_in_session(
                session, node_id, maximum_age_seconds=300
            ).observed_at,
        ),
        clock=clock,
    )
    operations.set_result_consumer(reconciliations.consume_result)
    return sessions, presence, operations, reconciliations, reconciliation_id, job_id


def _claimed(system):
    sessions, _presence, operations, reconciliations, reconciliation_id, job_id = system
    for _ in range(4):
        assert reconciliations.tick(reconciliation_id) is True
    claim = operations.claim(NODE_A, "serial-a", 30)
    assert claim is not None
    return sessions, operations, reconciliations, reconciliation_id, job_id, claim


def _clone_service(system) -> AgentReconciliationService:
    sessions, presence, operations, reconciliations, _reconciliation_id, _job_id = system

    def endpoint(session, node_id):
        observation = presence.latest_in_session(
            session, node_id, maximum_age_seconds=300
        )
        return observation.address, observation.observed_at

    return AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=reconciliations._publisher,
        endpoint_resolver=endpoint,
        clock=reconciliations._clock,
    )


def _race(*calls):
    start = threading.Barrier(len(calls))

    def invoke(call):
        start.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(invoke, call) for call in calls]
        return [future.result(timeout=10) for future in futures]


def test_postgres_contact_failure_rolls_back_claim_lease_and_presence(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "claim-contact")
    sessions, presence, operations, reconciliations, reconciliation_id, _job_id = system

    def reject_contact(session, source) -> None:
        presence.observe_in_session(session, source)
        raise ValueError("contact write rejected")

    operations.set_contact_consumer(reject_contact)
    for _ in range(4):
        reconciliations.tick(reconciliation_id)
    with pytest.raises(ValueError, match="contact write rejected"):
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            source=_source("10.0.0.43"),
        )

    with sessions() as session:
        stored = session.scalar(select(AgentOperation))
        contact = session.get(AgentPresence, NODE_A)
        assert stored is not None and stored.state == "queued"
        assert stored.current_attempt == 0
        assert session.scalar(select(func.count()).select_from(AgentOperationAttempt)) == 0
        assert contact is not None and contact.management_address == "10.0.0.42"


def test_postgres_phase_rejection_rolls_back_result_contact_and_projection(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "result-contact")
    sessions, presence, operations, _reconciliations, reconciliation_id, job_id = system
    operations.set_contact_consumer(presence.observe_in_session)
    sessions, operations, _reconciliations, reconciliation_id, job_id, claim = _claimed(
        system
    )
    with sessions.begin() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and job is not None
        stored.current_phase = "failed"
        stored.status = "failed"
        job.state = "failed"
    message = AgentResult(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state="succeeded",
        result=_verify_result(),
    )

    with pytest.raises(ValueError, match="phase"):
        operations.record_result(message, source=_source("10.0.0.44"))

    with sessions() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(select(AgentOperationAttempt))
        projection = session.scalar(select(ReconciliationOperation))
        contact = session.get(AgentPresence, NODE_A)
        assert operation is not None and operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.result is None
        assert projection is not None and projection.state == "queued"
        assert contact is not None and contact.management_address == "10.0.0.42"


def test_postgres_stale_result_does_not_write_contact(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "stale-contact")
    sessions, presence, operations, _reconciliations, _reconciliation_id, _job_id = system
    operations.set_contact_consumer(presence.observe_in_session)
    sessions, operations, _reconciliations, _reconciliation_id, _job_id, claim = _claimed(
        system
    )
    stale = AgentResult(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=str(uuid.uuid4()),
        node_id=claim.node_id,
        deadline=claim.deadline,
        state="succeeded",
        result=_verify_result(),
    )

    with pytest.raises(StaleAgentAttempt):
        operations.record_result(stale, source=_source("10.0.0.45"))

    with sessions() as session:
        contact = session.get(AgentPresence, NODE_A)
        assert contact is not None and contact.management_address == "10.0.0.42"


def test_postgres_publication_reuses_tick_session_for_presence(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "publication-session")
    _sessions, _presence, operations, reconciliations, reconciliation_id, _job_id = system
    _sessions, operations, reconciliations, reconciliation_id, _job_id, claim = _claimed(
        system
    )
    operations.succeed(claim, _verify_result())
    reconciliations.tick(reconciliation_id)
    reconciliations.tick(reconciliation_id)

    def bounded_lock_wait(_dbapi_connection, connection_record, connection_proxy) -> None:
        del connection_record, connection_proxy
        with _dbapi_connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '500ms'")

    event.listen(postgres_engine, "checkout", bounded_lock_wait)
    try:
        assert reconciliations.tick(reconciliation_id) is True
    finally:
        event.remove(postgres_engine, "checkout", bounded_lock_wait)

    with _sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        assert stored is not None and stored.current_phase == "completed"


def test_postgres_tick_tick_race_enqueues_one_operation(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "tick-tick")
    sessions, _presence, _operations, reconciliations, reconciliation_id, _job_id = system
    for _ in range(3):
        reconciliations.tick(reconciliation_id)
    other = _clone_service(system)

    assert _race(reconciliations.tick, other.tick) == [True, True]

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentOperation)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ReconciliationOperation))
            == 1
        )


def test_postgres_result_tick_race_preserves_exact_acceptance(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "result-tick")
    sessions, operations, _reconciliations, _reconciliation_id, _job_id, claim = _claimed(
        system
    )
    other = _clone_service(system)

    outcomes = _race(
        lambda: operations.succeed(claim, _verify_result()),
        other.tick,
    )

    assert outcomes[0] is None and outcomes[1] is True
    with sessions() as session:
        projection = session.scalar(select(ReconciliationOperation))
        assert projection is not None and projection.state == "accepted"
        assert projection.result_digest == _digest(_verify_result())
        assert session.scalar(select(func.count()).select_from(AgentOperation)) == 1


def test_postgres_result_revocation_race_never_publishes_revoked_target(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "result-revocation")
    sessions, operations, reconciliations, reconciliation_id, job_id, claim = _claimed(
        system
    )
    enrollment = EnrollmentService(sessions, RevokingAuthority(), clock=lambda: NOW)

    try:
        _race(
            lambda: operations.succeed(claim, _verify_result()),
            lambda: enrollment.revoke_node(NODE_A, "administrator"),
        )
    except StaleAgentAttempt:
        pass
    assert reconciliations.tick(reconciliation_id) is True

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert node is not None and node.state == "retired"
        assert stored is not None
        assert stored.current_phase == "waiting-for-operator"
        assert stored.status == "failed"
        assert job is not None and job.state == "waiting-for-operator"
        marker = json.loads(
            (tmp_path / "result-revocation" / "activation.json").read_bytes()
        )
        assert marker["state"] == "maintenance"


def test_postgres_publication_publication_race_activates_one_bundle(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    route_root = tmp_path / "publication-publication"
    system = _system(postgres_engine, route_root)
    sessions, operations, reconciliations, reconciliation_id, _job_id, claim = _claimed(
        system
    )
    operations.succeed(claim, _verify_result())
    reconciliations.tick(reconciliation_id)
    reconciliations.tick(reconciliation_id)
    other = _clone_service(system)

    outcomes = _race(reconciliations.tick, other.tick)

    assert sorted(outcomes) == [False, True]
    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "published"
    assert marker["reconciliation_id"] == reconciliation_id
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        publication = session.get(RoutePublication, reconciliation_id)
        assert stored is not None and stored.current_phase == "completed"
        assert publication is not None and publication.state == "completed"


def _compensation_system(postgres_engine: Engine, route_root: Path):
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    operation_id = f"model:{NODE_A}:workload.start"
    payload = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "spark-runtime-v1",
        "preparation_digest": "d" * 64,
    }
    graph = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "targets": [NODE_A, NODE_B],
        "nodes": [
            {
                "operation_id": operation_id,
                "node_id": NODE_A,
                "workload_id": "model",
                "kind": "workload.start",
                "dependencies": [],
                "compensation_kind": "workload.stop",
                "payload_digest": _digest(payload),
            }
        ],
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
        "operation_payloads": {operation_id: payload},
        "agent_protocol_range": [1, 1],
    }
    reconciliation_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    primary_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(node_id=NODE_A, state="active", capabilities=[]),
                AgentNode(node_id=NODE_B, state="active", capabilities=[]),
            ]
        )
        session.flush()
        session.add_all(
            [
                AgentCertificate(
                    serial="serial-a",
                    node_id=NODE_A,
                    not_before=NOW - timedelta(minutes=1),
                    not_after=NOW + timedelta(hours=1),
                    fingerprint="fingerprint-a",
                ),
                AgentCertificate(
                    serial="serial-b",
                    node_id=NODE_B,
                    not_before=NOW - timedelta(minutes=1),
                    not_after=NOW + timedelta(hours=1),
                    fingerprint="fingerprint-b",
                ),
            ]
        )
        session.add(
            Reconciliation(
                id=reconciliation_id,
                base_commit=BASE_COMMIT,
                status="running",
                summary={},
                graph=graph,
                graph_digest=_json_digest(graph),
                plan_digest=_json_digest(resolved),
                resolved_plan=resolved,
                current_phase="compensating",
                route_withdrawal_generation=0,
                terminal_reason="start failed",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="reconcile",
                state="running",
                actor="operator",
                base_commit=BASE_COMMIT,
                targets=[NODE_A, NODE_B],
                payload_digest=_digest({}),
                payload={},
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
                reconciliation_id=reconciliation_id,
            )
        )
        session.flush()
        session.add(
            AgentOperation(
                id=primary_id,
                parent_job_id=job_id,
                node_id=NODE_A,
                kind="workload.start",
                payload_digest=_digest(payload),
                payload=payload,
                base_commit=BASE_COMMIT,
                state="succeeded",
                current_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            RoutePublication(
                reconciliation_id=reconciliation_id,
                state="routes-withdrawn",
                generation=1,
                plan_digest=_json_digest(resolved),
            )
        )
        session.flush()
        session.add(
            ReconciliationOperation(
                reconciliation_id=reconciliation_id,
                graph_operation_id=operation_id,
                role="primary",
                agent_operation_id=primary_id,
                expected_payload_digest=_digest(payload),
                state="accepted",
                result_digest="1" * 64,
                evidence_digest="2" * 64,
                accepted_at=NOW,
            )
        )
    policy = ManagementAddressPolicy.parse("10.0.0.0/24")
    queue = AgentJobService(sessions, clock=lambda: NOW)
    publisher = AtomicRouteBundlePublisher(
        route_root,
        management_policy=policy,
        clock=lambda: NOW,
    )
    service = AgentReconciliationService(
        sessions,
        agent_jobs=queue,
        publisher=publisher,
        endpoint_resolver=lambda _session, _node: ("10.0.0.42", NOW),
        clock=lambda: NOW,
    )
    return sessions, queue, service, reconciliation_id


def test_postgres_compensation_tick_race_enqueues_one_stop(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    sessions, queue, service, _reconciliation_id = _compensation_system(
        postgres_engine, tmp_path / "compensation-tick"
    )
    other = AgentReconciliationService(
        sessions,
        agent_jobs=queue,
        publisher=service._publisher,
        endpoint_resolver=lambda _session, _node: ("10.0.0.42", NOW),
        clock=lambda: NOW,
    )

    assert _race(service.tick, other.tick) == [True, True]

    with sessions() as session:
        compensations = list(
            session.scalars(
                select(ReconciliationOperation).where(
                    ReconciliationOperation.role == "compensation"
                )
            )
        )
        stops = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "workload.stop")
            )
        )
        assert len(compensations) == len(stops) == 1
        assert compensations[0].agent_operation_id == stops[0].id


@pytest.mark.parametrize(
    ("role", "operation_state"),
    [
        ("primary", "queued"),
        ("primary", "running"),
        ("compensation", "queued"),
        ("compensation", "running"),
    ],
)
def test_postgres_revocation_quiesces_sibling_mutation_and_compensation(
    postgres_engine: Engine,
    tmp_path: Path,
    role: str,
    operation_state: str,
) -> None:
    sessions, queue, service, reconciliation_id = _compensation_system(
        postgres_engine, tmp_path / f"revocation-{role}-{operation_state}"
    )
    if role == "primary":
        with sessions.begin() as session:
            reconciliation = session.get(Reconciliation, reconciliation_id)
            projection = session.scalar(
                select(ReconciliationOperation).where(
                    ReconciliationOperation.role == "primary"
                )
            )
            operation = session.get(AgentOperation, projection.agent_operation_id)
            assert reconciliation is not None and operation is not None
            reconciliation.current_phase = "dispatching"
            projection.state = "queued"
            projection.result_digest = None
            projection.evidence_digest = None
            projection.accepted_at = None
            operation.state = "queued"
            operation.current_attempt = 0
    else:
        assert service.tick(reconciliation_id) is True

    with sessions() as session:
        projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.role == role,
                ReconciliationOperation.state == "queued",
            )
        )
        assert projection is not None and projection.agent_operation_id is not None
        operation_id = projection.agent_operation_id

    claim = None
    if operation_state == "running":
        claim = queue.claim(NODE_A, "serial-a", 30)
        assert claim is not None and claim.operation_id == operation_id

    enrollment = EnrollmentService(sessions, RevokingAuthority(), clock=lambda: NOW)
    enrollment.revoke_node(NODE_B, "administrator")
    assert service.tick(reconciliation_id) is True

    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        operation = session.get(AgentOperation, operation_id)
        projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == operation_id
            )
        )
        assert reconciliation is not None
        assert reconciliation.current_phase == "waiting-for-operator"
        assert operation is not None
        expected = "failed" if operation_state == "queued" else "waiting-for-operator"
        assert operation.state == expected
        assert projection is not None and projection.state == expected
        if claim is not None:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation_id
                )
            )
            assert attempt is not None and attempt.state == "waiting-for-operator"


def test_postgres_stale_completed_candidate_cannot_withdraw_refreshed_lease(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    current = [NOW]
    system = _system(
        postgres_engine,
        tmp_path / "stale-completed-candidate",
        clock=lambda: current[0],
    )
    sessions, presence, operations, reconciliations, reconciliation_id, _job_id = system
    sessions, operations, reconciliations, reconciliation_id, _job_id, claim = _claimed(
        system
    )
    operations.succeed(claim, _verify_result())
    for _ in range(3):
        reconciliations.tick(reconciliation_id)
    current[0] += timedelta(seconds=31)

    stale_service = AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=reconciliations._publisher,
        endpoint_resolver=lambda session, node_id: (
            presence.latest_in_session(
                session, node_id, maximum_age_seconds=300
            ).address,
            presence.latest_in_session(
                session, node_id, maximum_age_seconds=300
            ).observed_at,
        ),
        clock=lambda: current[0],
    )
    candidate_selected = threading.Event()
    release_candidate = threading.Event()
    stale_results: list[object] = []

    def pause_after_candidate(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            threading.current_thread().name == "stale-candidate"
            and "FROM reconciliations JOIN jobs" in statement
            and "route_publications.lease_expires_at" in statement
            and "FOR UPDATE" not in statement
        ):
            candidate_selected.set()
            assert release_candidate.wait(timeout=5)

    def run_stale_candidate() -> None:
        try:
            stale_results.append(stale_service.tick())
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:  # pragma: no cover - asserted below
            stale_results.append(error)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_candidate)
    try:
        stale = threading.Thread(target=run_stale_candidate, name="stale-candidate")
        stale.start()
        assert candidate_selected.wait(timeout=5)
        for _ in range(3):
            assert reconciliations.tick(reconciliation_id) is True
        with sessions() as session:
            fresh = session.get(Reconciliation, reconciliation_id)
            fresh_publication = session.execute(
                select(
                    Reconciliation.current_phase,
                ).where(Reconciliation.id == reconciliation_id)
            ).scalar_one()
            assert fresh is not None and fresh.current_phase == "completed"
            assert fresh_publication == "completed"
        release_candidate.set()
        stale.join(timeout=5)
    finally:
        release_candidate.set()
        event.remove(postgres_engine, "after_cursor_execute", pause_after_candidate)

    assert not stale.is_alive()
    assert stale_results == [False]
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        assert stored is not None and stored.current_phase == "completed"
