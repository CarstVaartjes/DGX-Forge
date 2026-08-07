from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cluster_profiles.fleet import ManagementEndpoint, NodeId
from cluster_profiles.fleet.install_contracts import (
    InstallationJournal,
    InstallationRequest,
)
from cluster_profiles.install.proposal import (
    ProposalError,
    build_node_proposal,
    emit_node_record,
)


def _journal(*, accepted: bool) -> InstallationJournal:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    journal = InstallationJournal.start(
        InstallationRequest(
            node_id=NodeId.parse("spk_0123456789abcdef0123456789abcdef"),
            display_name='lab "alpha"',
            endpoint=ManagementEndpoint("node.local", "admin", 2222, "secret://ssh/admin"),
            labels={"zone": "west", "purpose": "inference"},
        ),
        at=now,
    )
    if not accepted:
        return journal
    states = (
        "identity-gated", "inventoried", "key-installed", "hardened",
        "policy-applied", "post-inventoried", "accepted",
    )
    for index, state in enumerate(states, 1):
        journal = journal.advance(state, evidence_digest=f"{index:x}" * 64, at=now + timedelta(seconds=index))
    return journal


def test_proposal_is_deterministic_and_sanitized() -> None:
    journal = _journal(accepted=True)
    observations = {"hostname": "runtime-name", "serial": "PRIVATE KEY"}

    first = build_node_proposal("abc123", journal, observations)
    second = build_node_proposal("abc123", journal, observations)

    assert first == second
    assert first.base_commit == "abc123"
    assert first.target_path == "inventory/fleet.toml"
    assert first.sha256 == __import__("hashlib").sha256(first.content).hexdigest()
    assert b"PRIVATE KEY" not in first.content
    assert b"credential_ref" not in first.content
    assert b'schema_version = 2' in first.content
    assert b'purpose = "inference"' in first.content
    assert b'zone = "west"' in first.content
    assert emit_node_record(journal, hostname="runtime-name") in first.content


def test_unaccepted_install_cannot_emit_proposal() -> None:
    with pytest.raises(ProposalError, match="accepted"):
        build_node_proposal("abc123", _journal(accepted=False), {})


def test_observed_hostname_is_required_and_validated() -> None:
    journal = _journal(accepted=True)
    with pytest.raises(ProposalError, match="hostname"):
        build_node_proposal("abc123", journal, {})
    with pytest.raises(ProposalError, match="hostname"):
        build_node_proposal("abc123", journal, {"hostname": "bad\nname"})
