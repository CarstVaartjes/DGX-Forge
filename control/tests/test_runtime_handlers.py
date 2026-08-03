from __future__ import annotations

import hashlib
import json

import pytest

from dgx_control.runtime import RuntimeHandlers
from dgx_control.worker import HandlerRequest


def _request(payload, *, kind="reconcile"):
    return HandlerRequest(
        job_id="job", kind=kind, payload=payload,
        base_commit="a" * 40, targets=("spk_00000000000000000000000000000001",),
    )


def test_reconcile_runs_commit_pinned_release_and_profile_commands(tmp_path) -> None:
    content = {
        "commit": "a" * 40,
        "targets": ["spk_00000000000000000000000000000001"],
        "placements": {"profile": "agent", "workloads": {"model-a": ["spk_00000000000000000000000000000001"]}},
        "routes": {}, "releases": {"model-a": "b" * 64}, "input_digests": {},
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    calls = []
    handlers = RuntimeHandlers(tmp_path, eligible=lambda commit: commit == "a" * 40, current_commit=lambda: "a" * 40, run=lambda argv: calls.append(argv) or {"ok": True})

    result = handlers.reconcile(_request(content | {"plan_digest": digest}))

    assert calls == [
        (str(tmp_path / "scripts/deploy-runtime-release"), "model-a", "--root", str(tmp_path), "--apply"),
        (str(tmp_path / "bin/sparkctl"), "switch", "agent", "--json"),
    ]
    assert result == {"commit": "a" * 40, "plan_digest": digest, "commands": 2}


def test_reconcile_rejects_a_changed_plan_before_running_commands(tmp_path) -> None:
    handlers = RuntimeHandlers(tmp_path, eligible=lambda _commit: True, current_commit=lambda: "a" * 40, run=lambda _argv: pytest.fail("must not run"))
    with pytest.raises(ValueError, match="digest"):
        handlers.reconcile(_request({
            "plan_digest": "0" * 64, "placements": {"profile": "agent", "workloads": {}},
            "routes": {}, "releases": {}, "input_digests": {},
        }))


def test_reconcile_rejects_when_checkout_advanced(tmp_path) -> None:
    handlers = RuntimeHandlers(
        tmp_path, eligible=lambda _commit: True, current_commit=lambda: "b" * 40,
        run=lambda _argv: pytest.fail("must not run"),
    )
    with pytest.raises(ValueError, match="checkout"):
        handlers.reconcile(_request({}))


def test_production_registry_exposes_only_bounded_job_kinds(tmp_path) -> None:
    handlers = RuntimeHandlers(tmp_path, eligible=lambda _commit: True, current_commit=lambda: "a" * 40)
    assert set(handlers.registry()) == {"probe", "reconcile"}
