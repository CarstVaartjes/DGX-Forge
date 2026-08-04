from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from dgx_control.models import AgentNode, Base, Observation
from dgx_control.presence import (
    AgentPresenceService,
    ManagementAddressPolicy,
    PresenceError,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


NODE_ID = "spk_" + "a" * 32
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def test_management_address_policy_accepts_only_bounded_canonical_addresses() -> None:
    policy = ManagementAddressPolicy.parse(
        "10.0.0.0/24,10.1.0.0/16",
        forbidden_cidrs="10.0.0.240/28",
    )

    assert policy.validate("10.0.0.42") == "10.0.0.42"

    for address in (
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "10.0.0.241",
        "10.0.1.1",
    ):
        with pytest.raises(PresenceError):
            policy.validate(address)


def test_management_address_policy_rejects_noncanonical_and_ambiguous_networks() -> None:
    with pytest.raises(PresenceError, match="canonical"):
        ManagementAddressPolicy.parse("10.0.0.1/24")
    with pytest.raises(PresenceError, match="duplicate"):
        ManagementAddressPolicy.parse("10.0.0.0/24,10.0.0.0/24")
    with pytest.raises(PresenceError, match="empty"):
        ManagementAddressPolicy.parse("")
    with pytest.raises(PresenceError, match="fully forbidden"):
        ManagementAddressPolicy.parse(
            "10.0.0.0/24",
            forbidden_cidrs="10.0.0.0/24",
        )


def test_observe_updates_node_and_persists_latest_management_address(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'presence.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state="active", capabilities=[]))

    service = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse("10.0.0.0/24"),
    )
    observed = service.observe(NODE_ID, "10.0.0.42", NOW)

    assert observed.node_id == NODE_ID
    assert observed.address == "10.0.0.42"
    assert observed.observed_at == NOW
    with sessions() as session:
        node = session.get(AgentNode, NODE_ID)
        rows = session.scalars(select(Observation)).all()
    assert node is not None
    assert node.last_seen_at is not None
    assert node.last_seen_at.replace(tzinfo=UTC) == NOW
    assert len(rows) == 1
    assert rows[0].kind == "management-address"
    assert rows[0].payload == {"address": "10.0.0.42"}
    assert service.latest(NODE_ID, maximum_age_seconds=150, now=NOW) == observed

    with pytest.raises(PresenceError, match="stale"):
        service.latest(
            NODE_ID,
            maximum_age_seconds=150,
            now=NOW + timedelta(seconds=151),
        )


def test_observe_rejects_unknown_or_revoked_nodes(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'presence.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse("10.0.0.0/24"),
    )

    with pytest.raises(PresenceError, match="active"):
        service.observe(NODE_ID, "10.0.0.42", NOW)

    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_ID,
                state="revoked",
                capabilities=[],
                revoked_at=NOW,
            )
        )
    with pytest.raises(PresenceError, match="active"):
        service.observe(NODE_ID, "10.0.0.42", NOW)
