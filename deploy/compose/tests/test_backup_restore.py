from pathlib import Path

from spark_profiles.deployment_bundle import REQUIRED_DEPLOYMENT_ASSETS


def test_deployment_bundle_exposes_no_mutable_host_backup_program() -> None:
    root = Path(__file__).resolve().parents[3]
    forbidden = {
        "bin/backup-control-plane",
        "bin/restore-control-plane",
    }

    assert forbidden.isdisjoint(REQUIRED_DEPLOYMENT_ASSETS)
    assert not (root / "deploy/compose/bin/backup-control-plane").exists()
    assert not (root / "deploy/compose/bin/restore-control-plane").exists()
