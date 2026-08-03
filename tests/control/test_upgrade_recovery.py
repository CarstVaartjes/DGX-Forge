import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/accept-control-recovery"


def test_disposable_recovery_is_integral_and_fail_closed(tmp_path: Path) -> None:
    output = tmp_path / "recovery.json"
    result = subprocess.run(
        [SCRIPT, "--output", output, "--source-date-epoch", "1785715200", "--json"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["restore_integrity"] == "passed"
    assert report["route_before_health"] == "maintenance"
    assert report["route_after_health"] == "published"
    assert report["physical_replacement_host_exercised"] is False
    assert "physical-replacement-host-drill" in report["remaining_release_gates"]


def test_recovery_evidence_is_reproducible(tmp_path: Path) -> None:
    outputs = [tmp_path / "one.json", tmp_path / "two.json"]
    for output in outputs:
        subprocess.run([SCRIPT, "--output", output, "--source-date-epoch", "1785715200"], check=True)
    # Backup bytes are canonical, so evidence remains content-addressable.
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
