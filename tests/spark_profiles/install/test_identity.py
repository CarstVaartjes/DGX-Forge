from __future__ import annotations

import pytest

from spark_profiles.install.identity import (
    IdentityObservation,
    TrustedIdentityAssertion,
    evaluate_identity,
)

SERIAL = "a" * 64
MACHINE = "b" * 64
HOST_KEYS = ("SHA256:ed25519", "SHA256:ecdsa")


@pytest.fixture
def observation() -> IdentityObservation:
    return IdentityObservation(
        product_serial_sha256=SERIAL,
        machine_id_sha256=MACHINE,
        host_key_fingerprints=HOST_KEYS,
        requires_console_repair=False,
    )


def test_unanchored_first_contact_requires_console_verification(
    observation: IdentityObservation,
) -> None:
    decision = evaluate_identity(observation, trusted_assertion=None)

    assert decision.action == "wait-for-console"
    assert "trusted assertion" in decision.reason


def test_matching_trusted_identity_is_accepted(
    observation: IdentityObservation,
) -> None:
    assertion = TrustedIdentityAssertion(
        product_serial_sha256=SERIAL,
        host_key_fingerprints=HOST_KEYS,
    )

    decision = evaluate_identity(observation, assertion)

    assert decision.action == "accept"


def test_mismatched_physical_assertion_is_quarantined(
    observation: IdentityObservation,
) -> None:
    assertion = TrustedIdentityAssertion(
        product_serial_sha256="c" * 64,
        host_key_fingerprints=HOST_KEYS,
    )

    decision = evaluate_identity(observation, assertion)

    assert decision.action == "quarantine"
    assert "serial" in decision.reason


def test_duplicate_machine_or_host_key_identity_is_quarantined(
    observation: IdentityObservation,
) -> None:
    assertion = TrustedIdentityAssertion(
        product_serial_sha256=SERIAL,
        host_key_fingerprints=HOST_KEYS,
    )

    duplicate_machine = evaluate_identity(
        observation,
        assertion,
        known_machine_id_digests={MACHINE},
    )
    duplicate_key = evaluate_identity(
        observation,
        assertion,
        known_host_key_fingerprints={"SHA256:ecdsa"},
    )

    assert duplicate_machine.action == "quarantine"
    assert duplicate_key.action == "quarantine"


def test_probe_repair_flag_always_pauses_for_console(
    observation: IdentityObservation,
) -> None:
    unsafe = IdentityObservation(
        product_serial_sha256=observation.product_serial_sha256,
        machine_id_sha256=observation.machine_id_sha256,
        host_key_fingerprints=observation.host_key_fingerprints,
        requires_console_repair=True,
    )
    assertion = TrustedIdentityAssertion(
        product_serial_sha256=SERIAL,
        host_key_fingerprints=HOST_KEYS,
    )

    decision = evaluate_identity(unsafe, assertion)

    assert decision.action == "wait-for-console"
    assert "repair" in decision.reason


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "not-a-digest"])
def test_identity_contract_rejects_malformed_digests(digest: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        IdentityObservation(
            product_serial_sha256=digest,
            machine_id_sha256=MACHINE,
            host_key_fingerprints=HOST_KEYS,
            requires_console_repair=False,
        )
