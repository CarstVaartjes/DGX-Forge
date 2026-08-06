from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE = ROOT / "scripts/accept-platform-update"


def test_control_host_uses_only_exact_targets_and_generation_assets() -> None:
    result = subprocess.run(
        [ACCEPTANCE, "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    host = json.loads(result.stdout)["host"]
    assert host["crash_recovered"] is True
    assert host["recovery_state"] == "rolled-back"
    assert host["selected_generation"] == host["old_generation"]
    assert host["mutable_channel_resolved"] is False
    assert host["mutable_target_rejected"] is True
    assert host["revoked_predecessor_rejected"] is True
    assert host["tampered_target_rejected"] is True
    assert host["unsigned_target_rejected"] is True
    assert host["operation_journal_count"] == 3
    assert set(host["generation_asset_ids"]) == {
        host["candidate_generation"],
        host["old_generation"],
    }
    assert all(
        target.startswith("platform/releases/") and target.endswith(".json")
        for target in host["release_source_calls"]
    )
    assert host["newer_target_name"] not in host["release_source_calls"]


def test_control_host_recovers_after_each_apply_phase_effect(tmp_path: Path) -> None:
    phases = (
        "bundle-images-acquired",
        "generation-staged",
        "backup-completed",
        "services-stopped-database-migrated",
        "candidate-ready",
        "generation-committed",
        "generation-selected",
        "services-started",
        "worker-ready",
        "completed",
    )
    program = "\n".join(
        (
            "import json, os, runpy",
            "from pathlib import Path",
            'os.environ["DGX_PLATFORM_UPDATE_LOCKED_ENV"] = "1"',
            f"module = runpy.run_path({str(ACCEPTANCE)!r})",
            f"root = Path({str(tmp_path)!r})",
            f"phases = {phases!r}",
            "reports = [module['_host_generation_scenario'](root / str(index), crash_phase=module['UpgradePhase'](phase)) for index, phase in enumerate(phases)]",
            "print(json.dumps(reports))",
        )
    )
    result = subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--project",
            ROOT / "control",
            "--with-editable",
            ROOT,
            "python",
            "-c",
            program,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    reports = json.loads(result.stdout)
    assert [report["crash_phase"] for report in reports] == list(phases)
    assert all(report["crash_recovered"] for report in reports)
    assert all(
        report["selected_generation"] == report["old_generation"] for report in reports
    )
