from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "nodes" / "bin" / "inspect-node-identity"


def _identity_environment(tmp_path: Path) -> tuple[dict[str, str], str, str]:
    serial = "SERIAL-SECRET-123"
    machine_id = "0123456789abcdef0123456789abcdef"
    serial_path = tmp_path / "product_serial"
    machine_path = tmp_path / "machine-id"
    key_root = tmp_path / "ssh_host_ed25519_key"
    serial_path.write_text(serial + "\n")
    machine_path.write_text(machine_id + "\n")
    key_root.write_text("private-material-is-never-read")
    key_root.with_suffix(".pub").write_text("ssh-ed25519 PUBLIC host\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sshd = fake_bin / "sshd"
    sshd.write_text(
        f"#!/usr/bin/env bash\nprintf 'hostkey {key_root}\\n'\n"
    )
    sshd.chmod(0o755)
    ssh_keygen = fake_bin / "ssh-keygen"
    ssh_keygen.write_text(
        "#!/usr/bin/env bash\nprintf '256 SHA256:host-fingerprint fixture (ED25519)\\n'\n"
    )
    ssh_keygen.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DGX_IDENTITY_SERIAL_PATH": str(serial_path),
        "DGX_IDENTITY_MACHINE_ID_PATH": str(machine_path),
    }
    return environment, serial, machine_id


def test_identity_probe_emits_hashes_and_public_fingerprints_not_raw_identity(
    tmp_path: Path,
) -> None:
    environment, serial, machine_id = _identity_environment(tmp_path)

    completed = subprocess.run(
        ["bash", str(PROBE)],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result == {
        "schema_version": 1,
        "product_serial_sha256": hashlib.sha256(serial.encode()).hexdigest(),
        "machine_id_sha256": hashlib.sha256(machine_id.encode()).hexdigest(),
        "host_key_fingerprints": ["SHA256:host-fingerprint"],
        "requires_console_repair": False,
    }
    assert serial not in completed.stdout
    assert machine_id not in completed.stdout
    assert "private-material" not in completed.stdout


def test_identity_probe_marks_invalid_machine_id_for_console_repair(
    tmp_path: Path,
) -> None:
    environment, _, _ = _identity_environment(tmp_path)
    Path(environment["DGX_IDENTITY_MACHINE_ID_PATH"]).write_text("not-a-machine-id\n")

    completed = subprocess.run(
        ["bash", str(PROBE)],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout)["requires_console_repair"] is True


def test_identity_probe_rejects_arguments(tmp_path: Path) -> None:
    environment, _, _ = _identity_environment(tmp_path)

    completed = subprocess.run(
        ["bash", str(PROBE), "unexpected"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""


def test_identity_probe_requires_readable_identity_sources(tmp_path: Path) -> None:
    environment, _, _ = _identity_environment(tmp_path)
    environment["DGX_IDENTITY_SERIAL_PATH"] = str(tmp_path / "missing")

    completed = subprocess.run(
        ["bash", str(PROBE)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "missing" not in completed.stdout


def test_identity_probe_is_valid_bash() -> None:
    completed = subprocess.run(
        ["bash", "-n", str(PROBE)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
