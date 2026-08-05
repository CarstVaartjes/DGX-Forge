from datetime import UTC, datetime

import pytest
from dgx_control.jobs import JobService
from dgx_control.models import Base
from dgx_control.worker import HandlerRequest, Worker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _service(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}")
    Base.metadata.create_all(engine)
    return JobService(sessionmaker(engine, expire_on_commit=False), clock=lambda: datetime(2026, 8, 3, tzinfo=UTC))


def test_worker_dispatches_registered_handler_and_persists_result(tmp_path) -> None:
    jobs = _service(tmp_path)
    job = jobs.enqueue("probe", "admin", "abc", ["node"], {"value": 4})
    worker = Worker(jobs, "worker-1", {"probe": lambda payload: {"result": payload["value"] + 1}})
    assert worker.run_once()
    assert jobs.get(job.id).state == "succeeded"
    assert jobs.get(job.id).result == {"result": 5}


def test_worker_handler_receives_pinned_job_metadata(tmp_path) -> None:
    jobs = _service(tmp_path)
    job = jobs.enqueue("probe", "admin", "a" * 40, ["spk_a"], {"value": 4})
    received = []

    def handle(request: HandlerRequest):
        received.append(request)
        return {"ok": True}

    Worker(jobs, "worker-a", {"probe": handle}).run_once()

    assert received[0]["value"] == 4
    assert received[0].base_commit == "a" * 40
    assert received[0].targets == ("spk_a",)
    assert jobs.get(job.id).state == "succeeded"


def test_unknown_job_kind_fails_without_execution(tmp_path) -> None:
    jobs = _service(tmp_path)
    job = jobs.enqueue("unknown", "admin", "abc", [], {})
    assert Worker(jobs, "worker-1", {}).run_once()
    assert jobs.get(job.id).state == "failed"


def test_worker_does_not_mask_unexpected_programming_error(tmp_path) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "admin", "abc", [], {})

    with pytest.raises(AssertionError, match="programming defect"):
        Worker(
            jobs,
            "worker-1",
            {"probe": lambda _request: (_ for _ in ()).throw(
                AssertionError("programming defect")
            )},
        ).run_once()


def test_worker_ticks_durable_reconciliations_before_generic_jobs(tmp_path) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})

    class Reconciliations:
        def __init__(self) -> None:
            self.calls = 0

        def tick(self) -> bool:
            self.calls += 1
            return True

    reconciliations = Reconciliations()
    handled = []
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda request: handled.append(request) or {}},
        reconciliations=reconciliations,
    )

    assert worker.run_once() is True
    assert reconciliations.calls == 1
    assert handled == []


def test_worker_falls_through_when_no_reconciliation_can_advance(tmp_path) -> None:
    jobs = _service(tmp_path)
    jobs.enqueue("probe", "operator", "a" * 40, ["node"], {})

    class Reconciliations:
        def tick(self) -> bool:
            return False

    handled = []
    worker = Worker(
        jobs,
        "worker-1",
        {"probe": lambda request: handled.append(request.kind) or {}},
        reconciliations=Reconciliations(),
    )

    assert worker.run_once() is True
    assert handled == ["probe"]
