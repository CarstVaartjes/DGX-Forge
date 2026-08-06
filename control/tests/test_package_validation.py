from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from dgx_control.package_validation import ValidationController, ValidationError


def _candidate() -> dict[str, object]:
    return {
        "id": "candidate-1",
        "state": "resolved",
        "family_id": "future-stack",
        "release_digest": "a" * 64,
        "lock": {
            "family_id": "future-stack",
            "compatibility": {
                "architectures": ["linux-arm64"],
                "operating_systems": ["linux"],
                "required_capabilities": ["package-abi-v1"],
                "minimum_storage_bytes": 1,
            },
            "adapter_abi": 1,
            "provenance": [{"kind": "slsa", "digest": "sha256:" + "b" * 64}],
            "validation": [{"kind": "artifact", "required": True}],
        },
        "policy": {"required_evidence": ["slsa"]},
    }


FLEET = {
    "spk_" + "1" * 32: {
        "architecture": "linux-arm64",
        "operating_system": "linux",
        "storage_available_bytes": 100,
        "capabilities": ["package-abi-v1"],
        "authenticated": True,
        "online": True,
        "healthy": True,
        "adapter_abis": [1],
    }
}


def test_plan_schedules_prepare_and_verify_only() -> None:
    queued: list[dict[str, object]] = []
    controller = ValidationController(
        candidate_loader=lambda candidate_id: _candidate(),
        fleet_loader=lambda: FLEET,
        enqueue=lambda operation: queued.append(operation) or "op-1",
        clock=lambda: datetime(2026, 8, 6, tzinfo=UTC),
    )

    plan = controller.plan("candidate-1")

    assert plan.node_ids == ("spk_" + "1" * 32,)
    assert tuple(operation["kind"] for operation in queued) == (
        "package.prepare",
        "package.health",
    )
    assert all(operation["payload"]["release_digest"] == plan.release_digest for operation in queued)
    assert "package.activate" not in {operation["kind"] for operation in queued}
    assert plan.digest == hashlib.sha256(plan.canonical_bytes).hexdigest()


def test_validation_advances_to_passed_only_with_required_evidence() -> None:
    statuses = iter(("running", "passed"))
    controller = ValidationController(
        candidate_loader=lambda candidate_id: _candidate(),
        fleet_loader=lambda: FLEET,
        runner=lambda _run: {"status": next(statuses), "evidence": {"slsa": "c" * 64}},
        clock=lambda: datetime(2026, 8, 6, tzinfo=UTC),
    )
    plan = controller.plan("candidate-1")
    assert controller.advance(plan.run_id).state == "running"
    result = controller.advance(plan.run_id)
    assert result.state == "passed"
    assert result.evidence_digest == "7f2fec7727d32fa28bb0eac53e6c506018dfb29c4c22af656724683d1dc5789f"


def test_validation_rejects_missing_evidence_and_no_compatible_nodes() -> None:
    controller = ValidationController(
        candidate_loader=lambda candidate_id: _candidate(),
        fleet_loader=lambda: {"spk_" + "1" * 32: {"online": False}},
    )
    with pytest.raises(ValidationError, match="no compatible nodes"):
        controller.plan("candidate-1")

    controller = ValidationController(
        candidate_loader=lambda candidate_id: _candidate(),
        fleet_loader=lambda: FLEET,
        runner=lambda _run: {"status": "passed", "evidence": {}},
    )
    plan = controller.plan("candidate-1")
    with pytest.raises(ValidationError, match="required evidence"):
        controller.advance(plan.run_id)
