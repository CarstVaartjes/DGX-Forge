from __future__ import annotations

import json
from pathlib import Path

import pytest
from dgx_control.upgrade import (
    AmbiguousMigrationError,
    ControlUpgrade,
    UpgradeConflict,
    UpgradeReadinessError,
    UpgradeRecoveryRequired,
)

from spark_profiles.platform_release import PlatformRelease

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _artifact(name: str, digest: str) -> dict[str, object]:
    return {
        "name": name,
        "reference": f"ghcr.io/example/dgx-forge/{name}@sha256:{digest}",
        "sha256": digest,
        "size": 1024,
        "sbom_sha256": SHA_D,
        "provenance_sha256": SHA_E,
    }


def _release(tmp_path: Path) -> PlatformRelease:
    document = {
        "schema_version": 1,
        "platform_version": "1.2.0",
        "build_digest": f"sha256:{SHA_A}",
        "control": {
            "config_version": 3,
            "protocol": {"minimum": 1, "maximum": 2},
            "images": {
                "api": _artifact("api", SHA_A),
                "worker": _artifact("worker", SHA_B),
            },
            "assets": [_artifact("web", SHA_C)],
        },
        "database": {
            "expand_revision": "0010_update_rollouts",
            "contract_revision": None,
            "predecessor_compatible": True,
        },
        "agents": [
            {
                "architecture": "linux-arm64",
                "protocol": {"minimum": 1, "maximum": 2},
                "artifact": _artifact("agent-linux-arm64", SHA_A),
            }
        ],
        "supervisors": [
            {
                "architecture": "linux-arm64",
                "artifact": _artifact("supervisor-linux-arm64", SHA_B),
            }
        ],
        "tooling": [
            {
                "architecture": "linux-arm64",
                "artifact": _artifact("tooling-linux-arm64", SHA_C),
            }
        ],
        "rollback": {"compatible_predecessor_builds": [f"sha256:{SHA_B}"]},
    }
    path = tmp_path / "platform-release.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return PlatformRelease.load(path)


class FakeUpgradeBoundary:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.online = False
        self.free_bytes = 10 * 1024 * 1024
        self.failure: str | None = None

    def control_is_running(self) -> bool:
        self.events.append("check-online")
        return self.online

    def available_bytes(self) -> int:
        self.events.append("check-disk")
        return self.free_bytes

    def pull(self, references: tuple[str, ...]) -> None:
        self.events.append("pull:" + ",".join(references))

    def render_compose(self, environment: dict[str, str]) -> bytes:
        self.events.append("render")
        return (json.dumps(environment, sort_keys=True) + "\n").encode()

    def backup(self, generation_id: str) -> dict[str, object]:
        self.events.append("backup")
        return {"id": f"backup-{generation_id}", "sha256": SHA_C}

    def stop_worker(self) -> None:
        self.events.append("stop-worker")

    def migrate(self, revision: str) -> None:
        self.events.append(f"migrate:{revision}")
        if self.failure == "migration-ambiguous":
            raise AmbiguousMigrationError("database result is unknown")

    def start_api(self, generation_path: Path) -> None:
        self.events.append(f"start-api:{generation_path.name}")

    def readiness(self) -> dict[str, object]:
        self.events.append("readiness")
        if self.failure == "readiness":
            raise UpgradeReadinessError("candidate did not become ready")
        return {"status": "ready", "probe": "caddy"}

    def start_worker(self) -> None:
        self.events.append("start-worker")

    def stop_api(self) -> None:
        self.events.append("stop-api")

    def restore_generation(self, generation_path: Path) -> None:
        self.events.append(f"restore:{generation_path.name}")


def _seed_previous(state_root: Path) -> str:
    generation_id = "previous-generation"
    generation = state_root / "generations" / generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation_id": generation_id,
                "release_digest": f"sha256:{SHA_B}",
                "build_digest": f"sha256:{SHA_B}",
                "status": "active",
            }
        ),
        encoding="utf-8",
    )
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "active-generation").write_text(generation_id + "\n", encoding="utf-8")
    return generation_id


def test_upgrade_plan_is_deterministic_and_dry_run_mutates_nothing(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend)

    first = upgrade.plan(_release(tmp_path))
    second = upgrade.plan(_release(tmp_path))

    assert first == second
    assert first.plan_digest.startswith("sha256:")
    assert first.api_image.endswith(f"@sha256:{SHA_A}")
    assert first.worker_image.endswith(f"@sha256:{SHA_B}")
    assert backend.events == []
    assert not state_root.exists()


def test_upgrade_rejects_running_control_plane_before_mutation(tmp_path: Path) -> None:
    backend = FakeUpgradeBoundary()
    backend.online = True
    upgrade = ControlUpgrade(tmp_path / "state", backend)
    release = _release(tmp_path)

    with pytest.raises(UpgradeConflict, match="running"):
        upgrade.apply(upgrade.plan(release), release)

    assert backend.events == ["check-online"]
    assert not (tmp_path / "state").exists()


def test_upgrade_applies_backup_migration_readiness_and_commit_in_order(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend)
    release = _release(tmp_path)
    plan = upgrade.plan(release)

    result = upgrade.apply(plan, release)

    assert result.status == "active"
    assert result.previous_generation == previous
    assert backend.events[:5] == [
        "check-online",
        "check-disk",
        (
            "pull:ghcr.io/example/dgx-forge/api@sha256:"
            f"{SHA_A},ghcr.io/example/dgx-forge/worker@sha256:{SHA_B}"
        ),
        "render",
        "backup",
    ]
    assert backend.events.index("backup") < backend.events.index(
        "migrate:0010_update_rollouts"
    )
    assert backend.events.index("stop-worker") < backend.events.index(
        "migrate:0010_update_rollouts"
    )
    assert backend.events.index("readiness") < backend.events.index("start-worker")
    assert (state_root / "active-generation").read_text().strip() == result.generation_id
    receipt = json.loads(
        (state_root / "generations" / result.generation_id / "generation.json").read_text()
    )
    assert receipt["release_digest"] == release.digest
    assert receipt["backup"]["sha256"] == SHA_C
    assert receipt["readiness"]["probe"] == "caddy"


def test_failed_readiness_restores_previous_generation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    backend.failure = "readiness"
    upgrade = ControlUpgrade(state_root, backend)
    release = _release(tmp_path)

    with pytest.raises(UpgradeReadinessError):
        upgrade.apply(upgrade.plan(release), release)

    assert backend.events[-2:] == ["stop-api", f"restore:{previous}"]
    assert (state_root / "active-generation").read_text().strip() == previous


def test_ambiguous_database_failure_enters_operator_recovery(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    backend.failure = "migration-ambiguous"
    upgrade = ControlUpgrade(state_root, backend)
    release = _release(tmp_path)
    plan = upgrade.plan(release)

    with pytest.raises(UpgradeRecoveryRequired):
        upgrade.apply(plan, release)

    assert not any(event.startswith("restore:") for event in backend.events)
    recovery = json.loads((state_root / "recovery-required.json").read_text())
    assert recovery["generation_id"] == plan.generation_id
    assert recovery["previous_generation"] == previous
    assert recovery["phase"] == "migration-ambiguous"


def test_upgrade_rejects_insufficient_space_before_pull_or_backup(tmp_path: Path) -> None:
    backend = FakeUpgradeBoundary()
    backend.free_bytes = 1
    upgrade = ControlUpgrade(tmp_path / "state", backend)
    release = _release(tmp_path)

    with pytest.raises(UpgradeConflict, match="disk space"):
        upgrade.apply(upgrade.plan(release), release)

    assert backend.events == ["check-online", "check-disk"]


def test_explicit_rollback_selects_only_recorded_previous_generation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    previous = _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend)
    release = _release(tmp_path)
    active = upgrade.apply(upgrade.plan(release), release)
    backend.events.clear()
    backend.online = False

    result = upgrade.rollback(previous)

    assert result.status == "rolled-back"
    assert result.generation_id == previous
    assert result.previous_generation == active.generation_id
    assert backend.events == ["check-online", f"restore:{previous}"]
    assert (state_root / "active-generation").read_text().strip() == previous


def test_rollback_rejects_unrecorded_or_running_target(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _seed_previous(state_root)
    backend = FakeUpgradeBoundary()
    upgrade = ControlUpgrade(state_root, backend)

    with pytest.raises(UpgradeConflict, match="not the recorded predecessor"):
        upgrade.rollback("unrelated-generation")

    backend.online = True
    with pytest.raises(UpgradeConflict, match="running"):
        upgrade.rollback("previous-generation")
