from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "scripts/build-agent-deb"
VERIFY = ROOT / "scripts/verify-agent-deb"


def _aarch64_fixture(path: Path, marker: bytes) -> None:
    raw = bytearray(256)
    raw[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<H", raw, 16, 2)
    struct.pack_into("<H", raw, 18, 183)
    raw[64 : 64 + len(marker)] = marker
    path.write_bytes(raw)
    path.chmod(0o555)


def _release_key(path: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", path],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    path.chmod(0o600)


def _build(output: Path, binaries: Path, key: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            BUILD,
            "--version",
            "0.1.0",
            "--release-private-key",
            key,
            "--binaries-dir",
            binaries,
            "--source-date-epoch",
            "1786060800",
            "--output-dir",
            output,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_builder_produces_reproducible_verified_arm64_deb(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_result = _build(first, binaries, key)
    second_result = _build(second, binaries, key)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    package_name = "vonk-forge-agent_0.1.0_arm64.deb"
    first_deb = first / package_name
    second_deb = second / package_name
    assert first_deb.read_bytes() == second_deb.read_bytes()
    assert stat.S_IMODE(first_deb.stat().st_mode) == 0o644
    sidecar = (first / f"{package_name}.sha256").read_text().strip()
    assert (
        sidecar
        == f"{hashlib.sha256(first_deb.read_bytes()).hexdigest()}  {package_name}"
    )

    verified = subprocess.run(
        [VERIFY, "--json", first_deb],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr or verified.stdout
    assert '"ok": true' in verified.stdout

    control = tmp_path / "control"
    extracted = subprocess.run(
        ["/usr/bin/dpkg-deb", "--control", first_deb, control],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extracted.returncode == 0, extracted.stderr
    postinst = (control / "postinst").read_text()
    preinst = (control / "preinst").read_text()
    assert "python" not in postinst
    assert "curl" not in postinst
    assert "wget" not in postinst
    assert "new_version=0.1.0" in preinst
    assert os.access(control / "preinst", os.X_OK)
    fields = subprocess.run(
        ["/usr/bin/dpkg-deb", "--field", first_deb, "Depends"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "podman" in fields
    assert "uidmap" not in fields
    payload = tmp_path / "payload"
    subprocess.run(
        ["/usr/bin/dpkg-deb", "--extract", first_deb, payload], check=True
    )
    assert (payload / "etc/vonk-forge-agent/containers-storage.conf").is_file()
    unit = (payload / "lib/systemd/system/vonk-forge-agent.service").read_text()
    assert "Environment=HOME=/var/lib/vonk-forge-agent" in unit
    assert "Environment=XDG_RUNTIME_DIR=/run/vonk-forge-agent" in unit
    assert "RestrictNamespaces=user mnt pid ipc uts cgroup net" in unit
    assert "DeviceAllow=/dev/fuse rw" in unit
    assert "DeviceAllow=char-231:* rw" in unit
    assert "BindPaths=-/dev/fuse" in unit
    assert "Delegate=yes" in unit
    helper_socket = (
        payload / "lib/systemd/system/vonk-forge-package-helper.socket"
    ).read_text()
    assert (
        "ListenStream=/run/vonk-forge-package-helper/package-helper.sock"
        in helper_socket.splitlines()
    )
    assert "DirectoryMode=0711" in helper_socket.splitlines()
    assert "usermod --add-subuids" not in postinst
    assert "usermod --add-subgids" not in postinst
    assert "sed -i '/^vonk-agent:/d' /etc/subuid" in postinst
    assert "sed -i '/^vonk-agent:/d' /etc/subgid" in postinst
    assert 'ignore_chown_errors = "true"' in (
        payload / "etc/vonk-forge-agent/containers-storage.conf"
    ).read_text()


def test_verifier_rejects_tampered_release_sidecar(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    output = tmp_path / "dist"
    result = _build(output, binaries, key)
    assert result.returncode == 0, result.stderr
    deb = output / "vonk-forge-agent_0.1.0_arm64.deb"
    (output / f"{deb.name}.sha256").write_text(f"{'0' * 64}  {deb.name}\n")

    verified = subprocess.run(
        [VERIFY, "--json", deb],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert verified.returncode == 1
    assert "sidecar is invalid" in verified.stdout


def test_builder_rejects_symlinked_release_key(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    linked_key = tmp_path / "linked-release.pem"
    linked_key.symlink_to(key)

    result = _build(tmp_path / "dist", binaries, linked_key)

    assert result.returncode == 2
    assert "private key permissions are unsafe" in result.stderr
