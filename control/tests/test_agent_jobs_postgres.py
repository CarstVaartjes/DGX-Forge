from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import shutil
import subprocess
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from dgx_control.agent_jobs import AgentJobService, StaleAgentAttempt
from dgx_control.models import AgentCertificate, AgentNode, Base, Job


NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 40


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL locking integration tests")
    try:
        container = subprocess.check_output([
            "docker", "run", "--rm", "-d", "-e", "POSTGRES_PASSWORD=postgres",
            "-p", "127.0.0.1::5432", "postgres:16",
        ], text=True).strip()
    except subprocess.CalledProcessError as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
    try:
        port = subprocess.check_output([
            "docker", "inspect", "-f",
            "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}", container,
        ], text=True).strip()
        engine = create_engine(f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres")
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.skip("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(["docker", "stop", container], check=False, capture_output=True)


@pytest.fixture
def service(postgres_engine):
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.flush()
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentCertificate(
                serial=serial,
                node_id=node_id,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=1),
                fingerprint=f"fingerprint-{serial}",
            ))
    return sessions, clock


def parent(sessions, clock) -> Job:
    job = Job(
        request_id=str(uuid.uuid4()), kind="agent.operations", state="queued", actor="operator",
        base_commit=COMMIT, targets=[NODE_A, NODE_B], payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={}, current_attempt=0, created_at=clock.now, updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job


def state(sessions, job_id: str) -> str:
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job.state


def test_postgres_claim_locks_only_operations_without_nullable_join(service, postgres_engine) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if "FROM agent_operations" in statement and "FOR UPDATE" in statement:
            statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", record)
    try:
        assert jobs.claim(NODE_A, "serial-a", 30) is not None
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)

    assert len(statements) == 1
    assert "LEFT OUTER JOIN" not in statements[0]
    assert "FOR UPDATE OF agent_operations SKIP LOCKED" in statements[0]


def test_postgres_separate_services_cannot_claim_the_same_operation(service) -> None:
    sessions, clock = service
    first_service = AgentJobService(sessions, clock=clock)
    second_service = AgentJobService(sessions, clock=clock)
    operation = first_service.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    barrier = threading.Barrier(2)

    def claim(service):
        barrier.wait()
        return service.claim(NODE_A, "serial-a", 30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, (first_service, second_service)))

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].operation_id == operation.id


def test_postgres_complete_holds_operation_lock_against_expired_reclaim(service, postgres_engine) -> None:
    sessions, clock = service
    completing = AgentJobService(sessions, clock=clock)
    reclaiming = AgentJobService(sessions, clock=clock)
    completing.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = completing.claim(NODE_A, "serial-a", 30)
    assert first is not None
    clock.advance(seconds=30)
    locked = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def pause_after_operation_lock(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if (
            threading.current_thread().name == "finisher"
            and "FROM agent_operations" in statement
            and "FOR UPDATE OF agent_operations" in statement
        ):
            locked.set()
            assert release.wait(timeout=5)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_operation_lock)
    try:
        def finish() -> None:
            try:
                completing.succeed(first.fence, {"healthy": True})
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=finish, name="finisher")
        thread.start()
        assert locked.wait(timeout=5)
        assert reclaiming.claim(NODE_A, "serial-a", 30) is None
        release.set()
        thread.join(timeout=5)
    finally:
        event.remove(postgres_engine, "after_cursor_execute", pause_after_operation_lock)

    assert len(errors) == 1
    assert isinstance(errors[0], StaleAgentAttempt)
    assert not thread.is_alive()
    assert reclaiming.claim(NODE_A, "serial-a", 30) is not None


def test_postgres_concurrent_final_completions_aggregate_parent_once(service, postgres_engine) -> None:
    sessions, clock = service
    first_service = AgentJobService(sessions, clock=clock)
    second_service = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    first_service.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    first_service.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})
    first = first_service.claim(NODE_A, "serial-a", 30)
    second = second_service.claim(NODE_B, "serial-b", 30)
    assert first is not None and second is not None
    aggregation_started = threading.Event()
    release = threading.Event()

    def pause_before_aggregation_reads_siblings(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if (
            "FROM agent_operations" in statement
            and "parent_job_id" in statement
            and threading.current_thread().name in {"first-finisher", "second-finisher"}
        ):
            aggregation_started.set()
            assert release.wait(timeout=5)

    event.listen(postgres_engine, "after_cursor_execute", pause_before_aggregation_reads_siblings)
    errors: list[Exception] = []
    def complete(service, fence) -> None:
        try:
            service.succeed(fence, {"healthy": True})
        except Exception as error:
            errors.append(error)
    try:
        thread_a = threading.Thread(target=complete, args=(first_service, first.fence), name="first-finisher")
        thread_b = threading.Thread(target=complete, args=(second_service, second.fence), name="second-finisher")
        thread_a.start(); thread_b.start()
        assert aggregation_started.wait(timeout=5)
        time.sleep(0.25)
        release.set()
        thread_a.join(timeout=5); thread_b.join(timeout=5)
    finally:
        event.remove(postgres_engine, "after_cursor_execute", pause_before_aggregation_reads_siblings)

    assert not errors
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert state(sessions, parent_job.id) == "succeeded"
