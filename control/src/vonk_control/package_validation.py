"""Trust-bounded workload validation scheduling and lifecycle.

Validation is deliberately a control-plane concern.  It can enqueue prepare and
verify work on an explicitly selected disposable/canary node, but this module
has no activation or desired-state mutation operation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

from .package_compatibility import CompatibilityEvaluator, CompatibilityReport


class ValidationError(ValueError):
    """Candidate validation is not eligible or evidence is invalid."""


def _get(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    else:
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
    return default


def _digest(value: object) -> str:
    if isinstance(value, str):
        return hashlib.sha256(value.encode()).hexdigest()
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _lock(candidate: object) -> object:
    value = _get(candidate, "lock", "release_lock", "lock_bytes")
    if value is None:
        raise ValidationError("candidate has no release lock")
    return value


def _lock_digest(lock: object) -> str:
    digest = _get(lock, "digest", "release_digest")
    if isinstance(digest, str) and len(digest) == 64:
        return digest
    canonical = _get(lock, "canonical_bytes")
    if callable(canonical):
        canonical = canonical()
    return _digest(canonical if canonical is not None else lock)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


@dataclass(frozen=True)
class ValidationPlan:
    run_id: str
    candidate_id: str
    release_digest: str
    node_ids: tuple[str, ...]
    operations: tuple[Mapping[str, object], ...]
    compatibility_digest: str
    policy_digest: str
    digest: str
    canonical_bytes: bytes

    @property
    def fleet_digest(self) -> str:
        """Fleet binding alias; compatibility evidence is the fleet projection digest."""

        return self.compatibility_digest

    @property
    def operation_kinds(self) -> tuple[str, ...]:
        return tuple(str(operation["kind"]) for operation in self.operations)


@dataclass(frozen=True)
class ValidationState:
    run_id: str
    candidate_id: str
    release_digest: str
    state: str
    attempt: int
    reason_code: str | None = None
    evidence_digest: str | None = None
    compatibility_digest: str | None = None
    updated_at: datetime | None = None

    @property
    def passed(self) -> bool:
        return self.state == "passed"


@dataclass
class _Run:
    plan: ValidationPlan
    state: str = "planned"
    attempt: int = 0
    reason_code: str | None = None
    evidence_digest: str | None = None
    updated_at: datetime | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)


class ValidationController:
    """Plan and advance bounded package validation runs.

    ``candidate_loader`` and ``fleet_loader`` are intentionally narrow callbacks
    so W11's operational models can be introduced without changing this trust
    boundary.  ``enqueue`` receives an immutable operation mapping; callers may
    route it through the existing fenced Job/AgentOperation services.  The
    default second operation is ``package.health``: the package ABI intentionally
    has no separate ``package.verify`` verb, and health evidence carries the
    validation/verification result.
    """

    def __init__(
        self,
        candidate_loader: Callable[[str], object],
        *,
        fleet_loader: Callable[[], object],
        enqueue: Callable[[Mapping[str, object]], object] | None = None,
        runner: Callable[[ValidationState], Mapping[str, object]] | None = None,
        evaluator: CompatibilityEvaluator | None = None,
        family_loader: Callable[[str], object] | None = None,
        clock: Callable[[], datetime] | None = None,
        canary_nodes: int | None = None,
        verify_operation: str = "package.health",
    ) -> None:
        if not callable(candidate_loader) or not callable(fleet_loader):
            raise TypeError("candidate and fleet loaders are required")
        self._candidate_loader = candidate_loader
        self._fleet_loader = fleet_loader
        self._enqueue = enqueue
        self._runner = runner
        self._evaluator = evaluator or CompatibilityEvaluator()
        self._family_loader = family_loader
        self._clock = clock or (lambda: datetime.now(UTC))
        self._canary_nodes = canary_nodes
        if not isinstance(verify_operation, str) or not verify_operation.strip():
            raise ValueError("validation verify operation is invalid")
        self._verify_operation = verify_operation
        self._runs: dict[str, _Run] = {}

    def _candidate(self, candidate_id: str) -> object:
        candidate = self._candidate_loader(candidate_id)
        if candidate is None:
            raise ValidationError("candidate is unavailable")
        state = _get(candidate, "state", default="resolved")
        if state not in {"resolved", "validation", "validated", "promotion-ready"}:
            raise ValidationError("candidate is not resolved")
        return candidate

    def _policy(self, candidate_id: str, candidate: object) -> Mapping[str, object]:
        policy = _get(candidate, "policy")
        if policy is None and self._family_loader is not None:
            family_id = _get(candidate, "family_id")
            family = self._family_loader(str(family_id))
            policy = _get(family, "policy")
        if not isinstance(policy, Mapping):
            policy = {}
        return policy

    def _required_evidence(self, candidate_id: str, candidate: object) -> tuple[str, ...]:
        policy = self._policy(candidate_id, candidate)
        values = policy.get("required_evidence", policy.get("required_evidence_kinds", ()))
        if isinstance(values, str):
            return (values,)
        return tuple(str(item) for item in values) if isinstance(values, Sequence) else ()

    def _policy_gates(self, candidate_id: str, candidate: object, lock: object) -> None:
        """Reject missing trust and license inputs before scheduling any GPU node work."""

        for name in ("signature_verified", "provenance_verified"):
            if _get(candidate, name, default=True) is False or _get(lock, name, default=True) is False:
                raise ValidationError("trust evidence is not verified")
        provenance = _get(lock, "provenance", default=())
        if not isinstance(provenance, Sequence) or isinstance(provenance, (str, bytes)):
            raise ValidationError("provenance-missing")
        records: list[object] = list(provenance)
        # Checksum and similar component-scoped evidence lives on each
        # immutable descriptor, while provenance records live at lock level.
        # The policy vocabulary is intentionally generic; it must not assume a
        # fixed model/runtime catalog.
        for descriptor_name in ("components", "adapter"):
            descriptors = _get(lock, descriptor_name, default=())
            if descriptor_name == "adapter":
                descriptors = (descriptors,)
            if isinstance(descriptors, Sequence) and not isinstance(descriptors, (str, bytes)):
                for descriptor in descriptors:
                    evidence = _get(descriptor, "evidence", default=())
                    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
                        records.extend(evidence)
        provenance_kinds = {
            str(_get(item, "kind"))
            for item in records
            if _get(item, "kind") is not None
        }
        missing_evidence = sorted(set(self._required_evidence(candidate_id, candidate)) - provenance_kinds)
        if missing_evidence:
            raise ValidationError("required evidence is missing")
        accepted = _get(candidate, "license_accepted", "license_acceptance", default=True)
        if accepted is False or (
            isinstance(accepted, Mapping)
            and accepted.get("accepted") is not True
        ):
            raise ValidationError("license acceptance is missing")

    def plan(self, candidate_id: str) -> ValidationPlan:
        candidate = self._candidate(candidate_id)
        lock = _lock(candidate)
        self._policy_gates(candidate_id, candidate, lock)
        release_digest = _lock_digest(lock)
        report: CompatibilityReport = self._evaluator.evaluate(lock, self._fleet_loader())
        if not report.compatible_node_ids:
            raise ValidationError("no compatible nodes")
        canary = _get(self._policy(candidate_id, candidate), "canary")
        limit = self._canary_nodes
        if isinstance(canary, Mapping) and isinstance(canary.get("node_count"), int):
            limit = canary["node_count"] if limit is None else min(limit, canary["node_count"])
        if limit is not None:
            node_ids = report.compatible_node_ids[: max(1, limit)]
        else:
            node_ids = (report.compatible_node_ids[0],)
        policy = self._policy(candidate_id, candidate)
        policy_digest = _digest(policy)
        operations = tuple(
            {
                "kind": kind,
                "node_ids": list(node_ids),
                "payload": {
                    "schema_version": 1,
                    "candidate_id": candidate_id,
                    "release_digest": release_digest,
                    "compatibility_digest": report.digest,
                },
            }
            for kind in ("package.prepare", self._verify_operation)
        )
        deployment = _get(candidate, "deployment")
        if isinstance(deployment, Mapping):
            deployment_id = deployment.get("deployment_id")
            deployment_digest = _get(candidate, "deployment_digest")
            deployment_config_digest = _get(
                candidate, "deployment_config_digest", default=deployment_digest
            )
            if not all(
                isinstance(value, str) and len(value) == 64
                for value in (deployment_digest, deployment_config_digest)
            ) or not isinstance(deployment_id, str):
                raise ValidationError("validation deployment identity is invalid")
            operations = tuple(
                {
                    **operation,
                    "payload": {
                        **dict(operation["payload"]),
                        "deployment_id": deployment_id,
                        "deployment_digest": deployment_digest,
                        "deployment": dict(deployment),
                        "deployment_config_digest": deployment_config_digest,
                    },
                }
                for operation in operations
            )
        value = {
            "candidate_id": candidate_id,
            "compatibility_digest": report.digest,
            "node_ids": list(node_ids),
            "operations": list(operations),
            "policy_digest": policy_digest,
            "release_digest": release_digest,
        }
        canonical = _canonical(value)
        plan = ValidationPlan(
            run_id=str(uuid.uuid4()),
            candidate_id=candidate_id,
            release_digest=release_digest,
            node_ids=tuple(node_ids),
            operations=tuple(MappingProxyType(dict(operation)) for operation in operations),
            compatibility_digest=report.digest,
            policy_digest=policy_digest,
            digest=hashlib.sha256(canonical).hexdigest(),
            canonical_bytes=canonical,
        )
        run = _Run(plan=plan, updated_at=self._clock())
        self._runs[plan.run_id] = run
        if self._enqueue is not None:
            for operation in plan.operations:
                self._enqueue(operation)
        return plan

    def _state(self, run: _Run) -> ValidationState:
        return ValidationState(
            run_id=run.plan.run_id,
            candidate_id=run.plan.candidate_id,
            release_digest=run.plan.release_digest,
            state=run.state,
            attempt=run.attempt,
            reason_code=run.reason_code,
            evidence_digest=run.evidence_digest,
            compatibility_digest=run.plan.compatibility_digest,
            updated_at=run.updated_at,
        )

    def get(self, run_id: str) -> ValidationState:
        try:
            return self._state(self._runs[run_id])
        except KeyError:
            raise KeyError(run_id) from None

    def advance(self, run_id: str) -> ValidationState:
        try:
            run = self._runs[run_id]
        except KeyError:
            raise KeyError(run_id) from None
        now = self._clock()
        current_candidate = self._candidate_loader(run.plan.candidate_id)
        if _lock_digest(_lock(current_candidate)) != run.plan.release_digest:
            raise ValidationError("candidate lock bytes changed")
        current_report = self._evaluator.evaluate(
            _lock(current_candidate), self._fleet_loader()
        )
        if current_report.digest != run.plan.compatibility_digest:
            raise ValidationError("fleet compatibility evidence changed")
        if run.state in {"planned", "queued"}:
            run.state = "running"
            run.attempt += 1
            run.updated_at = now
            if self._runner is None:
                return self._state(run)
        if run.state != "running":
            return self._state(run)
        if self._runner is None:
            return self._state(run)
        try:
            result = self._runner(self._state(run))
        except (TimeoutError, ConnectionError, OSError):
            run.state = "retryable"
            run.reason_code = "upstream-outage"
            run.updated_at = now
            return self._state(run)
        if not isinstance(result, Mapping):
            raise ValidationError("validation runner result is invalid")
        status = result.get("status")
        if status == "running":
            run.state = "running"
            run.reason_code = None
        elif status in {"retryable", "upstream-outage"}:
            run.state = "retryable"
            run.reason_code = "upstream-outage"
        elif status in {"failed", "canary-failed", "canary-loss", "rejected"}:
            run.state = "rejected" if status == "rejected" else "failed"
            default_reason = (
                "canary-loss"
                if status in {"canary-failed", "canary-loss"}
                else "validation-failed"
            )
            run.reason_code = str(result.get("reason_code", default_reason))
        elif status == "passed":
            evidence = result.get("evidence", {})
            if not isinstance(evidence, Mapping):
                raise ValidationError("validation evidence is invalid")
            required = self._required_evidence(run.plan.candidate_id, self._candidate_loader(run.plan.candidate_id))
            missing = [kind for kind in required if kind not in evidence]
            if missing:
                raise ValidationError("required evidence is missing")
            run.state = "passed"
            run.evidence = dict(evidence)
            run.evidence_digest = _digest(evidence)
            run.reason_code = None
        else:
            raise ValidationError("validation runner status is invalid")
        run.updated_at = now
        return self._state(run)


__all__ = ["ValidationController", "ValidationError", "ValidationPlan", "ValidationState"]
