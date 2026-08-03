from __future__ import annotations

import hashlib
import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spark_profiles.fleet import ManagementEndpoint, NodeId
from spark_profiles.fleet.install_contracts import InstallationRequest
from spark_profiles.install.orchestrator import (
    FileEvidenceStore,
    NodeInstaller,
    StepResult,
    WaitForOperator,
)
from spark_profiles.install.store import InstallStore

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
STEP_NAMES = (
    "identity",
    "pre-inventory",
    "public-key",
    "ssh-hardening",
    "node-policy",
    "post-inventory",
    "acceptance",
)


class TickClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


def _request(index: int, display_name: str) -> InstallationRequest:
    return InstallationRequest(
        node_id=NodeId.parse(f"spk_{index:032x}"),
        display_name=display_name,
        endpoint=ManagementEndpoint(
            host=f"{display_name}.local",
            user="operator",
            credential_ref="secret://ssh/admin",
        ),
        labels={},
    )


def _installer(
    tmp_path: Path,
    handlers: dict[str, Callable[[InstallationRequest], StepResult]],
    clock: TickClock | None = None,
) -> NodeInstaller:
    active_clock = clock or TickClock()
    return NodeInstaller(
        store=InstallStore(tmp_path / "state", clock=active_clock),
        evidence_store=FileEvidenceStore(tmp_path / "evidence"),
        handlers=handlers,
        clock=active_clock,
    )


def test_installer_executes_declared_gates_and_accepts_one_node(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handler(name: str):
        def run(request: InstallationRequest) -> StepResult:
            calls.append(name)
            return StepResult(stdout=f"{request.display_name}:{name}\n".encode(), stderr=b"")

        return run

    installer = _installer(tmp_path, {name: handler(name) for name in STEP_NAMES})
    journal = installer.start(_request(1, "alpha"))

    completed = installer.run(journal.request.node_id)

    assert completed.state == "accepted"
    assert calls == list(STEP_NAMES)
    assert [step.state for step in completed.steps] == [
        "identity-gated",
        "inventoried",
        "key-installed",
        "hardened",
        "policy-applied",
        "post-inventoried",
        "accepted",
    ]


def test_installer_retries_failed_step_without_repeating_completed_gates(
    tmp_path: Path,
) -> None:
    calls = {name: 0 for name in STEP_NAMES}

    def handler(name: str):
        def run(_: InstallationRequest) -> StepResult:
            calls[name] += 1
            if name == "public-key" and calls[name] == 1:
                raise RuntimeError("temporary transport token=secret")
            return StepResult(stdout=name.encode(), stderr=b"")

        return run

    installer = _installer(tmp_path, {name: handler(name) for name in STEP_NAMES})
    node_id = installer.start(_request(1, "alpha")).request.node_id

    failed = installer.run(node_id)
    installer.retry(node_id)
    completed = installer.run(node_id)

    assert failed.state == "failed"
    assert "secret" not in failed.failure_reason
    assert completed.state == "accepted"
    assert calls["identity"] == calls["pre-inventory"] == 1
    assert calls["public-key"] == 2
    assert all(calls[name] == 1 for name in STEP_NAMES[3:])


def test_wait_for_console_is_persisted_and_requires_explicit_resume(
    tmp_path: Path,
) -> None:
    identity_calls = 0

    def identity(_: InstallationRequest) -> StepResult:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise WaitForOperator("verify physical console token=sensitive")
        return StepResult(stdout=b"verified", stderr=b"")

    handlers = {
        name: (identity if name == "identity" else lambda _: StepResult(b"ok", b""))
        for name in STEP_NAMES
    }
    installer = _installer(tmp_path, handlers)
    node_id = installer.start(_request(1, "alpha")).request.node_id

    waiting = installer.run(node_id)
    still_waiting = installer.run(node_id)
    installer.resume(node_id)
    completed = installer.run(node_id)

    assert waiting.waiting_reason == still_waiting.waiting_reason
    assert "sensitive" not in waiting.waiting_reason
    assert identity_calls == 2
    assert completed.state == "accepted"


def test_failure_on_one_target_never_touches_another_started_node(
    tmp_path: Path,
) -> None:
    touched: list[str] = []

    def fail_alpha(request: InstallationRequest) -> StepResult:
        touched.append(request.display_name)
        if request.display_name == "alpha":
            raise RuntimeError("stop")
        return StepResult(b"ok", b"")

    handlers = {name: fail_alpha for name in STEP_NAMES}
    installer = _installer(tmp_path, handlers)
    alpha = installer.start(_request(1, "alpha"))
    installer.start(_request(2, "beta"))

    failed = installer.run(alpha.request.node_id)

    assert failed.state == "failed"
    assert touched == ["alpha"]


def test_evidence_is_content_addressed_and_restrictive(tmp_path: Path) -> None:
    store = FileEvidenceStore(tmp_path / "evidence")
    result = StepResult(
        stdout=b"safe output",
        stderr=b"Authorization: Bearer evidence-secret",
    )

    digest = store.save(
        NodeId.parse("spk_00000000000000000000000000000001"),
        "identity",
        1,
        result,
    )

    evidence_path = next((tmp_path / "evidence").glob("spk_*/*.json"))
    assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == digest
    assert b"evidence-secret" not in evidence_path.read_bytes()
    payload = json.loads(evidence_path.read_text())
    assert b"[REDACTED]" in base64.b64decode(payload["stderr_base64"])
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert evidence_path.parent.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "evidence").stat().st_mode & 0o777 == 0o700


def test_installer_rejects_missing_or_extra_handler_registry(tmp_path: Path) -> None:
    handler = lambda _: StepResult(b"ok", b"")

    with pytest.raises(ValueError, match="handler registry"):
        _installer(tmp_path, {"identity": handler})
    with pytest.raises(ValueError, match="handler registry"):
        _installer(
            tmp_path,
            {**{name: handler for name in STEP_NAMES}, "unexpected": handler},
        )
