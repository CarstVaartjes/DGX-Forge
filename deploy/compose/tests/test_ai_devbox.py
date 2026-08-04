from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
DEVBOX = ROOT / "deploy/compose/ai-devbox"


def test_image_contract_is_hardened_and_home_independent() -> None:
    dockerfile = (DEVBOX / "Dockerfile").read_text()
    lowered = dockerfile.lower()

    assert "FROM ubuntu:24.04" in dockerfile
    assert "ARG USERNAME=ai-dev" in dockerfile
    assert "ARG USER_UID=1100" in dockerfile
    assert "ARG USER_GID=1100" in dockerfile
    for package in (
        "openssh-server",
        "git",
        "tmux",
        "ripgrep",
        "build-essential",
        "python3",
        "nodejs",
    ):
        assert package in dockerfile
    assert "setup_22.x" in dockerfile
    assert "/usr/local/bin" in dockerfile and "uv" in lowered
    assert "/opt/rust" in dockerfile
    assert "/etc/ai-devbox/skel" in dockerfile
    assert "useradd" in dockerfile
    assert "--no-log-init" in dockerfile
    for forbidden in ("sudo", "docker.sock", "privileged"):
        assert forbidden not in lowered


def test_sshd_is_key_only_and_disables_forwarding() -> None:
    config = (DEVBOX / "sshd_config").read_text()
    required = {
        "AllowUsers ai-dev",
        "AuthenticationMethods publickey",
        "PubkeyAuthentication yes",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "ChallengeResponseAuthentication no",
        "PermitEmptyPasswords no",
        "PermitRootLogin no",
        "UsePAM no",
        "X11Forwarding no",
        "AllowAgentForwarding no",
        "AllowTcpForwarding no",
        "PermitTunnel no",
        "GatewayPorts no",
        "PermitUserEnvironment no",
        "HostKey /var/lib/ai-devbox/ssh-host-keys/ssh_host_ed25519_key",
        "HostKey /var/lib/ai-devbox/ssh-host-keys/ssh_host_rsa_key",
    }
    assert required <= set(config.splitlines())


def _test_root(tmp_path: Path, authorized_keys: bytes | None) -> Path:
    root = tmp_path / "root"
    (root / "run/config").mkdir(parents=True)
    (root / "etc/ai-devbox/skel").mkdir(parents=True)
    (root / "etc/ai-devbox/skel/.bashrc").write_text("# managed seed\n")
    (root / "etc/ai-devbox/skel/.tmux.conf").write_text("set -g mouse on\n")
    (root / "workspaces").mkdir()
    sentinel = root / "workspaces/sentinel"
    sentinel.write_text("owned by the test\n")
    if authorized_keys is not None:
        (root / "run/config/authorized_keys").write_bytes(authorized_keys)
    return root


def _run_entrypoint(root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "AI_DEVBOX_TEST_ROOT": str(root),
        "AI_DEVBOX_TEST_UID": str(os.getuid()),
        "AI_DEVBOX_TEST_GID": str(os.getgid()),
    }
    return subprocess.run(
        ["bash", str(DEVBOX / "entrypoint.sh")],
        text=True,
        capture_output=True,
        env=env,
    )


@pytest.fixture
def public_key(tmp_path: Path) -> bytes:
    key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    return key.with_suffix(".pub").read_bytes()


@pytest.mark.parametrize("payload", [None, b"", b"not-an-ssh-key\n"])
def test_entrypoint_rejects_missing_empty_or_malformed_keys(
    tmp_path: Path, payload: bytes | None
) -> None:
    result = _run_entrypoint(_test_root(tmp_path, payload))
    assert result.returncode != 0


def test_entrypoint_rejects_oversized_or_non_regular_keys(
    tmp_path: Path, public_key: bytes
) -> None:
    root = _test_root(tmp_path, public_key * 5000)
    result = _run_entrypoint(root)
    assert result.returncode != 0

    source = root / "run/config/authorized_keys"
    source.unlink()
    source.symlink_to(tmp_path / "missing-key")
    result = _run_entrypoint(root)
    assert result.returncode != 0


def test_entrypoint_installs_keys_seeds_once_and_preserves_workspace_ownership(
    tmp_path: Path, public_key: bytes
) -> None:
    root = _test_root(tmp_path, b"# macbook\n\n" + public_key)
    sentinel = root / "workspaces/sentinel"
    original_owner = (sentinel.stat().st_uid, sentinel.stat().st_gid)

    first = _run_entrypoint(root)
    assert first.returncode == 0, first.stderr

    installed = root / "home/ai-dev/.ssh/authorized_keys"
    assert installed.read_bytes() == b"# macbook\n\n" + public_key
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert (root / "home/ai-dev/.bashrc").read_text() == "# managed seed\n"
    assert (root / "home/ai-dev/.tmux.conf").read_text() == "set -g mouse on\n"
    assert (sentinel.stat().st_uid, sentinel.stat().st_gid) == original_owner

    host_key_dir = root / "var/lib/ai-devbox/ssh-host-keys"
    fingerprints = {
        path.name: subprocess.run(
            ["ssh-keygen", "-lf", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for path in host_key_dir.glob("*.pub")
    }
    assert set(fingerprints) == {
        "ssh_host_ed25519_key.pub",
        "ssh_host_rsa_key.pub",
    }

    (root / "home/ai-dev/.bashrc").write_text("# user customized\n")
    second = _run_entrypoint(root)
    assert second.returncode == 0, second.stderr
    assert (root / "home/ai-dev/.bashrc").read_text() == "# user customized\n"
    assert fingerprints == {
        path.name: subprocess.run(
            ["ssh-keygen", "-lf", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for path in host_key_dir.glob("*.pub")
    }
