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
