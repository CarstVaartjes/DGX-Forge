import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_repository_to_running_profile_and_safe_withdrawal(tmp_path: Path) -> None:
    output = tmp_path / "lifecycle.json"
    result = subprocess.run(
        [
            ROOT / "scripts/accept-platform-lifecycle",
            "--host",
            "dynamic.example",
            "--display-name",
            "dynamic-spark",
            "--output",
            output,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["dynamic_input"] == {"host": "dynamic.example", "display_name": "dynamic-spark"}
    assert report["installation_gate_count"] == 7
    assert report["profile_source"] == "model-repository/revision"
    assert report["planner"] == "DesiredStateResolver"
    assert report["persisted_operation_count"] == 11
    assert report["claimed_operation_count"] == 12
    assert report["release_transition"] == {
        "from": "a" * 64,
        "to": "7" * 64,
    }
    assert report["durable_observation"] == {
        "current_release": "7" * 64,
        "current_workload": "test-model",
        "occupied": True,
    }
    assert report["served_status"] == 200
    assert report["withdrawn_status"] == 503
    assert report["code_host_pr_merge_exercised"] is False
