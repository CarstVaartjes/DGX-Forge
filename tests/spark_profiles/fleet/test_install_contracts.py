from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spark_profiles.fleet import ManagementEndpoint, NodeId
from spark_profiles.fleet.install_contracts import (
    InstallationJournal,
    InstallationRequest,
    InvalidInstallationTransition,
)

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


@pytest.fixture
def install_request() -> InstallationRequest:
    return InstallationRequest(
        node_id=NodeId.parse("spk_00000000000000000000000000000001"),
        display_name="alpha",
        endpoint=ManagementEndpoint(
            host="alpha.local",
            user="operator",
            credential_ref="secret://ssh/admin",
        ),
        labels={"rack": "lab"},
    )


def test_installation_cannot_skip_identity_gate(
    install_request: InstallationRequest,
) -> None:
    journal = InstallationJournal.start(install_request, at=NOW)

    with pytest.raises(InvalidInstallationTransition, match="discovered.*inventoried"):
        journal.advance(
            "inventoried",
            evidence_digest="a" * 64,
            at=NOW + timedelta(seconds=1),
        )


def test_installation_follows_explicit_gated_sequence(
    install_request: InstallationRequest,
) -> None:
    journal = InstallationJournal.start(install_request, at=NOW)
    states = [
        "identity-gated",
        "inventoried",
        "key-installed",
        "hardened",
        "policy-applied",
        "accepted",
    ]

    for offset, state in enumerate(states, start=1):
        journal = journal.advance(
            state,
            evidence_digest=f"{offset:x}" * 64,
            at=NOW + timedelta(seconds=offset),
        )

    assert journal.state == "accepted"
    assert tuple(step.state for step in journal.steps) == tuple(states)
    assert journal.steps[-1].evidence_digest == "6" * 64


def test_installation_rejects_invalid_or_missing_evidence_digest(
    install_request: InstallationRequest,
) -> None:
    journal = InstallationJournal.start(install_request, at=NOW)

    for digest in ("", "A" * 64, "a" * 63, "secret"):
        with pytest.raises(ValueError, match="SHA-256"):
            journal.advance("identity-gated", evidence_digest=digest, at=NOW)


def test_failed_installation_is_terminal_and_redacts_reason(
    install_request: InstallationRequest,
) -> None:
    journal = InstallationJournal.start(install_request, at=NOW).fail(
        reason="Authorization: Bearer sensitive-token",
        at=NOW + timedelta(seconds=1),
    )

    assert journal.state == "failed"
    assert "sensitive-token" not in journal.failure_reason
    assert "[REDACTED]" in journal.failure_reason
    with pytest.raises(InvalidInstallationTransition):
        journal.advance("identity-gated", evidence_digest="a" * 64, at=NOW)


def test_serialized_request_uses_credential_reference_only(
    install_request: InstallationRequest,
) -> None:
    payload = install_request.as_public_dict()

    assert payload["credential_ref"] == "secret://ssh/admin"
    assert payload["host"] == "alpha.local"
    assert "private" not in repr(payload).lower()
    assert "password" not in repr(payload).lower()


def test_request_copies_labels_and_rejects_raw_credential_fields() -> None:
    labels = {"rack": "lab"}
    request = InstallationRequest(
        node_id=NodeId.parse("spk_00000000000000000000000000000001"),
        display_name="alpha",
        endpoint=ManagementEndpoint(
            host="alpha.local",
            user="operator",
            credential_ref="secret://ssh/admin",
        ),
        labels=labels,
    )
    labels["rack"] = "changed"

    assert request.labels == {"rack": "lab"}
    with pytest.raises(TypeError):
        request.labels["rack"] = "changed"
    with pytest.raises(ValueError, match="credential reference"):
        InstallationRequest(
            node_id=request.node_id,
            display_name="alpha",
            endpoint=ManagementEndpoint(
                host="alpha.local",
                user="operator",
                credential_ref="-----BEGIN OPENSSH PRIVATE KEY-----",
            ),
            labels={},
        )
