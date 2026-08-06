from __future__ import annotations

from pathlib import Path

from dgx_control.db import build_engine, session_factory
from dgx_control.models import Base
from dgx_control.package_services import ProductionPackageProjectionService
from dgx_control.repository import RepositoryService


def test_production_package_projection_reads_git_authority_and_sql_state(tmp_path) -> None:
    root = Path(__file__).resolve().parents[2]
    engine = build_engine(f"sqlite:///{tmp_path / 'package-services.sqlite'}")
    sessions = session_factory(engine)
    # This test uses a private in-process SQLite database and never mutates the
    # repository.  The production adapter must read the same pinned tree as
    # reconciliation, not a mutable checkout path.
    Base.metadata.create_all(engine)
    service = ProductionPackageProjectionService(RepositoryService(root), sessions)

    families = service.families(None, 100)
    deployments = service.deployments(None, 100)

    assert families["total"] >= 2
    assert any(item["id"] == "mia-deepseek" for item in families["families"])
    assert deployments["total"] >= 2
    assert all(str(item["release_digest"]).startswith("sha256:") for item in deployments["deployments"])
    assert service.inventory(None, None, None, 100)["nodes"] == []
