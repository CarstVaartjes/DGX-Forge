from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "nodes" / "bin" / "install-ssh-hardening"
DROP_IN = ROOT / "nodes" / "etc" / "ssh" / "sshd_config.d" / "90-dgx-admin.conf"
FINGERPRINT = "SHA256:test-admin-key"


@pytest.fixture
def hardening_host(tmp_path: Path) -> dict[str, object]:
    config_dir = tmp_path / "sshd_config.d"
    admin_home = tmp_path / "admin-home"
    ssh_dir = admin_home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "authorized_keys").write_text("ssh-ed25519 PUBLIC admin\n")
    recovery = tmp_path / "recovery-marker"
    recovery.write_text("recovery-channel-verified\n")
    recovery.chmod(0o600)
    actions = tmp_path / "actions"
    actions.write_text("")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    getent = fake_bin / "getent"
    getent.write_text(
        f"#!/usr/bin/env bash\n[[ \"$1\" == passwd && \"$2\" == operator ]] || exit 2\nprintf 'operator:x:1000:1000::%s:/bin/bash\\n' '{admin_home}'\n"
    )
    ssh_keygen = fake_bin / "ssh-keygen"
    ssh_keygen.write_text(
        f"#!/usr/bin/env bash\nprintf '256 {FINGERPRINT} fixture (ED25519)\\n'\n"
    )
    sshd = fake_bin / "sshd"
    sshd.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  -t) exit 0 ;;
  -T)
    printf '%s\n' \
      'passwordauthentication no' \
      'kbdinteractiveauthentication no' \
      'pubkeyauthentication yes' \
      'permitrootlogin prohibit-password'
    ;;
  *) exit 64 ;;
esac
"""
    )
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  reload) printf 'reload\n' >> "${HARDENING_TEST_ACTIONS:?}" ;;
  is-active) printf 'active\n' ;;
  *) exit 64 ;;
esac
"""
    )
    for command in (getent, ssh_keygen, sshd, systemctl):
        command.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DGX_SSHD_CONFIG_DIR": str(config_dir),
        "DGX_SSHD_BIN": str(sshd),
        "DGX_SYSTEMCTL_BIN": str(systemctl),
        "DGX_GETENT_BIN": str(getent),
        "DGX_SSH_KEYGEN_BIN": str(ssh_keygen),
        "HARDENING_TEST_ACTIONS": str(actions),
    }
    return {
        "environment": environment,
        "config_dir": config_dir,
        "recovery": recovery,
        "actions": actions,
    }


def _run(
    host: dict[str, object],
    action: str,
    *,
    user: str = "operator",
    fingerprint: str = FINGERPRINT,
    recovery: bool = True,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "bash",
        str(SCRIPT),
        "--admin-user",
        user,
        "--admin-key-fingerprint",
        fingerprint,
        "--drop-in",
        str(DROP_IN),
    ]
    if recovery:
        argv.extend(["--recovery-marker", str(host["recovery"])])
    argv.append(action)
    return subprocess.run(
        argv,
        env=host["environment"],
        check=False,
        capture_output=True,
        text=True,
    )


def test_check_apply_verify_and_second_apply_are_idempotent(
    hardening_host: dict[str, object],
) -> None:
    checked = _run(hardening_host, "--check")
    first = _run(hardening_host, "--apply")
    verified = _run(hardening_host, "--verify", recovery=False)
    second = _run(hardening_host, "--apply")

    assert checked.returncode == 2
    assert json.loads(checked.stdout)["status"] == "change-required"
    assert first.returncode == verified.returncode == second.returncode == 0
    assert json.loads(first.stdout)["status"] == "changed"
    assert json.loads(verified.stdout)["status"] == "verified"
    assert json.loads(second.stdout)["status"] == "unchanged"
    target = hardening_host["config_dir"] / "90-dgx-admin.conf"
    assert target.read_bytes() == DROP_IN.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o644
    assert hardening_host["actions"].read_text() == "reload\n"


def test_apply_requires_explicit_verified_recovery_marker(
    hardening_host: dict[str, object],
) -> None:
    no_marker = _run(hardening_host, "--apply", recovery=False)
    hardening_host["recovery"].write_text("not-verified\n")
    wrong_marker = _run(hardening_host, "--apply")

    assert no_marker.returncode == 64
    assert wrong_marker.returncode == 64
    assert not hardening_host["config_dir"].exists()


def test_wrong_user_or_key_fingerprint_never_mutates(
    hardening_host: dict[str, object],
) -> None:
    wrong_user = _run(hardening_host, "--apply", user="missing")
    wrong_key = _run(hardening_host, "--apply", fingerprint="SHA256:wrong")

    assert wrong_user.returncode != 0
    assert wrong_key.returncode != 0
    assert not hardening_host["config_dir"].exists()
    assert hardening_host["actions"].read_text() == ""


def test_foreign_target_is_refused_and_preserved(
    hardening_host: dict[str, object],
) -> None:
    config_dir = hardening_host["config_dir"]
    config_dir.mkdir()
    target = config_dir / "90-dgx-admin.conf"
    target.write_text("PermitRootLogin yes\n")

    result = _run(hardening_host, "--apply")

    assert result.returncode != 0
    assert target.read_text() == "PermitRootLogin yes\n"
    assert hardening_host["actions"].read_text() == ""


def test_rollback_removes_only_matching_managed_drop_in(
    hardening_host: dict[str, object],
) -> None:
    assert _run(hardening_host, "--apply").returncode == 0

    rolled_back = _run(hardening_host, "--rollback")
    checked = _run(hardening_host, "--check", recovery=False)

    assert rolled_back.returncode == 0
    assert json.loads(rolled_back.stdout)["status"] == "rolled-back"
    assert checked.returncode == 2
    assert hardening_host["actions"].read_text() == "reload\nreload\n"


def test_script_requires_all_identity_inputs_and_has_no_defaults(
    hardening_host: dict[str, object],
) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        env=hardening_host["environment"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "--admin-user" in result.stderr
    assert "--admin-key-fingerprint" in result.stderr
