"""Bounded, generic workload-package failure evidence.

The package lane deliberately reports a small, stable taxonomy.  A failure
record contains only immutable workload identities and a redacted diagnostic;
it never carries source URLs, credentials, adapter output, or payload bytes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

_DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_NODE = re.compile(r"spk_[0-9a-f]{32}\Z")
_FENCE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


class PackageFailureReason(StrEnum):
    """Stable reason codes shared by control-plane evidence and operators."""

    DISCOVERY_UNAVAILABLE = "discovery-unavailable"
    UPSTREAM_MUTATION = "upstream-mutation"
    RESOLUTION_UNSUPPORTED = "resolution-unsupported"
    TRUST_PROVENANCE_FAILURE = "trust-provenance-failure"
    POLICY_LICENSE_REJECTION = "policy-license-rejection"
    INCOMPATIBLE_PLATFORM = "incompatible-platform"
    MISSING_CREDENTIAL = "missing-credential"
    INSUFFICIENT_CAPACITY = "insufficient-capacity"
    RETRYABLE_TRANSPORT = "retryable-transport"
    DIGEST_SIZE_MISMATCH = "digest-size-mismatch"
    ENVIRONMENT_BUILD_FAILURE = "environment-build-failure"
    PACKAGE_VALIDATION_FAILURE = "package-validation-failure"
    ACTIVATION_FAILURE = "activation-failure"
    RUNTIME_HEALTH_FAILURE = "runtime-health-failure"
    ROLLBACK_FAILURE = "rollback-failure"
    CANCELLATION = "cancellation"
    CORRUPT_STORE = "corrupt-store"
    GC_INTERRUPTED = "gc-interrupted"


class PackageFailureDisposition(StrEnum):
    """How reconciliation may safely proceed after a package failure."""

    SAFE_TO_RETRY = "safe-to-retry"
    COMPENSATE = "compensate"
    OPERATOR_INTERVENTION = "operator-intervention"


_SECRET_WORDS = (
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


def _redact(value: object, *, key: str = "") -> object:
    lowered = key.lower()
    if any(word in lowered for word in _SECRET_WORDS):
        return "[redacted]"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for name in sorted(value)[:16]:
            if isinstance(name, str):
                result[name[:64]] = _redact(value[name], key=name)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value[:16]]
    if isinstance(value, str):
        # Do not echo URLs, paths, or arbitrary adapter output into evidence.
        if value.startswith(("http://", "https://", "ssh://", "/")):
            return "[redacted]"
        return value[:256]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return "[redacted]"


def _digest(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a sha256 digest")
    return value.removeprefix("sha256:")


@dataclass(frozen=True)
class PackageFailure(Mapping[str, object]):
    """Canonical failure evidence with no secret-bearing diagnostic fields."""

    reason_code: PackageFailureReason
    disposition: PackageFailureDisposition
    family_id: str
    upstream_version: str
    release_digest: str | None
    component: str | None
    node_id: str
    fence: str
    diagnostic: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.reason_code, PackageFailureReason):
            raise TypeError("package failure reason is invalid")
        if not isinstance(self.disposition, PackageFailureDisposition):
            raise TypeError("package failure disposition is invalid")
        for value, label in (
            (self.family_id, "family ID"),
            (self.upstream_version, "upstream version"),
        ):
            if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
                raise ValueError(f"package failure {label} is invalid")
        if self.component is not None and (
            not isinstance(self.component, str) or _TOKEN.fullmatch(self.component) is None
        ):
            raise ValueError("package failure component is invalid")
        _digest(self.release_digest, "package failure release digest")
        if not isinstance(self.node_id, str) or _NODE.fullmatch(self.node_id) is None:
            raise ValueError("package failure node ID is invalid")
        if not isinstance(self.fence, str) or _FENCE.fullmatch(self.fence) is None:
            raise ValueError("package failure fence is invalid")
        if not isinstance(self.diagnostic, Mapping):
            raise TypeError("package failure diagnostic is invalid")
        object.__setattr__(self, "release_digest", _digest(self.release_digest, "release digest"))
        object.__setattr__(self, "diagnostic", MappingProxyType(_redact(self.diagnostic)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "family_id": self.family_id,
            "upstream_version": self.upstream_version,
            "release_digest": self.release_digest,
            "component": self.component,
            "node_id": self.node_id,
            "fence": self.fence,
            "reason_code": self.reason_code.value,
            "disposition": self.disposition.value,
            "diagnostic": dict(self.diagnostic),
        }

    def __getitem__(self, key: str) -> object:
        return self.to_mapping()[key]

    def __iter__(self):
        return iter(self.to_mapping())

    def __len__(self) -> int:
        return len(self.to_mapping())


def failure(
    reason_code: PackageFailureReason,
    *,
    disposition: PackageFailureDisposition,
    family_id: str,
    upstream_version: str,
    release_digest: str | None,
    component: str | None,
    node_id: str,
    fence: str,
    diagnostic: Mapping[str, object] | None = None,
) -> PackageFailure:
    """Construct a validated failure record for callers and acceptance gates."""

    return PackageFailure(
        reason_code,
        disposition,
        family_id,
        upstream_version,
        release_digest,
        component,
        node_id,
        fence,
        diagnostic or {},
    )


__all__ = [
    "PackageFailure",
    "PackageFailureDisposition",
    "PackageFailureReason",
    "failure",
]
