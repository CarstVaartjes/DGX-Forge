from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dgx_control.jobs import JobService
from dgx_control.models import Base
from dgx_control.worker import Worker


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


def test_unknown_job_kind_fails_without_execution(tmp_path) -> None:
    jobs = _service(tmp_path)
    job = jobs.enqueue("unknown", "admin", "abc", [], {})
    assert Worker(jobs, "worker-1", {}).run_once()
    assert jobs.get(job.id).state == "failed"
