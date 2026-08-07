"""Trusted-identity decisions for first-contact GPU node onboarding."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _digest(value: str, *, field: str) -> None:
    if _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase hexadecimal SHA-256 digest")


def _fingerprints(values: tuple[str, ...]) -> None:
    if not values or len(values) != len(set(values)):
        raise ValueError("host-key fingerprints must be nonempty and unique")
    if any(not value.startswith("SHA256:") or len(value) <= 8 for value in values):
        raise ValueError("host-key fingerprint must use SHA256 format")


@dataclass(frozen=True)
class IdentityObservation:
    product_serial_sha256: str
    machine_id_sha256: str
    host_key_fingerprints: tuple[str, ...]
    requires_console_repair: bool

    def __post_init__(self) -> None:
        _digest(self.product_serial_sha256, field="product serial")
        _digest(self.machine_id_sha256, field="machine id")
        _fingerprints(self.host_key_fingerprints)
        if not isinstance(self.requires_console_repair, bool):
            raise TypeError("requires_console_repair must be boolean")


@dataclass(frozen=True)
class TrustedIdentityAssertion:
    product_serial_sha256: str
    host_key_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.product_serial_sha256, field="product serial")
        _fingerprints(self.host_key_fingerprints)


@dataclass(frozen=True)
class IdentityDecision:
    action: Literal["accept", "wait-for-console", "quarantine"]
    reason: str


def evaluate_identity(
    observation: IdentityObservation,
    trusted_assertion: TrustedIdentityAssertion | None,
    *,
    known_machine_id_digests: set[str] | frozenset[str] = frozenset(),
    known_host_key_fingerprints: set[str] | frozenset[str] = frozenset(),
) -> IdentityDecision:
    """Require a physical assertion and reject cloned or changed identities."""

    if observation.requires_console_repair:
        return IdentityDecision(
            "wait-for-console",
            "node identity requires console repair before onboarding",
        )
    if trusted_assertion is None:
        return IdentityDecision(
            "wait-for-console",
            "first contact requires a trusted assertion from the physical console",
        )
    if observation.product_serial_sha256 != trusted_assertion.product_serial_sha256:
        return IdentityDecision(
            "quarantine",
            "observed product serial does not match the trusted console assertion",
        )
    if set(observation.host_key_fingerprints) != set(
        trusted_assertion.host_key_fingerprints
    ):
        return IdentityDecision(
            "quarantine",
            "observed host keys do not match the trusted console assertion",
        )
    if observation.machine_id_sha256 in known_machine_id_digests:
        return IdentityDecision(
            "quarantine",
            "observed machine identity duplicates an existing fleet node",
        )
    if set(observation.host_key_fingerprints) & set(known_host_key_fingerprints):
        return IdentityDecision(
            "quarantine",
            "an observed host key duplicates an existing fleet node",
        )
    return IdentityDecision("accept", "trusted node identity is unique")
