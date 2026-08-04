import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dgx_control.dashboard import DashboardService
from dgx_control.models import AgentNode, Base, Observation


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class Repository:
    def head(self): return "a" * 40
    def read_document(self, commit, path):
        return type("Document", (), {"parsed": {"schema_version": 2, "nodes": {
            "spk_00000000000000000000000000000001": {"display_name": "Alpha", "hostname": "alpha", "lifecycle": "ready", "management": {"host": "alpha.local", "user": "admin", "port": 22}, "labels": {"zone": "lab"}}
        }}})()


class PresenceRepository:
    def head(self): return "b" * 40
    def read_document(self, commit, path):
        nodes = {
            "spk_" + character * 32: {
                "display_name": name,
                "hostname": f"{name.lower()}.lan",
                "lifecycle": "ready",
            }
            for character, name in (
                ("a", "Active"),
                ("b", "Stale"),
                ("c", "Revoked"),
                ("d", "Missing"),
            )
        }
        return type("Document", (), {"parsed": {"schema_version": 2, "nodes": nodes}})()


def test_dashboard_joins_repository_fleet_with_latest_observation(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'dashboard.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(Observation(node_id="spk_00000000000000000000000000000001", kind="health", payload={"status": "healthy"}, observed_at=datetime(2026, 8, 3, tzinfo=UTC)))
    result = DashboardService(Repository(), sessions).fleet()
    assert result["commit"] == "a" * 40
    node = result["nodes"][0]
    assert {key: node[key] for key in ("id", "display_name", "hostname", "lifecycle", "healthy", "labels", "profile")} == {
        "id": "spk_00000000000000000000000000000001", "display_name": "Alpha", "hostname": "alpha", "lifecycle": "ready", "healthy": True, "labels": {"zone": "lab"}, "profile": None,
    }
    assert "management" not in result["nodes"][0]


def test_dashboard_projects_agent_availability_without_addresses(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'presence-dashboard.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    active = "spk_" + "a" * 32
    stale = "spk_" + "b" * 32
    revoked = "spk_" + "c" * 32
    with sessions.begin() as session:
        session.add_all(
            (
                AgentNode(
                    node_id=active,
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW - timedelta(seconds=149),
                ),
                AgentNode(
                    node_id=stale,
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW - timedelta(seconds=151),
                ),
                AgentNode(
                    node_id=revoked,
                    state="revoked",
                    capabilities=[],
                    last_seen_at=NOW - timedelta(seconds=1),
                    revoked_at=NOW - timedelta(seconds=1),
                ),
                Observation(
                    node_id=active,
                    kind="management-address",
                    payload={"address": "10.0.0.42"},
                    observed_at=NOW,
                ),
            )
        )

    result = DashboardService(
        PresenceRepository(),
        sessions,
        clock=lambda: NOW,
        agent_online_window_seconds=150,
    ).fleet()

    nodes = {node["display_name"]: node for node in result["nodes"]}
    assert nodes["Active"]["agent_state"] == "active"
    assert nodes["Active"]["agent_online"] is True
    assert nodes["Active"]["agent_last_seen_at"] == (NOW - timedelta(seconds=149)).isoformat()
    assert nodes["Stale"]["agent_state"] == "active"
    assert nodes["Stale"]["agent_online"] is False
    assert nodes["Revoked"]["agent_state"] == "revoked"
    assert nodes["Revoked"]["agent_online"] is False
    assert nodes["Missing"]["agent_state"] == "unregistered"
    assert nodes["Missing"]["agent_last_seen_at"] is None
    assert nodes["Missing"]["agent_online"] is False
    encoded = json.dumps(result, sort_keys=True)
    assert "10.0.0.42" not in encoded
    assert "management-address" not in encoded
