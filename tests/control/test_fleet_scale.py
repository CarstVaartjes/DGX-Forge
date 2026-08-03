import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control/src"))
from dgx_control.acceptance import simulate  # noqa: E402


@pytest.mark.parametrize("nodes", [1, 2, 16, 64])
def test_fleet_operations_have_no_fixed_node_limit(nodes: int) -> None:
    result = simulate(nodes)
    assert result.planned_nodes == nodes
    assert result.duplicate_mutations == 0
    assert result.terminal_job_state == "succeeded"
    assert result.route_state == "published"
