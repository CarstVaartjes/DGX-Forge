"""Durable job worker entry point and bounded handler registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .jobs import JobService

Handler = Callable[[Mapping[str, object]], Mapping[str, object]]


class Worker:
    def __init__(self, jobs: JobService, worker_id: str, handlers: Mapping[str, Handler]) -> None:
        self._jobs = jobs
        self._worker_id = worker_id
        self._handlers = dict(handlers)

    def run_once(self) -> bool:
        attempt = self._jobs.claim(self._worker_id, 30)
        if attempt is None:
            return False
        handler = self._handlers.get(attempt.kind)
        if handler is None:
            self._jobs.fail(attempt, f"unsupported job kind: {attempt.kind}")
            return True
        try:
            result = handler(attempt.payload)
        except Exception as error:
            self._jobs.fail(attempt, f"{type(error).__name__}: {error}")
        else:
            self._jobs.succeed(attempt, result)
        return True


if __name__ == "__main__":
    import os
    import time
    from datetime import UTC, datetime

    from .db import build_engine, session_factory
    from .settings import Settings
    from .offline import OnlineLock

    settings = Settings.from_env_and_secrets()
    jobs = JobService(session_factory(build_engine(settings.database_url)), clock=lambda: datetime.now(UTC))
    worker = Worker(jobs, os.environ.get("HOSTNAME", "control-worker"), {})
    with OnlineLock(settings.state_path / "offline.lock"):
        while True:
            if not worker.run_once():
                time.sleep(1)
