"""Durable, evidence-gated execution of persisted reconciliation graphs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from dgx_agent_protocol import canonical_message

from .orchestration import OperationNode

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKLOAD_ACTIONS = {
    "workload.prepare": "prepare",
    "workload.start": "start",
    "workload.stop": "stop",
    "workload.health": "health",
    "workload.verify": "verify",
}


def _digest(document: object) -> str:
    return hashlib.sha256(canonical_message(document)).hexdigest()


def ready_operation_ids(
    nodes: Sequence[OperationNode], states: Mapping[str, str]
) -> tuple[str, ...]:
    """Return the deterministic next wave using accepted projections only."""

    accepted = {operation_id for operation_id, state in states.items() if state == "accepted"}
    pending = {
        node.operation_id
        for node in nodes
        if node.operation_id not in states
        and all(dependency in accepted for dependency in node.dependencies)
    }
    return tuple(sorted(pending))


def compensation_order(
    nodes: Sequence[OperationNode], states: Mapping[str, str]
) -> tuple[str, ...]:
    """Return accepted compensatable mutations in reverse graph order."""

    return tuple(
        node.operation_id
        for node in reversed(tuple(nodes))
        if states.get(node.operation_id) == "accepted"
        and node.compensation_kind is not None
    )


def accepted_result_digests(
    kind: str,
    payload: Mapping[str, object],
    result: object,
) -> tuple[str, str]:
    """Authenticate bounded agent evidence against the exact dispatched request."""

    if not isinstance(result, Mapping) or set(result) != {"status", "evidence"}:
        raise ValueError("accepted agent result is invalid")
    if result.get("status") != "ok":
        raise ValueError("accepted agent result status is invalid")
    evidence = result.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TypeError("accepted agent result evidence is invalid")
    if kind == "release.install":
        _release_evidence(payload, evidence)
    elif kind in _WORKLOAD_ACTIONS:
        _workload_evidence(kind, payload, evidence)
    elif kind == "node.probe":
        _probe_evidence(payload, evidence)
    else:
        raise ValueError("accepted agent result operation is invalid")
    return _digest(result), _digest(evidence)


def _release_evidence(
    payload: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    if set(evidence) != {
        "status",
        "release_digest",
        "manifest_digest",
        "adapter_id",
    }:
        raise ValueError("release evidence is invalid")
    if evidence.get("status") not in {"installed", "already-installed"}:
        raise ValueError("release evidence status is invalid")
    if (
        evidence.get("release_digest") != payload.get("target_digest")
        or evidence.get("manifest_digest") != payload.get("oci_manifest_digest")
        or evidence.get("adapter_id") != payload.get("adapter_id")
        or not isinstance(evidence.get("release_digest"), str)
        or _DIGEST.fullmatch(evidence["release_digest"]) is None
        or not isinstance(evidence.get("manifest_digest"), str)
        or _OCI_DIGEST.fullmatch(evidence["manifest_digest"]) is None
        or not isinstance(evidence.get("adapter_id"), str)
        or not evidence["adapter_id"]
    ):
        raise ValueError("release evidence does not match the request")


def _workload_evidence(
    kind: str,
    payload: Mapping[str, object],
    evidence: Mapping[str, object],
) -> None:
    if set(evidence) != {
        "status",
        "action",
        "workload_id",
        "release_digest",
        "evidence_digest",
    }:
        raise ValueError("workload evidence is invalid")
    evidence_digest = evidence.get("evidence_digest")
    if (
        not isinstance(evidence.get("status"), str)
        or not evidence["status"]
        or evidence.get("action") != _WORKLOAD_ACTIONS[kind]
        or evidence.get("workload_id") != payload.get("workload_id")
        or evidence.get("release_digest") != payload.get("release_digest")
        or not isinstance(evidence_digest, str)
        or _DIGEST.fullmatch(evidence_digest) is None
    ):
        raise ValueError("workload evidence does not match the request")
    if kind == "workload.verify" and evidence_digest != payload.get(
        "expected_digest"
    ):
        raise ValueError("workload verify evidence digest does not match the request")


def _probe_evidence(
    payload: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    if payload != {"require_active_nvidia_compute_processes": 0}:
        raise ValueError("node probe request is not an authenticated compute gate")
    health = evidence.get("dgx_forge")
    nvidia = evidence.get("nvidia")
    accelerator = health.get("accelerator") if isinstance(health, Mapping) else None
    if (
        set(evidence) != {"dgx_forge", "nvidia"}
        or not isinstance(health, Mapping)
        or health.get("schema_version") != 1
        or not isinstance(accelerator, Mapping)
        or accelerator.get("active_nvidia_compute_processes") != 0
        or not isinstance(nvidia, Mapping)
    ):
        raise ValueError("node probe compute gate evidence is invalid")
