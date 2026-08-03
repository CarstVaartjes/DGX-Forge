"""Merged-commit-only, fail-closed reconciliation orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from .git_policy import GitPolicy
from .jobs import JobService
from .proposals import ProposalService


class ChangeService:
    def __init__(self, proposals: ProposalService, policy: GitPolicy) -> None:
        self._proposals = proposals
        self._policy = policy

    def submit(self, digest: str, actor: str, request_id: str) -> dict[str, object]:
        preview = self._proposals.apply(digest)
        return asdict(self._policy.submit(preview, actor=actor, request_id=request_id))


class IneligibleCommit(RuntimeError):
    pass


class RepositoryDefinitions:
    def __init__(self, repository, path: str = "inventory/reconciliation.json") -> None:
        self._repository = repository
        self._path = path

    def __call__(self, commit: str) -> Mapping[str, object]:
        parsed = self._repository.read_document(commit, self._path).parsed
        if not isinstance(parsed, Mapping):
            raise ValueError("reconciliation document must be a JSON object")
        return parsed


@dataclass(frozen=True)
class ReconciliationPlan:
    commit: str
    targets: tuple[str, ...]
    placements: Mapping[str, object]
    routes: Mapping[str, object]
    releases: Mapping[str, object]
    input_digests: Mapping[str, str]
    digest: str


@dataclass(frozen=True)
class ReconciliationResult:
    plan_digest: str
    commit: str
    targets: tuple[str, ...]
    status: str
    reason: str | None = None


def _plan_content(commit: str, values: Mapping[str, object]) -> tuple[dict[str, object], bytes]:
    targets = values.get("targets")
    if not isinstance(targets, list) or not targets or not all(isinstance(target, str) and target.strip() for target in targets):
        raise ValueError("reconciliation definitions require nonempty string targets")
    ordered_targets = sorted(set(targets))
    if len(ordered_targets) != len(targets):
        raise ValueError("reconciliation targets must be unique")
    content = {
        "commit": commit,
        "targets": ordered_targets,
        "placements": values.get("placements", {}),
        "routes": values.get("routes", {}),
        "releases": values.get("releases", {}),
        "input_digests": values.get("input_digests", {}),
    }
    for field in ("placements", "routes", "releases", "input_digests"):
        if not isinstance(content[field], Mapping):
            raise ValueError(f"reconciliation {field} must be a mapping")
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return content, encoded


class Reconciler:
    def __init__(
        self,
        policy,
        definitions: Callable[[str], Mapping[str, object]],
        routes=None,
        controller=None,
        leases=None,
        *,
        jobs: JobService | None = None,
    ) -> None:
        self._policy = policy
        self._definitions = definitions
        self._routes = routes
        self._controller = controller
        self._leases = leases
        self._jobs = jobs
        self._plans: dict[str, ReconciliationPlan] = {}

    def _eligible(self, commit: str) -> None:
        eligibility = self._policy.eligible(commit)
        if not eligibility.ok:
            raise IneligibleCommit("; ".join(eligibility.reasons))

    def plan(self, commit: str) -> ReconciliationPlan:
        self._eligible(commit)
        content, encoded = _plan_content(commit, self._definitions(commit))
        digest = hashlib.sha256(encoded).hexdigest()
        plan = ReconciliationPlan(
            commit=commit,
            targets=tuple(content["targets"]),
            placements=dict(content["placements"]),
            routes=dict(content["routes"]),
            releases=dict(content["releases"]),
            input_digests=dict(content["input_digests"]),
            digest=digest,
        )
        self._plans[digest] = plan
        return plan

    def _verify_plan(self, plan: ReconciliationPlan) -> None:
        values = {
            "targets": list(plan.targets), "placements": plan.placements,
            "routes": plan.routes, "releases": plan.releases,
            "input_digests": plan.input_digests,
        }
        _, encoded = _plan_content(plan.commit, values)
        if hashlib.sha256(encoded).hexdigest() != plan.digest:
            raise ValueError("reconciliation plan content no longer matches its digest")

    def execute(self, plan: ReconciliationPlan) -> ReconciliationResult:
        if self._routes is None or self._controller is None or self._leases is None:
            raise RuntimeError("reconciliation execution is available only in the worker")
        self._verify_plan(plan)
        self._eligible(plan.commit)
        self._routes.withdraw(plan.targets)
        try:
            with self._leases.acquire(tuple(sorted(plan.targets))):
                self._controller.apply(plan)
                if self._controller.verify(plan) is not True:
                    raise RuntimeError("acceptance verification failed")
                self._routes.publish_atomically(dict(plan.routes))
        except Exception as error:
            return ReconciliationResult(plan.digest, plan.commit, plan.targets, "failed", f"{type(error).__name__}: reconciliation step failed")
        return ReconciliationResult(plan.digest, plan.commit, plan.targets, "succeeded")

    def enqueue(self, plan_digest: str, actor: str, request_id: str) -> dict[str, object]:
        if self._jobs is None:
            raise RuntimeError("durable reconciliation jobs are unavailable")
        try:
            plan = self._plans[plan_digest]
        except KeyError:
            raise ValueError("unknown reconciliation plan digest") from None
        self._verify_plan(plan)
        self._eligible(plan.commit)
        job = self._jobs.enqueue(
            "reconcile", actor, plan.commit, list(plan.targets),
            {
                "plan_digest": plan.digest,
                "placements": dict(plan.placements),
                "routes": dict(plan.routes),
                "releases": dict(plan.releases),
                "input_digests": dict(plan.input_digests),
            },
            request_id=request_id,
        )
        return {"job_id": job.id, "state": job.state, "base_commit": plan.commit}
