from __future__ import annotations

import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from dgx_control.git_policy import Eligibility
from dgx_control.models import Base, Reconciliation
from dgx_control.orchestration import (
    OperationGraph,
    OperationNode,
    ReconciliationOrchestrator,
)
from dgx_control.reconcile import Reconciler, resolved_reconciliation_plan
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

NODE_ID = "spk_" + "1" * 32
BASE_COMMIT = "a" * 40


class Policy:
    def eligible(self, commit: str) -> Eligibility:
        return Eligibility(commit, True, ())


class DesiredPlanner:
    def resolve(self, commit, profile_id, observations):
        operation = OperationNode(
            "model:probe",
            NODE_ID,
            "model",
            "node.probe",
            (),
            None,
            "b" * 64,
        )
        return resolved_reconciliation_plan(
            commit=commit,
            targets=(NODE_ID,),
            placements={"model": (NODE_ID,)},
            routes={},
            releases={},
            input_digests={"fleet": "f" * 64},
            operation_graph=OperationGraph(
                "pending",
                commit,
                (NODE_ID,),
                (operation,),
                "c" * 64,
            ),
            operation_payloads={"model:probe": {}},
            agent_protocol_range=(1, 1),
        )


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL concurrency tests")
    try:
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
    except subprocess.CalledProcessError as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
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
            pytest.skip("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(
            ["docker", "stop", container], check=False, capture_output=True
        )


def test_concurrent_identical_plans_get_one_atomic_persisted_reconciliation(
    postgres_engine: Engine,
) -> None:
    """A split lookup/insert would raise IntegrityError and leave an orphan row."""

    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 8, 5, tzinfo=UTC)
    stale_lookup = threading.Barrier(2)

    class SynchronizedOrchestrator(ReconciliationOrchestrator):
        def resolved_plan(self, plan_digest):
            resolved = super().resolved_plan(plan_digest)
            if resolved is None:
                stale_lookup.wait(timeout=10)
            return resolved

    services = (
        Reconciler(
            Policy(),
            DesiredPlanner(),
            observations=lambda: ("durable",),
            orchestrator=SynchronizedOrchestrator(sessions, clock=clock),
        ),
        Reconciler(
            Policy(),
            DesiredPlanner(),
            observations=lambda: ("durable",),
            orchestrator=SynchronizedOrchestrator(sessions, clock=clock),
        ),
    )
    start = threading.Barrier(2)

    def plan(service: Reconciler):
        start.wait(timeout=10)
        return service.plan(BASE_COMMIT, "inference")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(plan, service) for service in services]
        results = []
        integrity_errors = []
        for future in futures:
            try:
                results.append(future.result())
            except IntegrityError as error:
                integrity_errors.append(error)

    assert integrity_errors == []
    assert len({plan.digest for plan in results}) == 1
    assert len({plan.operation_graph.reconciliation_id for plan in results}) == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Reconciliation)) == 1
        stored = session.scalar(select(Reconciliation))
        assert stored is not None
        assert stored.plan_digest == results[0].digest
        assert stored.resolved_plan is not None
