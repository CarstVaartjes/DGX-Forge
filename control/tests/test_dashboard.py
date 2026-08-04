from datetime import UTC, datetime

import pytest
from dgx_control.dashboard import DashboardService
from dgx_control.models import Base, Observation
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Repository:
    def head(self): return "a" * 40
    def read_document(self, commit, path):
        return type("Document", (), {"parsed": {"schema_version": 2, "nodes": {
            "spk_00000000000000000000000000000001": {"display_name": "Alpha", "hostname": "alpha", "lifecycle": "ready", "management": {"host": "alpha.local", "user": "admin", "port": 22}, "labels": {"zone": "lab"}}
        }}})()


def test_dashboard_rejects_nonmapping_fleet_document_as_type_error() -> None:
    class InvalidRepository(Repository):
        def read_document(self, commit, path):
            return type("Document", (), {"parsed": []})()

    with pytest.raises(TypeError, match="node table"):
        DashboardService(InvalidRepository(), None).fleet()


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
