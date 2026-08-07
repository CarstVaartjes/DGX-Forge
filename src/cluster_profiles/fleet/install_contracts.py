"""Immutable state contracts for resumable per-node installation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from ._redact import redact_message
from .types import ManagementEndpoint, NodeId

InstallationState = Literal[
    "discovered",
    "identity-gated",
    "inventoried",
    "key-installed",
    "hardened",
    "policy-applied",
    "post-inventoried",
    "accepted",
    "failed",
]

_NEXT_STATE: dict[InstallationState, InstallationState] = {
    "discovered": "identity-gated",
    "identity-gated": "inventoried",
    "inventoried": "key-installed",
    "key-installed": "hardened",
    "hardened": "policy-applied",
    "policy-applied": "post-inventoried",
    "post-inventoried": "accepted",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


class InvalidInstallationTransition(ValueError):
    """An installation attempted to skip, repeat, or leave a terminal state."""


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("installation timestamps must be timezone-aware")


@dataclass(frozen=True)
class InstallationRequest:
    node_id: NodeId
    display_name: str
    endpoint: ManagementEndpoint
    labels: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("installation display name must not be blank")
        reference = self.endpoint.credential_ref
        if reference is None or not reference.startswith("secret://"):
            raise ValueError("installation requires a secret:// credential reference")
        labels = dict(self.labels)
        if any(not key.strip() or not value.strip() for key, value in labels.items()):
            raise ValueError("installation label keys and values must not be blank")
        object.__setattr__(self, "labels", MappingProxyType(labels))

    def as_public_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id.value,
            "display_name": self.display_name,
            "host": self.endpoint.host,
            "user": self.endpoint.user,
            "port": self.endpoint.port,
            "credential_ref": self.endpoint.credential_ref,
            "labels": dict(self.labels),
        }


@dataclass(frozen=True)
class InstallationStep:
    state: InstallationState
    evidence_digest: str
    completed_at: datetime


@dataclass(frozen=True)
class InstallationJournal:
    request: InstallationRequest
    state: InstallationState
    steps: tuple[InstallationStep, ...]
    created_at: datetime
    updated_at: datetime
    failure_reason: str | None = None
    waiting_reason: str | None = None
    retry_count: int = 0
    resume_count: int = 0

    @classmethod
    def start(
        cls,
        request: InstallationRequest,
        *,
        at: datetime,
    ) -> InstallationJournal:
        _aware(at)
        return cls(
            request=request,
            state="discovered",
            steps=(),
            created_at=at,
            updated_at=at,
        )

    def advance(
        self,
        state: InstallationState,
        *,
        evidence_digest: str,
        at: datetime,
    ) -> InstallationJournal:
        _aware(at)
        if self.waiting_reason is not None:
            raise InvalidInstallationTransition(
                "cannot advance installation while waiting for operator"
            )
        if _SHA256.fullmatch(evidence_digest) is None:
            raise ValueError("installation evidence digest must be lowercase SHA-256")
        expected = _NEXT_STATE.get(self.state)
        if expected != state:
            raise InvalidInstallationTransition(
                f"cannot transition installation from {self.state} to {state}"
            )
        if at < self.updated_at:
            raise ValueError("installation timestamp cannot move backwards")
        step = InstallationStep(
            state=state,
            evidence_digest=evidence_digest,
            completed_at=at,
        )
        return replace(
            self,
            state=state,
            steps=(*self.steps, step),
            updated_at=at,
        )

    def wait(self, *, reason: str, at: datetime) -> InstallationJournal:
        _aware(at)
        if self.state in {"accepted", "failed"}:
            raise InvalidInstallationTransition(
                f"cannot wait from terminal installation state {self.state}"
            )
        if not reason.strip():
            raise ValueError("operator wait reason must not be blank")
        if at < self.updated_at:
            raise ValueError("installation timestamp cannot move backwards")
        return replace(
            self,
            updated_at=at,
            waiting_reason=redact_message(reason)[:1024],
        )

    def resume(self, *, at: datetime) -> InstallationJournal:
        _aware(at)
        if self.waiting_reason is None:
            raise InvalidInstallationTransition("installation is not waiting")
        if at < self.updated_at:
            raise ValueError("installation timestamp cannot move backwards")
        return replace(
            self,
            updated_at=at,
            waiting_reason=None,
            resume_count=self.resume_count + 1,
        )

    def fail(self, *, reason: str, at: datetime) -> InstallationJournal:
        _aware(at)
        if self.state in {"accepted", "failed"}:
            raise InvalidInstallationTransition(
                f"cannot fail installation from terminal state {self.state}"
            )
        if at < self.updated_at:
            raise ValueError("installation timestamp cannot move backwards")
        return replace(
            self,
            state="failed",
            updated_at=at,
            failure_reason=redact_message(reason)[:1024],
            waiting_reason=None,
        )

    def retry(self, *, at: datetime) -> InstallationJournal:
        _aware(at)
        if self.state != "failed":
            raise InvalidInstallationTransition(
                f"cannot retry installation from state {self.state}"
            )
        if at < self.updated_at:
            raise ValueError("installation timestamp cannot move backwards")
        resumed_state: InstallationState = (
            self.steps[-1].state if self.steps else "discovered"
        )
        return replace(
            self,
            state=resumed_state,
            updated_at=at,
            failure_reason=None,
            retry_count=self.retry_count + 1,
        )
