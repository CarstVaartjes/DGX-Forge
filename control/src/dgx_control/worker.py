"""Durable job worker entry point and bounded handler registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

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
        housekeeping: Callable[[], object] | None = None,
    ) -> None:
        self._jobs = jobs
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._logs = logs
        self._housekeeping = housekeeping

    def run_once(self) -> bool:
        if self._housekeeping is not None:
            self._housekeeping()
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
        except Exception as error:
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

    from .code_host import RepositoryCodeHost
    from .db import build_engine, session_factory
    from .git_policy import GitPolicy, PolicyStore
    from .logging import JobLogStore
    from .offline import OnlineLock
    from .presence import AgentPresenceService, ManagementAddressPolicy
    from .route_runtime import ProductionRouteManager, RouteRuntimeError
    from .runtime import RuntimeHandlers
    from .settings import Settings

    settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url))
    jobs = JobService(sessions, clock=lambda: datetime.now(UTC))
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
    upstream_key_source = os.environ.get("DGX_LITELLM_UPSTREAM_KEY_FILE", "")
    upstream_key_path = Path(upstream_key_source) if upstream_key_source else None
    if (
        upstream_key_path is None
        or upstream_key_path.is_symlink()
        or not upstream_key_path.is_file()
    ):
        raise RouteRuntimeError("production LiteLLM upstream key file is unavailable")
    upstream_key = upstream_key_path.read_text().strip()
    live_config_raw = os.environ.get("DGX_LITELLM_CONFIG_PATH", "")
    if not live_config_raw:
        raise RouteRuntimeError("production LiteLLM config path is unavailable")
    management_policy = ManagementAddressPolicy.parse(
        settings.management_cidrs,
        forbidden_cidrs=settings.direct_fabric_cidrs,
    )
    route_manager = ProductionRouteManager(
        settings.repository_path,
        state_root=settings.state_path / "route-runtime",
        live_config=Path(live_config_raw),
        presence=AgentPresenceService(sessions, management_policy),
        management_policy=management_policy,
        upstream_key=upstream_key,
    )
    runtime = RuntimeHandlers(
        settings.repository_path,
        eligible=lambda commit: policy.eligible(commit).ok,
        route_manager=route_manager,
    )
    worker = Worker(
        jobs, os.environ.get("HOSTNAME", "control-worker"), runtime.registry(),
        logs=JobLogStore(settings.state_path / "job-logs"),
        housekeeping=lambda: route_manager.refresh_if_due(
            runtime.current_commit,
            eligible=lambda commit: policy.eligible(commit).ok,
        ),
    )
    with OnlineLock(settings.state_path / "offline.lock"):
        while True:
            if not worker.run_once():
                time.sleep(1)
