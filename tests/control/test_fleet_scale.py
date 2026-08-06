import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("nodes", [1, 2, 16, 64])
def test_fleet_operations_have_no_fixed_node_limit(nodes: int) -> None:
    completed = subprocess.run([ROOT / "scripts/accept-fleet-scale", "--json"], capture_output=True, text=True, check=True)
    result = next(item for item in json.loads(completed.stdout)["scale"] if item["nodes"] == nodes)
    assert result["planned_nodes"] == nodes
    assert result["duplicate_mutations"] == 0
    assert result["terminal_job_state"] == "succeeded"
    assert result["route_state"] == "published"


def test_workload_failure_matrix_covers_generated_fleet_recovery() -> None:
    completed = subprocess.run(
        [ROOT / "scripts/accept-workload-package-failures", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(completed.stdout)
    assert report["concurrent_identical_downloads"] == "one-fetch-many-consumers"
    assert report["restart_recovery"] == "passed"
    assert report["gc_restart_recovery"] == "passed"
    assert {case["reason_code"] for case in report["cases"]} >= {
        "insufficient-capacity",
        "retryable-transport",
        "activation-failure",
        "rollback-failure",
    }
