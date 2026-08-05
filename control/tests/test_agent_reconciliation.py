from __future__ import annotations

import hashlib

import pytest
from dgx_agent_protocol import canonical_message
from dgx_control.agent_reconciliation import (
    accepted_result_digests,
    compensation_order,
    ready_operation_ids,
)
from dgx_control.orchestration import OperationNode


NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _node(
    operation_id: str,
    kind: str,
    dependencies: tuple[str, ...] = (),
    *,
    node_id: str = NODE_A,
    workload_id: str = "model",
    compensation_kind: str | None = None,
    payload: dict[str, object] | None = None,
) -> OperationNode:
    payload = {} if payload is None else payload
    return OperationNode(
        operation_id,
        node_id,
        workload_id,
        kind,
        dependencies,
        compensation_kind,
        _digest(payload),
    )


def test_dependency_waves_trust_only_accepted_projection_rows() -> None:
    nodes = (
        _node("worker:start", "workload.start", compensation_kind="workload.stop"),
        _node(
            "entrypoint:start",
            "workload.start",
            ("worker:start",),
            node_id=NODE_B,
            compensation_kind="workload.stop",
        ),
        _node(
            "entrypoint:verify",
            "workload.verify",
            ("entrypoint:start",),
            node_id=NODE_B,
        ),
    )

    assert ready_operation_ids(nodes, {}) == ("worker:start",)
    assert ready_operation_ids(nodes, {"worker:start": "succeeded"}) == ()
    assert ready_operation_ids(nodes, {"worker:start": "accepted"}) == (
        "entrypoint:start",
    )
    assert ready_operation_ids(
        nodes,
        {"worker:start": "accepted", "entrypoint:start": "accepted"},
    ) == ("entrypoint:verify",)


def test_ready_wave_is_deterministic_for_sixteen_independent_nodes() -> None:
    nodes = tuple(
        _node(
            f"worker-{index:02d}:prepare",
            "workload.prepare",
            node_id="spk_" + f"{index:032x}",
        )
        for index in reversed(range(16))
    )

    assert ready_operation_ids(nodes, {}) == tuple(
        f"worker-{index:02d}:prepare" for index in range(16)
    )


def test_compensation_reverses_graph_order_for_accepted_starts_only() -> None:
    nodes = (
        _node("worker:start", "workload.start", compensation_kind="workload.stop"),
        _node(
            "entrypoint:start",
            "workload.start",
            ("worker:start",),
            node_id=NODE_B,
            compensation_kind="workload.stop",
        ),
        _node("entrypoint:health", "workload.health", ("entrypoint:start",)),
    )

    assert compensation_order(
        nodes,
        {"worker:start": "accepted", "entrypoint:start": "accepted"},
    ) == ("entrypoint:start", "worker:start")
    assert compensation_order(nodes, {"worker:start": "succeeded"}) == ()


def test_release_evidence_is_bound_to_the_exact_request() -> None:
    payload = {
        "schema_version": 1,
        "target_name": "model",
        "oci_manifest_digest": "sha256:" + "9" * 64,
        "target_digest": "a" * 64,
        "provenance_digest": "b" * 64,
        "adapter_id": "spark-runtime-v1",
    }
    result = {
        "status": "ok",
        "evidence": {
            "status": "installed",
            "release_digest": "a" * 64,
            "manifest_digest": "sha256:" + "9" * 64,
            "adapter_id": "spark-runtime-v1",
        },
    }

    result_digest, evidence_digest = accepted_result_digests(
        "release.install", payload, result
    )

    assert result_digest == _digest(result)
    assert evidence_digest == _digest(result["evidence"])
    bad = dict(result)
    bad["evidence"] = dict(result["evidence"], adapter_id="other")
    with pytest.raises(ValueError, match="release evidence"):
        accepted_result_digests("release.install", payload, bad)


@pytest.mark.parametrize(
    ("kind", "extra", "action"),
    (
        ("workload.prepare", {"profile_digest": "c" * 64}, "prepare"),
        ("workload.start", {"preparation_digest": "d" * 64}, "start"),
        ("workload.stop", {}, "stop"),
        ("workload.health", {}, "health"),
        ("workload.verify", {"expected_digest": "e" * 64}, "verify"),
    ),
)
def test_workload_evidence_binds_action_identity_release_and_verify_digest(
    kind: str, extra: dict[str, object], action: str
) -> None:
    payload = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "spark-runtime-v1",
    } | extra
    result = {
        "status": "ok",
        "evidence": {
            "status": "healthy" if action == "health" else "completed",
            "action": action,
            "workload_id": "model",
            "release_digest": "a" * 64,
            "evidence_digest": "e" * 64,
        },
    }

    accepted_result_digests(kind, payload, result)

    for field, value in (
        ("action", "start" if action != "start" else "stop"),
        ("workload_id", "other"),
        ("release_digest", "f" * 64),
    ):
        bad = dict(result)
        bad["evidence"] = dict(result["evidence"], **{field: value})
        with pytest.raises(ValueError, match="workload evidence"):
            accepted_result_digests(kind, payload, bad)

    if kind == "workload.verify":
        bad = dict(result)
        bad["evidence"] = dict(result["evidence"], evidence_digest="f" * 64)
        with pytest.raises(ValueError, match="verify"):
            accepted_result_digests(kind, payload, bad)


def test_node_gate_requires_exact_zero_compute_evidence() -> None:
    payload = {"require_active_nvidia_compute_processes": 0}
    result = {
        "status": "ok",
        "evidence": {
            "dgx_forge": {
                "schema_version": 1,
                "accelerator": {"active_nvidia_compute_processes": 0},
            },
            "nvidia": {"tools": {}},
        },
    }

    accepted_result_digests("node.probe", payload, result)
    result["evidence"]["dgx_forge"]["accelerator"][
        "active_nvidia_compute_processes"
    ] = 1
    with pytest.raises(ValueError, match="compute gate"):
        accepted_result_digests("node.probe", payload, result)


@pytest.mark.parametrize(
    "result",
    (
        {},
        {"status": "failed", "error_code": "workload_failed"},
        {"status": "ok"},
        {"status": "ok", "evidence": []},
    ),
)
def test_only_canonical_success_evidence_can_be_accepted(result: object) -> None:
    with pytest.raises((TypeError, ValueError), match="result|evidence"):
        accepted_result_digests("workload.stop", {}, result)
