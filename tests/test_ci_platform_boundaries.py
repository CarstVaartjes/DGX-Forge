from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_linux_node_runtime_cases_skip_on_non_linux_hosts() -> None:
    test_cases = (
        (
            "tests/nodes/test_inspect_node_identity.py::"
            "test_identity_probe_emits_hashes_and_public_fingerprints_not_raw_identity"
        ),
        (
            "tests/nodes/test_inspect_node_identity.py::"
            "test_identity_probe_marks_invalid_machine_id_for_console_repair"
        ),
        (
            "tests/nodes/test_install_ssh_hardening.py::"
            "test_check_apply_verify_and_second_apply_are_idempotent"
        ),
        (
            "tests/nodes/test_install_ssh_hardening.py::"
            "test_rollback_removes_only_matching_managed_drop_in"
        ),
    )
    command = (
        "import sys; "
        "sys.platform = 'darwin'; "
        "import pytest; "
        f"raise SystemExit(pytest.main({['-q', *test_cases]!r}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=ROOT,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 skipped" in result.stdout


def test_image_runtime_case_skips_when_only_compose_is_available(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "[[ ${1:-} == compose ]] || {\n"
        "  echo 'only docker compose is available' >&2\n"
        "  exit 2\n"
        "}\n"
        "exit 0\n"
    )
    docker.chmod(0o755)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            (
                "tests/runbooks/test_agent_pki.py::"
                "test_pinned_step_image_supports_documented_jwk_thumbprint_command"
            ),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
