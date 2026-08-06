import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("fault", ["postgres", "worker", "caddy", "litellm", "git", "ssh", "host"])
def test_fault_never_publishes_unhealthy_route(fault: str) -> None:
    completed = subprocess.run([ROOT / "scripts/accept-fleet-scale", "--json"], capture_output=True, text=True, check=True)
    result = next(item for item in json.loads(completed.stdout)["failure_injection"] if item["fault"] == fault)
    assert result["terminal_job_state"] == "failed"
    assert result["route_state"] == "maintenance"


def test_workload_failure_matrix_report_is_structured_and_secret_free() -> None:
    completed = subprocess.run(
        [ROOT / "scripts/accept-workload-package-failures", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(completed.stdout)
    assert report["report_type"] == "dgx-forge-workload-package-failure-matrix"
    assert report["failure_matrix"] is True
    assert report["status"] == "passed"
    assert report["physical_sparks_exercised"] is False
    assert report["ssh_calls"] == report["agent_update_calls"] == 0
    assert len(report["cases"]) >= 15
    assert all(
        {"family_id", "release_digest", "node_id", "fence", "reason_code", "disposition"}
        <= case.keys()
        for case in report["cases"]
    )
    assert "secret" not in completed.stdout.lower()
    assert "https://" not in completed.stdout
