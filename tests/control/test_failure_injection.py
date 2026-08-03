import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control/src"))
from dgx_control.acceptance import simulate  # noqa: E402


@pytest.mark.parametrize("fault", ["postgres", "worker", "caddy", "litellm", "git", "ssh", "host"])
def test_fault_never_publishes_unhealthy_route(fault: str) -> None:
    result = simulate(2, fault)
    assert result.terminal_job_state == "failed"
    assert result.route_state == "maintenance"
