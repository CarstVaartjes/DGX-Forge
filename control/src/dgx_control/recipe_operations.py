"""Digest-bound orchestration for local recipe installation and execution."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from dgx_agent_protocol import canonical_message
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .install_admission import InstallAdmissionService, InstallPlan
from .models import (
    AgentOperation,
    InstallationNode,
    Job,
    LocalRecipeRevision,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .run_admission import RunAdmissionService, RunPlan
from .topology import Placement


class AgentJobQueue(Protocol):
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        base_commit: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> AgentOperation: ...

    def notify_available(self) -> None: ...


class RecipeOperationConflict(RuntimeError):
    """A lifecycle request is stale, conflicting, or unsafe to execute."""


@dataclass(frozen=True, slots=True)
class RecipeOperationView:
    id: str
    kind: str
    owner_id: str
    state: str
    plan_digest: str
    nodes: tuple[str, ...]
    result: dict[str, object] | None


_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "expired"})


class RecipeOperationService:
    """Turn accepted admission plans into one fenced, gang-aware operation group."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        install_admission: InstallAdmissionService,
        run_admission: RunAdmissionService,
        agent_jobs: AgentJobQueue,
        clock: Callable[[], datetime],
        route_withdrawer: Callable[[str], None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._install_admission = install_admission
        self._run_admission = run_admission
        self._agent_jobs = agent_jobs
        self._clock = clock
        self._route_withdrawer = route_withdrawer or (lambda _run_id: None)

    def preview_install(
        self, recipe_revision_id: str, node_ids: tuple[str, ...]
    ) -> InstallPlan:
        return self._install_admission.plan_install(
            recipe_revision_id, node_ids, now=self._clock()
        )

    def preview_run(
        self, installation_id: str, placements: tuple[Placement, ...]
    ) -> RunPlan:
        return self._run_admission.plan_run(
            installation_id, placements, now=self._clock()
        )

    def install(
        self,
        plan: InstallPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.install", plan_digest)
        if existing is not None:
            return existing
        if plan_digest != plan.plan_digest:
            raise RecipeOperationConflict("submitted plan digest does not match preview")
        try:
            installation_id = self._install_admission.accept_install(
                plan, actor=actor, now=self._clock()
            )
        except (RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error
        with self._sessions.begin() as session:
            installation = session.get(RecipeInstallation, installation_id)
            assert installation is not None
            installation.state = "installing"
            installation.updated_at = self._clock()
        return self._queue(
            kind="recipe.install",
            owner_kind="installation",
            owner_id=installation_id,
            plan_digest=plan.plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "schema_version": 1,
                        "installation_id": installation_id,
                        "recipe_revision_id": plan.recipe_revision_id,
                        "recipe_content_sha256": plan.recipe_content_sha256,
                        "plan_digest": plan.plan_digest,
                        "expected_bytes": node.required_bytes,
                    },
                )
                for node in plan.nodes
            ),
            authority_digest=plan.recipe_content_sha256,
        )

    def start(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        alias: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.start", plan_digest)
        if existing is not None:
            return existing
        if plan_digest != plan.plan_digest:
            raise RecipeOperationConflict("submitted plan digest does not match preview")
        try:
            run_id = self._run_admission.accept_run(
                plan, alias=alias, actor=actor, now=self._clock()
            )
        except (RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error
        with self._sessions.begin() as session:
            run = session.get(RecipeRun, run_id)
            revision = session.get(LocalRecipeRevision, plan.recipe_revision_id)
            assert run is not None and revision is not None
            run.state = "starting"
            run.updated_at = self._clock()
            recipe_digest = revision.content_sha256
        assert recipe_digest is not None
        return self._queue(
            kind="recipe.start",
            owner_kind="run",
            owner_id=run_id,
            plan_digest=plan.plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "installation_id": plan.installation_id,
                        "recipe_revision_id": plan.recipe_revision_id,
                        "recipe_content_sha256": recipe_digest,
                        "plan_digest": plan.plan_digest,
                        "alias": alias,
                        "rank": node.rank,
                        "role": node.role,
                        "port": node.port,
                        "reserved_memory_bytes": node.required_memory_bytes,
                    },
                )
                for node in plan.nodes
            ),
            authority_digest=recipe_digest,
        )

    def stop(
        self, run_id: str, *, actor: str, request_id: str
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.stop", None)
        if existing is not None:
            return existing
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise RecipeOperationConflict("recipe run does not exist")
            if run.state not in {"starting", "running", "failed", "lost"}:
                raise RecipeOperationConflict("recipe run is not stoppable")
            nodes = tuple(
                session.scalars(
                    select(RunNode).where(RunNode.run_id == run_id).order_by(RunNode.rank)
                )
            )
            plan_digest = run.plan_digest
        # Route removal is deliberately synchronous and precedes creation of
        # stop work. If withdrawal fails, no stop command can race a live route.
        self._route_withdrawer(run_id)
        with self._sessions.begin() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None
            run.state = "stopping"
            run.route_state = "withdrawn"
            run.route_error = None
            run.updated_at = self._clock()
        return self._queue(
            kind="recipe.stop",
            owner_kind="run",
            owner_id=run_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "plan_digest": plan_digest,
                    },
                )
                for node in nodes
            ),
            authority_digest=plan_digest,
        )

    def uninstall(
        self, installation_id: str, *, actor: str, request_id: str
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.uninstall", None)
        if existing is not None:
            return existing
        with self._sessions() as session:
            installation = session.get(RecipeInstallation, installation_id)
            if installation is None:
                raise RecipeOperationConflict("recipe installation does not exist")
            active_run = session.scalar(
                select(RecipeRun.id).where(
                    RecipeRun.installation_id == installation_id,
                    RecipeRun.state.not_in({"stopped"}),
                )
            )
            if active_run is not None:
                raise RecipeOperationConflict("installation has an active run")
            if installation.state not in {"installed", "partial", "failed"}:
                raise RecipeOperationConflict("recipe installation is not uninstallable")
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .order_by(InstallationNode.node_id)
                )
            )
            plan_digest = installation.plan_digest
            revision = session.get(LocalRecipeRevision, installation.recipe_revision_id)
            assert revision is not None and revision.content_sha256 is not None
            recipe_digest = revision.content_sha256
        return self._queue(
            kind="recipe.uninstall",
            owner_kind="installation",
            owner_id=installation_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "schema_version": 1,
                        "installation_id": installation_id,
                        "recipe_content_sha256": recipe_digest,
                        "plan_digest": plan_digest,
                    },
                )
                for node in nodes
            ),
            authority_digest=recipe_digest,
        )

    def retry(
        self, operation_id: str, *, actor: str, request_id: str
    ) -> RecipeOperationView:
        previous = self.get(operation_id)
        if previous.kind != "recipe.install" or previous.state != "failed":
            raise RecipeOperationConflict("only failed recipe installs can be retried")
        existing = self._idempotent(request_id, "recipe.install", previous.plan_digest)
        if existing is not None:
            return existing
        with self._sessions() as session:
            installation = session.get(RecipeInstallation, previous.owner_id)
            assert installation is not None
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation.id)
                    .order_by(InstallationNode.node_id)
                )
            )
            revision = session.get(LocalRecipeRevision, installation.recipe_revision_id)
            assert revision is not None and revision.content_sha256 is not None
            recipe_digest = revision.content_sha256
        with self._sessions.begin() as session:
            installation = session.get(RecipeInstallation, previous.owner_id)
            assert installation is not None
            installation.state = "installing"
            installation.updated_at = self._clock()
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation.id
                )
            ):
                node.state = "planned"
        return self._queue(
            kind="recipe.install",
            owner_kind="installation",
            owner_id=previous.owner_id,
            plan_digest=previous.plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "schema_version": 1,
                        "installation_id": previous.owner_id,
                        "recipe_revision_id": installation.recipe_revision_id,
                        "recipe_content_sha256": recipe_digest,
                        "plan_digest": previous.plan_digest,
                        "expected_bytes": node.required_bytes,
                    },
                )
                for node in nodes
            ),
            authority_digest=recipe_digest,
        )

    def record_node_result(
        self,
        operation_id: str,
        node_id: str,
        *,
        succeeded: bool,
        evidence: Mapping[str, object],
    ) -> RecipeOperationView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise KeyError(operation_id)
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == job.id,
                    AgentOperation.node_id == node_id,
                )
            )
            if operation is None:
                raise RecipeOperationConflict("node is not part of operation group")
            operation.state = "succeeded" if succeeded else "failed"
            operation.updated_at = now
            self._project_node_result(
                session,
                job,
                operation,
                succeeded=succeeded,
                evidence=evidence,
                now=now,
            )
        return self.get(operation_id)

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        _attempt: object,
        message: object,
    ) -> None:
        """Project an authenticated agent result in the queue transaction."""
        job = session.get(Job, operation.parent_job_id)
        if job is None or not job.kind.startswith("recipe."):
            return
        state = getattr(message, "state", None)
        result = getattr(message, "result", None)
        if state not in {"succeeded", "failed"} or not isinstance(result, Mapping):
            raise RecipeOperationConflict("recipe agent result is invalid")
        raw_evidence = result.get("evidence", result)
        if not isinstance(raw_evidence, Mapping):
            raise RecipeOperationConflict("recipe agent evidence is invalid")
        self._project_node_result(
            session,
            job,
            operation,
            succeeded=state == "succeeded",
            evidence=raw_evidence,
            now=self._clock(),
        )

    def _project_node_result(
        self,
        session: Session,
        job: Job,
        operation: AgentOperation,
        *,
        succeeded: bool,
        evidence: Mapping[str, object],
        now: datetime,
    ) -> None:
        node_id = operation.node_id
        owner_id = _required_string(job.payload, "owner_id")
        if job.kind == "recipe.install":
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = "installed" if succeeded else "failed"
            if succeeded:
                installed_bytes = evidence.get("installed_bytes")
                if not isinstance(installed_bytes, int) or installed_bytes < 0:
                    raise RecipeOperationConflict("install evidence is invalid")
                node.installed_bytes = installed_bytes
            node.updated_at = now
        elif job.kind in {"recipe.start", "recipe.stop"}:
            node = session.scalar(
                select(RunNode).where(
                    RunNode.run_id == owner_id, RunNode.node_id == node_id
                )
            )
            assert node is not None
            node.state = (
                "running"
                if job.kind == "recipe.start" and succeeded
                else "stopped"
                if succeeded
                else "failed"
            )
            if job.kind == "recipe.start" and succeeded:
                endpoint = evidence.get("endpoint")
                digest = evidence.get("evidence_digest")
                if not isinstance(endpoint, str) or not isinstance(digest, str):
                    raise RecipeOperationConflict("start evidence is invalid")
                node.endpoint = {"url": endpoint}
                node.evidence_digest = digest
            node.updated_at = now
        elif job.kind == "recipe.uninstall":
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = "uninstalled" if succeeded else "failed"
            node.updated_at = now
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        )
        terminal = all(child.state in _TERMINAL_JOB_STATES for child in children)
        if terminal:
            successful = sorted(
                child.node_id for child in children if child.state == "succeeded"
            )
            failed = sorted(child.node_id for child in children if child.state == "failed")
            job.state = "failed" if failed else "succeeded"
            job.result = {"successful_nodes": successful, "failed_nodes": failed}
            if job.kind == "recipe.install":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = "partial" if failed else "installed"
                installation.updated_at = now
            elif job.kind == "recipe.start":
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                run.state = "failed" if failed else "running"
                run.route_state = "failed" if failed else "pending"
                run.route_error = "one or more ranks failed to start" if failed else None
                run.updated_at = now
            elif job.kind == "recipe.stop":
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                run.state = "failed" if failed else "stopped"
                run.stopped_at = now if not failed else None
                run.updated_at = now
                if not failed:
                    self._release(session, "run", owner_id, now)
            elif job.kind == "recipe.uninstall":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = "failed" if failed else "uninstalled"
                installation.updated_at = now
                if not failed:
                    self._release(session, "installation", owner_id, now)
        else:
            job.state = "running"
        job.updated_at = now

    def get(self, operation_id: str) -> RecipeOperationView:
        with self._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise KeyError(operation_id)
            return self._view(job)

    def _idempotent(
        self, request_id: str, kind: str, plan_digest: str | None
    ) -> RecipeOperationView | None:
        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is None:
                return None
            existing_digest = existing.payload.get("plan_digest")
            if (
                existing.kind != kind
                or (plan_digest is not None and existing_digest != plan_digest)
            ):
                raise RecipeOperationConflict("request key was already used differently")
            return self._view(existing)

    def _queue(
        self,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
        plan_digest: str,
        actor: str,
        request_id: str,
        node_payloads: Sequence[tuple[str, Mapping[str, object]]],
        authority_digest: str,
    ) -> RecipeOperationView:
        if not node_payloads:
            raise RecipeOperationConflict("operation group has no target nodes")
        job_id = str(uuid.uuid4())
        targets = sorted(node_id for node_id, _payload in node_payloads)
        job_payload: dict[str, object] = {
            "schema_version": 1,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "plan_digest": plan_digest,
        }
        now = self._clock()
        with self._sessions.begin() as session:
            if session.scalar(select(Job.id).where(Job.request_id == request_id)):
                raise RecipeOperationConflict("request key was already used differently")
            job = Job(
                id=job_id,
                request_id=request_id,
                kind=kind,
                state="running",
                actor=actor,
                # The transport's legacy authority slot is still 40 hex chars.
                # Full recipe authority remains in the immutable typed payload.
                base_commit=authority_digest[:40],
                targets=targets,
                payload_digest=hashlib.sha256(canonical_message(job_payload)).hexdigest(),
                payload=job_payload,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            for node_id, payload in node_payloads:
                self._agent_jobs.enqueue_in_session(
                    session,
                    job_id,
                    node_id,
                    kind,
                    authority_digest[:40],
                    payload,
                    operation_id=str(uuid.uuid4()),
                )
        self._agent_jobs.notify_available()
        return self.get(job_id)

    def _view(self, job: Job) -> RecipeOperationView:
        return RecipeOperationView(
            id=job.id,
            kind=job.kind,
            owner_id=_required_string(job.payload, "owner_id"),
            state=job.state,
            plan_digest=_required_string(job.payload, "plan_digest"),
            nodes=tuple(job.targets),
            result=dict(job.result) if isinstance(job.result, Mapping) else None,
        )

    @staticmethod
    def _release(session: Session, owner_kind: str, owner_id: str, now: datetime) -> None:
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == owner_kind,
                ResourceReservation.owner_id == owner_id,
                ResourceReservation.state == "active",
            )
        ):
            reservation.state = "released"
            reservation.released_at = now


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise RecipeOperationConflict(f"operation {key} is invalid")
    return item


__all__ = [
    "RecipeOperationConflict",
    "RecipeOperationService",
    "RecipeOperationView",
]
