"""Merged-commit-only, fail-closed reconciliation orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from .git_policy import GitPolicy
from .jobs import JobService
from .orchestration import OperationGraph, ReconciliationOrchestrator
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
    """Read the retired static plan format for explicit compatibility callers."""

    def __init__(self, repository, path: str = "inventory/reconciliation.json") -> None:
        self._repository = repository
        self._path = path

    def __call__(self, commit: str) -> Mapping[str, object]:
        parsed = self._repository.read_document(commit, self._path).parsed
        if not isinstance(parsed, Mapping):
            raise TypeError("reconciliation document must be a JSON object")
        return parsed


class CompatibilityDefinitions:
    """Explicitly opt a test fixture or legacy tool into static plan input."""

    def __init__(self, definitions: Callable[[str], Mapping[str, object]]) -> None:
        if not callable(definitions):
            raise TypeError("compatibility definitions must be callable")
        self._definitions = definitions

    def __call__(self, commit: str) -> Mapping[str, object]:
        return self._definitions(commit)


class DesiredStatePlanner(Protocol):
    def resolve(
        self, commit: str, profile_id: str, observations: Iterable[Any]
    ) -> ReconciliationPlan: ...


@dataclass(frozen=True)
class ReconciliationPlan:
    commit: str
    targets: tuple[str, ...]
    placements: Mapping[str, object]
    routes: Mapping[str, object]
    releases: Mapping[str, object]
    input_digests: Mapping[str, str]
    digest: str
    operation_graph: OperationGraph | None = None
    operation_payloads: Mapping[str, Mapping[str, object]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    agent_protocol_range: tuple[int, int] | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    plan_digest: str
    commit: str
    targets: tuple[str, ...]
    status: str
    reason: str | None = None


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for key, item in sorted(value.items()):
        if isinstance(item, Mapping):
            frozen[key] = _freeze_mapping(cast_mapping(item))
        elif isinstance(item, list):
            frozen[key] = tuple(item)
        else:
            frozen[key] = item
    return MappingProxyType(frozen)


def cast_mapping(value: Mapping[object, object]) -> Mapping[str, Any]:
    if not all(isinstance(key, str) for key in value):
        raise TypeError("reconciliation mapping keys must be strings")
    return {str(key): item for key, item in value.items()}


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
    for field_name in ("placements", "routes", "releases", "input_digests"):
        if not isinstance(content[field_name], Mapping):
            raise TypeError(f"reconciliation {field_name} must be a mapping")
    graph = values.get("operation_graph")
    if graph is not None:
        if not isinstance(graph, OperationGraph) or graph.base_commit != commit:
            raise ValueError("reconciliation operation graph is invalid")
        payloads = values.get("operation_payloads")
        protocol_range = values.get("agent_protocol_range")
        if not isinstance(payloads, Mapping):
            raise TypeError("reconciliation operation payloads must be a mapping")
        if (
            not isinstance(protocol_range, tuple)
            or len(protocol_range) != 2
            or not all(isinstance(item, int) for item in protocol_range)
            or protocol_range[0] < 1
            or protocol_range[0] > protocol_range[1]
        ):
            raise ValueError("reconciliation agent protocol range is invalid")
        content["operation_graph"] = graph.document
        content["operation_payloads"] = payloads
        content["agent_protocol_range"] = list(protocol_range)
    encoded = json.dumps(
        _jsonable(content), sort_keys=True, separators=(",", ":")
    ).encode()
    return content, encoded


def resolved_reconciliation_plan(
    *,
    commit: str,
    targets: tuple[str, ...],
    placements: Mapping[str, object],
    routes: Mapping[str, object],
    releases: Mapping[str, object],
    input_digests: Mapping[str, str],
    operation_graph: OperationGraph,
    operation_payloads: Mapping[str, Mapping[str, object]],
    agent_protocol_range: tuple[int, int],
) -> ReconciliationPlan:
    values: dict[str, object] = {
        "targets": list(targets),
        "placements": placements,
        "routes": routes,
        "releases": releases,
        "input_digests": input_digests,
        "operation_graph": operation_graph,
        "operation_payloads": operation_payloads,
        "agent_protocol_range": agent_protocol_range,
    }
    _, encoded = _plan_content(commit, values)
    return ReconciliationPlan(
        commit=commit,
        targets=tuple(sorted(targets)),
        placements=_freeze_mapping(cast_mapping(placements)),
        routes=_freeze_mapping(cast_mapping(routes)),
        releases=_freeze_mapping(cast_mapping(releases)),
        input_digests=MappingProxyType(dict(sorted(input_digests.items()))),
        digest=hashlib.sha256(encoded).hexdigest(),
        operation_graph=operation_graph,
        operation_payloads=_freeze_mapping(cast_mapping(operation_payloads)),
        agent_protocol_range=agent_protocol_range,
    )


class Reconciler:
    def __init__(
        self,
        policy,
        planner: DesiredStatePlanner | CompatibilityDefinitions,
        routes=None,
        controller=None,
        leases=None,
        *,
        jobs: JobService | None = None,
        observations: Callable[[], Iterable[Any]] | None = None,
        orchestrator: ReconciliationOrchestrator | None = None,
    ) -> None:
        if not isinstance(planner, CompatibilityDefinitions) and not callable(
            getattr(planner, "resolve", None)
        ):
            raise TypeError("reconciliation planner must be a desired-state resolver")
        self._policy = policy
        self._planner = planner
        self._routes = routes
        self._controller = controller
        self._leases = leases
        self._jobs = jobs
        self._observations = observations
        self._orchestrator = orchestrator
        self._plans: dict[str, ReconciliationPlan] = {}

    def _eligible(self, commit: str) -> None:
        eligibility = self._policy.eligible(commit)
        if not eligibility.ok:
            raise IneligibleCommit("; ".join(eligibility.reasons))

    def plan(
        self,
        commit: str,
        profile_id: str | None = None,
        observations: Iterable[Any] | None = None,
    ) -> ReconciliationPlan:
        self._eligible(commit)
        if isinstance(self._planner, CompatibilityDefinitions):
            if profile_id is not None or observations is not None:
                raise ValueError("static compatibility planning accepts no desired-state input")
            content, encoded = _plan_content(commit, self._planner(commit))
            plan = ReconciliationPlan(
                commit=commit,
                targets=tuple(content["targets"]),
                placements=_freeze_mapping(cast_mapping(content["placements"])),
                routes=_freeze_mapping(cast_mapping(content["routes"])),
                releases=_freeze_mapping(cast_mapping(content["releases"])),
                input_digests=MappingProxyType(
                    dict(sorted(cast_mapping(content["input_digests"]).items()))
                ),
                digest=hashlib.sha256(encoded).hexdigest(),
            )
        else:
            if profile_id is None:
                raise ValueError("desired-state planning requires a profile ID")
            current = observations
            if current is None and self._observations is not None:
                current = self._observations()
            if current is None:
                raise ValueError("desired-state planning requires durable observations")
            plan = self._planner.resolve(commit, profile_id, current)
            if self._orchestrator is not None:
                loaded = self._orchestrator.resolved_plan(plan.digest)
                if loaded is not None:
                    plan = self._restore_plan(plan.digest, *loaded)
                else:
                    provisional = plan.operation_graph
                    if provisional is None:
                        raise ValueError("resolved desired state lacks an operation graph")
                    graph = self._orchestrator.plan(
                        {
                            "base_commit": provisional.base_commit,
                            "targets": list(provisional.targets),
                            "route_withdrawal_generation": 0,
                            "operations": [
                                node.to_document() for node in provisional.nodes
                            ],
                        }
                    )
                    plan = resolved_reconciliation_plan(
                        commit=plan.commit,
                        targets=plan.targets,
                        placements=plan.placements,
                        routes=plan.routes,
                        releases=plan.releases,
                        input_digests=plan.input_digests,
                        operation_graph=graph,
                        operation_payloads=plan.operation_payloads,
                        agent_protocol_range=plan.agent_protocol_range or (1, 1),
                    )
                    content, _ = _plan_content(
                        plan.commit,
                        {
                            "targets": list(plan.targets),
                            "placements": plan.placements,
                            "routes": plan.routes,
                            "releases": plan.releases,
                            "input_digests": plan.input_digests,
                            "operation_graph": graph,
                            "operation_payloads": plan.operation_payloads,
                            "agent_protocol_range": plan.agent_protocol_range,
                        },
                    )
                    self._orchestrator.store_resolved_plan(
                        graph, plan.digest, cast_mapping(_jsonable(content))
                    )
        self._plans[plan.digest] = plan
        return plan

    def _restore_plan(
        self,
        plan_digest: str,
        graph: OperationGraph,
        document: Mapping[str, object],
    ) -> ReconciliationPlan:
        if document.get("operation_graph") != graph.document:
            raise ValueError("persisted resolved plan graph is invalid")
        protocol = document.get("agent_protocol_range")
        if not isinstance(protocol, list) or len(protocol) != 2:
            raise ValueError("persisted resolved plan protocol is invalid")
        plan = resolved_reconciliation_plan(
            commit=graph.base_commit,
            targets=tuple(cast_mapping(document)["targets"]),
            placements=cast_mapping(document["placements"]),
            routes=cast_mapping(document["routes"]),
            releases=cast_mapping(document["releases"]),
            input_digests=cast_mapping(document["input_digests"]),
            operation_graph=graph,
            operation_payloads=cast_mapping(document["operation_payloads"]),
            agent_protocol_range=(protocol[0], protocol[1]),
        )
        if plan.digest != plan_digest:
            raise ValueError("persisted resolved plan content is invalid")
        return plan

    def _verify_plan(self, plan: ReconciliationPlan) -> None:
        values = {
            "targets": list(plan.targets), "placements": plan.placements,
            "routes": plan.routes, "releases": plan.releases,
            "input_digests": plan.input_digests,
        }
        if plan.operation_graph is not None:
            values.update(
                {
                    "operation_graph": plan.operation_graph,
                    "operation_payloads": plan.operation_payloads,
                    "agent_protocol_range": plan.agent_protocol_range,
                }
            )
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
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            return ReconciliationResult(plan.digest, plan.commit, plan.targets, "failed", f"{type(error).__name__}: reconciliation step failed")
        return ReconciliationResult(plan.digest, plan.commit, plan.targets, "succeeded")

    def enqueue(self, plan_digest: str, actor: str, request_id: str) -> dict[str, object]:
        if self._jobs is None:
            raise RuntimeError("durable reconciliation jobs are unavailable")
        plan = None
        if self._orchestrator is not None:
            loaded = self._orchestrator.resolved_plan(plan_digest)
            if loaded is not None:
                plan = self._restore_plan(plan_digest, *loaded)
        if plan is None:
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
                "placements": _jsonable(plan.placements),
                "routes": _jsonable(plan.routes),
                "releases": _jsonable(plan.releases),
                "input_digests": dict(plan.input_digests),
                **(
                    {
                        "operation_graph": plan.operation_graph.document,
                        "operation_payloads": _jsonable(plan.operation_payloads),
                        "agent_protocol_range": list(plan.agent_protocol_range or ()),
                        "reconciliation_id": plan.operation_graph.reconciliation_id,
                    }
                    if plan.operation_graph is not None
                    else {}
                ),
            },
            request_id=request_id,
        )
        result = {"job_id": job.id, "state": job.state, "base_commit": plan.commit}
        if plan.operation_graph is not None:
            result["reconciliation_id"] = plan.operation_graph.reconciliation_id
        return result
