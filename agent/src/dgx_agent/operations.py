"""Closed dispatch for fenced outbound-agent operations."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
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
from .nvidia_tools import InstalledToolSecurityError
from .probe import ProbeError


class UnsupportedOperation(AgentProtocolError):
    """The compiled agent has no handler for this operation."""


class NodeProbe(Protocol):
    def collect(self, deadline: datetime) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class OperationContext:
    node_id: str
    state: AgentStateStore
    probe: NodeProbe


class InspectionDisposition(StrEnum):
    READY = "ready"
    SAFE_TO_RETRY = "safe-to-retry"
    COMPLETED = "completed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class OperationInspection:
    disposition: InspectionDisposition
    result: AgentResult | None = None
    canonical_result: bytes | None = None


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
        self._validate(claim, context)
        pending = context.state.recover_pending()
        if pending is not None:
            _require_exact(pending, claim)
            if pending.result is not None and pending.canonical_result is not None:
                return OperationExecution(
                    pending.result, pending.canonical_result, True
                )
        if claim.deadline <= datetime.now(UTC):
            raise AgentProtocolError("claim deadline has expired")
        record = context.state.begin(claim)
        if record.result is not None:
            assert record.canonical_result is not None
            return OperationExecution(record.result, record.canonical_result, True)
        try:
            evidence = context.probe.collect(claim.deadline)
            result = _result(claim, "succeeded", {"status": "ok", "evidence": evidence})
        except Exception as error:
            error_code = _stable_error_code(error)
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
        self._validate(claim, context)
        unresolved = context.state.recover_active()
        if unresolved is None:
            unresolved = context.state.recover_pending()
        if unresolved is None:
            return OperationInspection(InspectionDisposition.READY)
        _require_exact(unresolved, claim)
        if unresolved.result is None:
            return OperationInspection(InspectionDisposition.SAFE_TO_RETRY)
        return OperationInspection(
            InspectionDisposition.COMPLETED,
            unresolved.result,
            unresolved.canonical_result,
        )

    @staticmethod
    def _validate(claim: AgentClaim, context: OperationContext) -> None:
        if type(claim) is not AgentClaim or claim.operation is not AgentOperation.NODE_PROBE:
            raise UnsupportedOperation("operation is not compiled into this agent")
        if claim.payload:
            raise AgentProtocolError("node probe payload must be empty")
        if claim.node_id != context.node_id:
            raise AgentProtocolError("claim node does not match this agent")


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


def _stable_error_code(error: Exception) -> str:
    if not isinstance(error, (ProbeError, InstalledToolSecurityError)):
        return "probe_failed"
    code = getattr(error, "error_code", None)
    return code if code in _PROBE_ERROR_CODES else "probe_failed"
