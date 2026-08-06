from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/platform-release-authority"


def _run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, *arguments],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_exposes_only_delegated_target_and_channel_operations() -> None:
    result = _run("--help")

    assert result.returncode == 0
    assert "publish-target" in result.stdout
    assert "publish-channel" in result.stdout
    assert "root-key" not in result.stdout
    assert "private-key" not in result.stdout


def test_cli_fails_closed_without_oidc_authority_configuration(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"{}\n")
    digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
    result = _run(
        "publish-target",
        "--target-name",
        f"platform/releases/1.2.0/{digest}.json",
        "--target-sha256",
        digest,
        "--target-file",
        str(target),
        env={"PATH": os.environ["PATH"]},
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "delegated authority configuration" in result.stderr


def test_cli_rejects_symlink_target_before_oidc_request(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"{}\n")
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)
    digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
    result = _run(
        "publish-target",
        "--target-name",
        f"platform/releases/1.2.0/{digest}.json",
        "--target-sha256",
        digest,
        "--target-file",
        str(linked),
        env={
            "PATH": os.environ["PATH"],
            "DGX_PLATFORM_AUTHORITY_URL": "https://authority.example.invalid",
            "DGX_PLATFORM_AUTHORITY_AUDIENCE": "audience",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example.invalid",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "x" * 32,
        },
    )

    assert result.returncode == 2
    assert "unsafe" in result.stderr
