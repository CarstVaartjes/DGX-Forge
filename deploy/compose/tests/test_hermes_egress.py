from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy/compose/bin/harden-hermes-egress"


def _tools(tmp_path: Path) -> tuple[dict[str, str], Path]:
    calls = tmp_path / "firewall.calls"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *\"network ls\"*) printf '%s\\n' dgx-test_application dgx-test_data dgx-test_hermes-egress ;;\n"
        "  *\"inspect dgx-test_hermes-egress\"*) printf '%s\\n' 172.30.0.0/24 ;;\n"
        "  *\"inspect dgx-test_application\"*) printf '%s\\n' 172.31.0.0/24 ;;\n"
        "  *\"inspect dgx-test_data\"*) printf '%s\\n' 172.29.0.0/24 ;;\n"
        "  *) exit 41 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    firewall = tmp_path / "iptables"
    firewall.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >>{calls}\n"
        "exit 0\n"
    )
    firewall.chmod(0o755)
    return (
        os.environ
        | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "COMPOSE_PROJECT_NAME": "dgx-test",
            "DGX_MANAGEMENT_CIDRS": "10.0.0.0/24",
            "DGX_DIRECT_FABRIC_CIDRS": "192.168.100.0/24,192.168.101.0/24",
            "DOCKER_BIN": str(docker),
            "IPTABLES_BIN": str(firewall),
        },
        calls,
    )


def test_default_is_non_mutating_and_renders_resolved_boundaries(tmp_path: Path) -> None:
    environment, calls = _tools(tmp_path)
    result = subprocess.run(
        ["bash", SCRIPT],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not calls.exists()
    for cidr in (
        "172.30.0.0/24",
        "10.0.0.0/24",
        "192.168.100.0/24",
        "192.168.101.0/24",
        "169.254.0.0/16",
        "172.31.0.0/24",
        "172.29.0.0/24",
    ):
        assert cidr in result.stdout
    assert "RETURN" in result.stdout


def test_apply_is_idempotent_and_verify_never_flushes(tmp_path: Path) -> None:
    environment, calls = _tools(tmp_path)
    applied = subprocess.run(
        ["bash", SCRIPT, "--apply"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    content = calls.read_text()
    assert "-N DGX_HERMES_EGRESS" in content
    assert "-F DGX_HERMES_EGRESS" in content
    assert "-I DOCKER-USER 1 -s 172.30.0.0/24 -j DGX_HERMES_EGRESS" in content
    assert "-d 10.0.0.0/24 -j REJECT" in content
    assert "-d 169.254.0.0/16 -j REJECT" in content
    assert "-A DGX_HERMES_EGRESS -j RETURN" in content

    calls.unlink()
    verified = subprocess.run(
        ["bash", SCRIPT, "--verify"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    content = calls.read_text()
    assert "-C DOCKER-USER" in content
    assert "-C DGX_HERMES_EGRESS" in content
    assert " -F " not in f" {content} "
    assert " -N " not in f" {content} "


def test_unresolved_or_broad_cidrs_fail_before_firewall_mutation(tmp_path: Path) -> None:
    environment, calls = _tools(tmp_path)
    for value in ("$UNRESOLVED", "0.0.0.0/0", "10.0.0.0/7"):
        result = subprocess.run(
            ["bash", SCRIPT, "--apply"],
            env=environment | {"DGX_MANAGEMENT_CIDRS": value},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
    assert not calls.exists()
