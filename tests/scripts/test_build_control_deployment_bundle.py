from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-control-deployment-bundle"


def _run(output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source-root",
            str(ROOT / "deploy/compose"),
            "--output",
            str(output),
            *extra,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_builder_cli_writes_one_deterministic_new_bundle(tmp_path: Path) -> None:
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"

    first_result = _run(first)
    second_result = _run(second)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert first.read_bytes() == second.read_bytes()
    assert first_result.stdout == (
        "sha256:" + hashlib.sha256(first.read_bytes()).hexdigest() + "\n"
    )


def test_builder_cli_never_overwrites_output_or_accepts_arbitrary_assets(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle.tar"
    output.write_bytes(b"operator-owned")

    existing = _run(output)
    arbitrary = _run(tmp_path / "other.tar", "--asset", "/etc/passwd")

    assert existing.returncode == 2
    assert output.read_bytes() == b"operator-owned"
    assert arbitrary.returncode == 2
