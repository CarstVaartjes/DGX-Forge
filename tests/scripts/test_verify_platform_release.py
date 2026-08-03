import json
import shutil
import subprocess
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-platform-release"


def test_release_verifier_lists_external_gates() -> None:
    result = subprocess.run([SCRIPT, "--candidate", "1.0.0", "--json"], capture_output=True, text=True)
    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert report["status"] == "blocked"
    assert "protected-code-host" in report["missing_gates"]
    assert "approved-physical-spark-lifecycle" in report["missing_gates"]
    schema = json.loads((ROOT / "schemas/platform-release-evidence.schema.json").read_text())
    jsonschema.validate(report, schema)


def test_release_verifier_lists_missing_report(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / "inventory/reports").mkdir(parents=True)
    (repository / "scripts").mkdir()
    shutil.copy2(SCRIPT, repository / "scripts/verify-platform-release")
    supply = repository / "scripts/verify-supply-chain"
    supply.write_text("#!/bin/sh\nexit 0\n")
    supply.chmod(0o755)
    result = subprocess.run([repository / "scripts/verify-platform-release", "--root", repository, "--candidate", "1.0.0", "--json"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "control-plane-recovery" in json.loads(result.stdout)["missing_gates"]
