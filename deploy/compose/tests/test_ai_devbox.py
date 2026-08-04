from __future__ import annotations

import os
import json
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
DEVBOX = ROOT / "deploy/compose/ai-devbox"
COMPOSE = ROOT / "deploy/compose"


def _compose_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for line in (COMPOSE / "tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            environment[key] = value
    return environment


def _rendered() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE / "compose.yaml"),
            "-f",
            str(COMPOSE / "compose.step-ca.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_compose_environment(),
    )
    return json.loads(result.stdout)


def _volume_targets(service: dict[str, object]) -> dict[str, dict[str, object]]:
    return {volume["target"]: volume for volume in service.get("volumes", [])}


def test_image_contract_is_hardened_and_home_independent() -> None:
    dockerfile = (DEVBOX / "Dockerfile").read_text()
    lowered = dockerfile.lower()

    assert "ARG UBUNTU_IMAGE=ubuntu:24.04@sha256:" in dockerfile
    assert "FROM ${UBUNTU_IMAGE}" in dockerfile
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
    assert 'passwd -d "${USERNAME}"' in dockerfile
    assert "COPY --chmod" not in dockerfile
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
    entrypoint = (DEVBOX / "entrypoint.sh").read_text()
    assert 'chown root:root "${HOST_KEY_DIR}"' in entrypoint


def test_compose_devbox_is_unpublished_and_strongly_isolated() -> None:
    service = _rendered()["services"]["ai-devbox"]

    assert service["init"] is True
    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert not service.get("ports")
    assert not service.get("devices")
    assert not service.get("privileged")
    assert not service.get("cap_add")
    assert set(service["networks"]) == {"tailnet-ssh-edge", "devbox-egress"}
    assert service["cpus"] == 4.0
    assert int(service["mem_limit"]) == 8 * 1024**3
    assert int(service["mem_reservation"]) == 4 * 1024**3
    assert int(service["shm_size"]) == 2 * 1024**3
    assert service["tmpfs"] == [
        "/run:size=64m,mode=755",
        "/tmp:size=2g,mode=1777",
        "/var/tmp:size=1g,mode=1777",
    ]

    volumes = _volume_targets(service)
    assert volumes["/home/ai-dev"]["source"] == "/srv/dgx-forge/ai-devbox/home"
    assert volumes["/workspaces"]["source"] == "/srv/dgx-forge/ai-devbox/workspaces"
    assert volumes["/cache"]["source"] == "/srv/dgx-forge/ai-devbox/cache"
    assert (
        volumes["/var/lib/ai-devbox/ssh-host-keys"]["source"]
        == "/srv/dgx-forge/ai-devbox/ssh-host-keys"
    )
    authorized = volumes["/run/config/authorized_keys"]
    assert authorized["source"] == "/srv/dgx-forge/secrets/ai-devbox-authorized-keys"
    assert authorized["read_only"] is True
    assert authorized["bind"]["create_host_path"] is False
    assert "docker.sock" not in json.dumps(service)


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


def test_runtime_harness_covers_isolation_and_persistence() -> None:
    harness = (COMPOSE / "tests/ai-devbox-runtime.sh").read_text()

    assert "StrictHostKeyChecking=yes" in harness
    assert 'SSH_CLIENT_BIN="${SSH_CLIENT_BIN:-/usr/bin/ssh}"' in harness
    assert "whoami" in harness
    assert "id -u" in harness and "id -g" in harness
    for tool in ("uv", "node", "python", "rustc"):
        assert f"command -v {tool}" in harness
    for negative in (
        "root@127.0.0.1",
        "PasswordAuthentication=yes",
        "KbdInteractiveAuthentication=yes",
        "AllowTcpForwarding no",
        "SSH_AUTH_SOCK",
        "sudo -n true",
    ):
        assert negative in harness
    assert "Privileged" in harness
    assert "CapAdd" in harness
    assert "Devices" in harness
    assert "docker.sock" in harness
    assert "--force-recreate" in harness
    assert "ssh-keygen -lf" in harness
