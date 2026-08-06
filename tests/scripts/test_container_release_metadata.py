from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/container-release-metadata"
SHA = "0123456789abcdef0123456789abcdef01234567"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_stable_tag_emits_exact_public_package_metadata() -> None:
    result = run("tag", "v1.2.3", SHA)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "version=1.2.3",
        f"commit_tag=sha-{SHA}",
        "api_image=ghcr.io/carstvaartjes/dgx-forge-api",
        "worker_image=ghcr.io/carstvaartjes/dgx-forge-worker",
        "hermes_image=ghcr.io/carstvaartjes/dgx-forge-hermes",
        "agent_artifact=ghcr.io/carstvaartjes/dgx-forge-agent",
        "supervisor_artifact=ghcr.io/carstvaartjes/dgx-forge-agent-supervisor",
        "tooling_artifact=ghcr.io/carstvaartjes/dgx-forge-tooling",
        (
            "deployment_bundle_repository="
            "ghcr.io/carstvaartjes/dgx-forge-control-deployment"
        ),
        "platform_channel=stable",
    ]


@pytest.mark.parametrize(
    ("ref_type", "ref_name", "commit"),
    (
        ("branch", "v1.2.3", SHA),
        ("tag", "1.2.3", SHA),
        ("tag", "v01.2.3", SHA),
        ("tag", "v1.2", SHA),
        ("tag", "v1.2.3-rc.1", SHA),
        ("tag", "v1.2.3+build.1", SHA),
        ("tag", "v1.2.3", SHA.upper()),
        ("tag", "v1.2.3", SHA[:-1]),
    ),
)
def test_non_release_input_fails_closed(
    ref_type: str, ref_name: str, commit: str
) -> None:
    result = run(ref_type, ref_name, commit)
    assert result.returncode == 64
    assert result.stdout == ""
    assert "release metadata is invalid" in result.stderr
