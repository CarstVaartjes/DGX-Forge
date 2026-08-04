"""Closed dispatch for fenced outbound-agent operations."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Mapping, Protocol

from dgx_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)

from .state import AgentAttemptRecord, AgentStateConflict, AgentStateStore
from .deadlines import MonotonicDeadline
from .nvidia_tools import InstalledToolSecurityError
from .probe import ProbeError
from .releases import (
    ReleaseDisposition,
    ReleaseEvidence,
    ReleaseInstallError,
    ReleaseInspection,
    ReleaseRequest,
    ReleaseValidationError,
)
from .workloads import (
    WorkloadAction,
    WorkloadDisposition,
    WorkloadEvidence,
    WorkloadExecutionError,
    WorkloadInspection,
    WorkloadRequest,
    WorkloadValidationError,
)


class UnsupportedOperation(AgentProtocolError):
    """The compiled agent has no handler for this operation."""


class NodeProbe(Protocol):
    def collect(self, deadline: datetime) -> Mapping[str, object]: ...


class ReleaseInstallerBoundary(Protocol):
    def install(
        self, request: ReleaseRequest, deadline: MonotonicDeadline
    ) -> ReleaseEvidence: ...

    def inspect(
        self, request: ReleaseRequest, deadline: MonotonicDeadline
    ) -> ReleaseInspection: ...


class WorkloadOperationsBoundary(Protocol):
    def execute(
        self,
        request: WorkloadRequest,
        deadline: MonotonicDeadline,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
    ) -> WorkloadEvidence: ...

    def inspect(
        self,
        request: WorkloadRequest,
        deadline: MonotonicDeadline,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
    ) -> WorkloadInspection: ...


@dataclass(frozen=True)
class OperationContext:
    node_id: str
    state: AgentStateStore
    probe: NodeProbe
    releases: ReleaseInstallerBoundary | None = None
    workloads: WorkloadOperationsBoundary | None = None


class InspectionDisposition(StrEnum):
    READY = "ready"
    SAFE_TO_RETRY = "safe-to-retry"
    COMPLETED = "completed"
    COMPENSATE = "compensate"
    OPERATOR_INTERVENTION = "operator-intervention"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class OperationInspection:
    disposition: InspectionDisposition
    result: AgentResult | None = None
    canonical_result: bytes | None = None
    evidence: Mapping[str, object] | None = None


@dataclass(frozen=True)
class OperationExecution(Mapping[str, Any]):
    """Immutable execution record that also exposes the result evidence mapping."""

    result: AgentResult
    canonical_result: bytes
    replayed: bool

    def __getitem__(self, key: str) -> Any:
        return self.result.result[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.result.result)

    def __len__(self) -> int:
        return len(self.result.result)


class OperationRegistry:
    """A source-defined registry; it deliberately has no plugin discovery."""

    def execute(
        self, claim: AgentClaim, context: OperationContext
    ) -> OperationExecution:
        request = self._validate(claim, context)
        exact = context.state.lookup_exact(claim)
        if exact is not None and exact.result is not None:
            assert exact.canonical_result is not None
            return OperationExecution(exact.result, exact.canonical_result, True)
        if exact is not None:
            inspection = _inspect_request(claim, request, context)
            if (
                inspection.disposition is InspectionDisposition.COMPLETED
                and inspection.evidence is not None
            ):
                recovered = _result(
                    claim,
                    "succeeded",
                    {"status": "ok", "evidence": inspection.evidence},
                )
                finished = context.state.finish(recovered)
                assert (
                    finished.result is not None
                    and finished.canonical_result is not None
                )
                return OperationExecution(
                    finished.result, finished.canonical_result, True
                )
            if inspection.disposition is not InspectionDisposition.SAFE_TO_RETRY:
                raise AgentStateConflict(
                    "interrupted mutation requires explicit disposition"
                )
        pending = context.state.recover_pending()
        if pending is not None:
            _require_exact(pending, claim)
            if pending.result is not None and pending.canonical_result is not None:
                return OperationExecution(
                    pending.result, pending.canonical_result, True
                )
        try:
            execution_deadline = MonotonicDeadline.bind(claim.deadline)
        except Exception as error:
            raise AgentProtocolError("claim deadline has expired") from error
        record = context.state.begin(claim)
        if record.result is not None:
            assert record.canonical_result is not None
            return OperationExecution(record.result, record.canonical_result, True)
        try:
            evidence = _execute_request(
                claim, request, context, execution_deadline
            )
            result = _result(claim, "succeeded", {"status": "ok", "evidence": evidence})
        except Exception as error:
            error_code = _stable_error_code(error, claim.operation)
            result = _result(
                claim,
                "failed",
                {"status": "failed", "error_code": error_code},
            )
        finished = context.state.finish(result)
        assert finished.result is not None and finished.canonical_result is not None
        return OperationExecution(finished.result, finished.canonical_result, False)

    def inspect(
        self, claim: AgentClaim, context: OperationContext
    ) -> OperationInspection:
        request = self._validate(claim, context)
        exact = context.state.lookup_exact(claim)
        if exact is not None:
            if exact.result is None:
                return _inspect_request(claim, request, context)
            return OperationInspection(
                InspectionDisposition.COMPLETED,
                exact.result,
                exact.canonical_result,
            )
        unresolved = context.state.recover_active()
        if unresolved is None:
            unresolved = context.state.recover_pending()
        if unresolved is None:
            return OperationInspection(InspectionDisposition.READY)
        _require_exact(unresolved, claim)
        if unresolved.result is None:
            return _inspect_request(claim, request, context)
        return OperationInspection(
            InspectionDisposition.COMPLETED,
            unresolved.result,
            unresolved.canonical_result,
        )

    @staticmethod
    def _validate(
        claim: AgentClaim, context: OperationContext
    ) -> ReleaseRequest | WorkloadRequest | None:
        if type(claim) is not AgentClaim:
            raise UnsupportedOperation("operation is not compiled into this agent")
        if claim.node_id != context.node_id:
            raise AgentProtocolError("claim node does not match this agent")
        if claim.operation is AgentOperation.NODE_PROBE:
            if claim.payload:
                raise AgentProtocolError("node probe payload must be empty")
            return None
        if claim.operation is AgentOperation.RELEASE_INSTALL:
            if context.releases is None:
                raise UnsupportedOperation("release installation is unavailable")
            try:
                return ReleaseRequest.parse(claim.payload)
            except ReleaseValidationError as error:
                raise AgentProtocolError("release payload is invalid") from error
        action = _WORKLOAD_ACTIONS.get(claim.operation)
        if action is not None:
            if context.workloads is None:
                raise UnsupportedOperation("workload operations are unavailable")
            try:
                return WorkloadRequest.parse(action, claim.payload)
            except WorkloadValidationError as error:
                raise AgentProtocolError("workload payload is invalid") from error
        raise UnsupportedOperation("operation is not compiled into this agent")


_WORKLOAD_ACTIONS = {
    AgentOperation.WORKLOAD_PREPARE: WorkloadAction.PREPARE,
    AgentOperation.WORKLOAD_START: WorkloadAction.START,
    AgentOperation.WORKLOAD_STOP: WorkloadAction.STOP,
    AgentOperation.WORKLOAD_HEALTH: WorkloadAction.HEALTH,
    AgentOperation.WORKLOAD_VERIFY: WorkloadAction.VERIFY,
}


def _execute_request(
    claim: AgentClaim,
    request: ReleaseRequest | WorkloadRequest | None,
    context: OperationContext,
    deadline: MonotonicDeadline,
) -> Mapping[str, object]:
    if request is None:
        return context.probe.collect(claim.deadline)
    if isinstance(request, ReleaseRequest):
        assert context.releases is not None
        return context.releases.install(
            request, deadline
        ).to_mapping()
    assert context.workloads is not None
    return context.workloads.execute(
        request,
        deadline,
        claim.job_id,
        claim.operation_id,
        claim.attempt,
        claim.fence,
    ).to_mapping()


def _inspect_request(
    claim: AgentClaim,
    request: ReleaseRequest | WorkloadRequest | None,
    context: OperationContext,
) -> OperationInspection:
    if request is None or claim.operation in {
        AgentOperation.WORKLOAD_HEALTH,
        AgentOperation.WORKLOAD_VERIFY,
    }:
        return OperationInspection(InspectionDisposition.SAFE_TO_RETRY)
    if isinstance(request, ReleaseRequest):
        assert context.releases is not None
        inspection = context.releases.inspect(request, _recovery_deadline())
        mapping = {
            ReleaseDisposition.READY: InspectionDisposition.OPERATOR_INTERVENTION,
            ReleaseDisposition.SAFE_TO_RESUME: InspectionDisposition.SAFE_TO_RETRY,
            ReleaseDisposition.COMPLETED: InspectionDisposition.COMPLETED,
            ReleaseDisposition.OPERATOR_INTERVENTION: InspectionDisposition.OPERATOR_INTERVENTION,
        }
    else:
        assert context.workloads is not None
        inspection = context.workloads.inspect(
            request,
            _recovery_deadline(),
            claim.job_id,
            claim.operation_id,
            claim.attempt,
            claim.fence,
        )
        mapping = {
            WorkloadDisposition.READY: InspectionDisposition.OPERATOR_INTERVENTION,
            WorkloadDisposition.SAFE_TO_RETRY: InspectionDisposition.SAFE_TO_RETRY,
            WorkloadDisposition.COMPLETED: InspectionDisposition.COMPLETED,
            WorkloadDisposition.COMPENSATE: InspectionDisposition.COMPENSATE,
            WorkloadDisposition.OPERATOR_INTERVENTION: InspectionDisposition.OPERATOR_INTERVENTION,
        }
    evidence = None if inspection.evidence is None else inspection.evidence.to_mapping()
    return OperationInspection(mapping[inspection.disposition], evidence=evidence)


def _recovery_deadline() -> MonotonicDeadline:
    return MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=15))


def _require_exact(record: AgentAttemptRecord, claim: AgentClaim) -> None:
    if record.canonical_claim != canonical_message(claim):
        raise AgentStateConflict("claim conflicts with unresolved state")


def _result(
    claim: AgentClaim, state: str, evidence: Mapping[str, Any]
) -> AgentResult:
    return AgentResult(
        schema_version=claim.schema_version,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state=state,
        result=evidence,
    )


_PROBE_ERROR_CODES = frozenset(
    {
        "probe_failed",
        "probe_timeout",
        "probe_output_limit",
        "probe_result_limit",
        "probe_collector_failed",
        "probe_security_failure",
    }
)


def _stable_error_code(error: Exception, operation: AgentOperation) -> str:
    if operation is AgentOperation.RELEASE_INSTALL:
        code = getattr(error, "error_code", None)
        return (
            code
            if isinstance(error, ReleaseInstallError)
            and code == "release_install_failed"
            else "release_install_failed"
        )
    if operation in _WORKLOAD_ACTIONS:
        code = getattr(error, "error_code", None)
        return (
            code
            if isinstance(error, WorkloadExecutionError) and code == "workload_failed"
            else "workload_failed"
        )
    if not isinstance(error, (ProbeError, InstalledToolSecurityError)):
        return "probe_failed"
    code = getattr(error, "error_code", None)
    return code if code in _PROBE_ERROR_CODES else "probe_failed"
