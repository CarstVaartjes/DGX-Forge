from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cluster_profiles.fleet import NodeId
from cluster_profiles.install.cli import CliDependencies, main
from cluster_profiles.install.orchestrator import (
    FileEvidenceStore,
    NodeInstaller,
    StepResult,
    WaitForOperator,
)
from cluster_profiles.install.store import InstallStore

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "bin" / "spark-install"
NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


class TickClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


def _dependencies(tmp_path: Path, *, wait_once: bool = False) -> CliDependencies:
    clock = TickClock()
    identity_calls = 0

    def identity(_):
        nonlocal identity_calls
        identity_calls += 1
        if wait_once and identity_calls == 1:
            raise WaitForOperator("verify console")
        return StepResult(b"ok", b"")

    step_names = (
        "identity",
        "pre-inventory",
        "public-key",
        "ssh-hardening",
        "node-policy",
        "post-inventory",
        "acceptance",
    )
    handlers = {
        name: (identity if name == "identity" else lambda _: StepResult(b"ok", b""))
        for name in step_names
    }
    installer = NodeInstaller(
        store=InstallStore(tmp_path / "state", clock=clock),
        evidence_store=FileEvidenceStore(tmp_path / "evidence"),
        handlers=handlers,
        clock=clock,
    )
    return CliDependencies(
        installer=installer,
        node_id_factory=lambda: NodeId.parse(
            "spk_00000000000000000000000000000042"
        ),
    )


def _start_args(*extra: str) -> list[str]:
    return [
        "node",
        "start",
        "--host",
        "alpha.local",
        "--user",
        "operator",
        "--credential-ref",
        "secret://ssh/admin",
        "--display-name",
        "alpha",
        "--label",
        "rack=lab",
        "--json",
        *extra,
    ]


def test_start_defaults_to_nonmutating_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dependencies = _dependencies(tmp_path)

    result = main(_start_args(), dependencies=lambda: dependencies)
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["mode"] == "plan"
    assert payload["node_id"] == "spk_00000000000000000000000000000042"
    assert payload["host"] == "alpha.local"
    assert payload["labels"] == {"rack": "lab"}
    assert not (tmp_path / "state" / f"{payload['node_id']}.json").exists()


def test_start_apply_runs_all_gates_and_status_reads_journal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dependencies = _dependencies(tmp_path)

    assert main(_start_args("--apply"), dependencies=lambda: dependencies) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["state"] == "accepted"

    assert main(
        ["node", "status", applied["node_id"], "--json"],
        dependencies=lambda: dependencies,
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "accepted"
    assert status["completed_gates"] == 7


def test_waiting_install_requires_explicit_resume_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dependencies = _dependencies(tmp_path, wait_once=True)
    main(_start_args("--apply"), dependencies=lambda: dependencies)
    waiting = json.loads(capsys.readouterr().out)

    assert waiting["state"] == "discovered"
    assert waiting["waiting_reason"] == "verify console"
    main(
        ["node", "resume", waiting["node_id"], "--json"],
        dependencies=lambda: dependencies,
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "plan"
    main(
        ["node", "status", waiting["node_id"], "--json"],
        dependencies=lambda: dependencies,
    )
    assert json.loads(capsys.readouterr().out)["waiting_reason"] == "verify console"

    main(
        ["node", "resume", waiting["node_id"], "--json", "--apply"],
        dependencies=lambda: dependencies,
    )
    completed = json.loads(capsys.readouterr().out)
    assert completed["state"] == "accepted"


def test_cli_rejects_duplicate_or_malformed_labels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dependencies = _dependencies(tmp_path)

    result = main(
        _start_args("--label", "rack=other"),
        dependencies=lambda: dependencies,
    )

    assert result == 2
    assert "duplicate label" in capsys.readouterr().err


def test_launcher_requires_name_address_user_and_credential_reference() -> None:
    completed = subprocess.run(
        ["uv", "run", "--no-project", "--with", "jsonschema", str(LAUNCHER), "node", "start", "--apply"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--host" in completed.stderr
    assert "--user" in completed.stderr
    assert "--credential-ref" in completed.stderr
    assert "--display-name" in completed.stderr


def test_launcher_valid_dry_run_does_not_require_live_step_configuration() -> None:
    completed = subprocess.run(
        [
            str(LAUNCHER),
            "node",
            "start",
            "--host",
            "dynamic.local",
            "--user",
            "operator",
            "--credential-ref",
            "secret://ssh/admin",
            "--display-name",
            "dynamic",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["mode"] == "plan"
    assert payload["node_id"].startswith("spk_")


def test_launcher_help_lists_resumable_node_commands() -> None:
    completed = subprocess.run(
        [str(LAUNCHER), "node", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    for command in ("start", "status", "resume", "retry", "verify", "emit-record"):
        assert command in completed.stdout
