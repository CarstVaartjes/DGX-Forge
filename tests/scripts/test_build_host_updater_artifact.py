from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-host-updater-artifact"


def test_builder_packages_exact_wheel_closure_deterministically(tmp_path: Path) -> None:
    wheels = {
        "control": tmp_path / "dgx_control-0.1.0-py3-none-any.whl",
        "platform": tmp_path / "spark_profiles-0.1.0-py3-none-any.whl",
        "protocol": tmp_path / "dgx_agent_protocol-2.0.0-py3-none-any.whl",
    }
    for name, path in wheels.items():
        path.write_bytes(f"{name}-wheel".encode())
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"

    def build(output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                SCRIPT,
                "--control-wheel",
                wheels["control"],
                "--platform-wheel",
                wheels["platform"],
                "--protocol-wheel",
                wheels["protocol"],
                "--output",
                output,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    first_result = build(first)
    second_result = build(second)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(first.read_bytes()), mode="r:") as archive:
        names = archive.getnames()
        manifest = json.loads(archive.extractfile("host-updater.json").read())
    assert names == sorted(["host-updater.json", *(path.name for path in wheels.values())])
    assert manifest["entry_point"] == "dgx-control-offline"
    assert manifest["wheels"] == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in wheels.values()
    }


def test_control_wheel_declares_installed_offline_entry_point() -> None:
    text = (ROOT / "control/pyproject.toml").read_text()
    assert '[project.scripts]' in text
    assert 'dgx-control-offline = "dgx_control.offline:main"' in text
