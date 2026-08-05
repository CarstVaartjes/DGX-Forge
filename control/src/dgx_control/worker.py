"""Durable job worker entry point and bounded handler registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from .jobs import JobService


@dataclass(frozen=True)
class HandlerRequest(Mapping[str, object]):
    job_id: str
    kind: str
    payload: Mapping[str, object]
    base_commit: str
    targets: tuple[str, ...]

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


Handler = Callable[[HandlerRequest], Mapping[str, object]]


class Worker:
    def __init__(
        self,
        jobs: JobService,
        worker_id: str,
        handlers: Mapping[str, Handler],
        *,
        logs=None,
        reconciliations=None,
    ) -> None:
        self._jobs = jobs
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._logs = logs
        self._reconciliations = reconciliations

    def run_once(self) -> bool:
        if self._reconciliations is not None and self._reconciliations.tick():
            return True
        attempt = self._jobs.claim(self._worker_id, 30)
        if attempt is None:
            return False
        if self._logs is not None:
            self._logs.save(attempt.job_id, f"job {attempt.kind} attempt {attempt.attempt} started".encode())
        handler = self._handlers.get(attempt.kind)
        if handler is None:
            self._jobs.fail(attempt, f"unsupported job kind: {attempt.kind}")
            if self._logs is not None:
                self._logs.save(attempt.job_id, b"job failed: unsupported job kind")
            return True
        try:
            result = handler(HandlerRequest(
                attempt.job_id, attempt.kind, attempt.payload,
                attempt.base_commit, attempt.targets,
            ))
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            self._jobs.fail(attempt, f"{type(error).__name__}: {error}")
            if self._logs is not None:
                self._logs.save(attempt.job_id, f"job failed: {type(error).__name__}: {error}".encode())
        else:
            self._jobs.succeed(attempt, result)
            if self._logs is not None:
                self._logs.save(attempt.job_id, b"job succeeded")
        return True


if __name__ == "__main__":
    import os
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    from .agent_jobs import AgentJobService
    from .agent_reconciliation import AgentReconciliationService
    from .code_host import RepositoryCodeHost
    from .db import build_engine, session_factory
    from .git_policy import GitPolicy, PolicyStore
    from .logging import JobLogStore
    from .offline import OnlineLock
    from .presence import AgentPresenceService, ManagementAddressPolicy
    from .route_runtime import AtomicRouteBundlePublisher
    from .runtime import RuntimeHandlers
    from .settings import Settings

    settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    jobs = JobService(sessions, clock=clock)
    address_policy = ManagementAddressPolicy.parse(
        settings.management_cidrs,
        forbidden_cidrs=settings.direct_fabric_cidrs,
    )
    presence = AgentPresenceService(sessions, address_policy, clock=clock)
    agent_jobs = AgentJobService(sessions, clock=clock)

    def endpoint(session, node_id: str) -> tuple[str, datetime]:
        observation = presence.latest_in_session(
            session, node_id, maximum_age_seconds=300
        )
        return observation.address, observation.observed_at

    reconciliations = AgentReconciliationService(
        sessions,
        agent_jobs=agent_jobs,
        publisher=AtomicRouteBundlePublisher(
            Path("/routes"),
            management_policy=address_policy,
            clock=clock,
            maximum_lease_seconds=300,
        ),
        endpoint_resolver=endpoint,
        clock=clock,
    )
    if settings.git_signing_key_path is None:
        raise RuntimeError("production Git signing key is unavailable")
    code_host = RepositoryCodeHost(
        settings.repository_path,
        signing_key=settings.git_signing_key_path,
        lock_path=settings.state_path / "git-change.lock",
    )
    policy = GitPolicy(
        PolicyStore(settings.state_path / "git-policy"), code_host,
        protected_branch=settings.deployment_branch,
        required_checks=settings.required_checks,
    )
    runtime = RuntimeHandlers(
        settings.repository_path,
        eligible=lambda commit: policy.eligible(commit).ok,
    )
    worker = Worker(
        jobs, os.environ.get("HOSTNAME", "control-worker"), runtime.registry(),
        logs=JobLogStore(settings.state_path / "job-logs"),
        reconciliations=reconciliations,
    )
    with OnlineLock(settings.state_path / "offline.lock"):
        while True:
            if not worker.run_once():
                time.sleep(1)
