"""Deterministic failure-injection simulator for the outbound agent lifecycle.

The simulator uses the production ``AgentStateStore`` and ``OperationRegistry``.
Only the network and release-host effects are represented in memory; its output
is therefore acceptance evidence for protocol behavior, never physical-host
evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from dgx_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)

from .operations import OperationContext, OperationRegistry
from .releases import (
    ReleaseDisposition,
    ReleaseEvidence,
    ReleaseInspection,
    ReleaseInstallError,
    ReleaseRequest,
)
from .state import AgentStateConflict, AgentStateStore

_BASE_RELEASE = "0" * 64
DEFAULT_CLI_NODE_SAFETY_THRESHOLD = 256
_FAULTS = (
    "bad-artifact",
    "bad-certificate",
    "crash",
    "disconnect",
    "failed-activation",
    "stale-fence",
)
_BAD_ARTIFACT_TRANSITIONS = (
    "candidate-staged:B:generation-1",
    "artifact-validation-rejected:B:generation-1",
    "candidate-cleared:B:generation-1",
)
_FAILED_ACTIVATION_TRANSITIONS = (
    "candidate-staged:B:generation-1",
    "artifact-validated:B:generation-1",
    "activation-attempted:A->B:generation-1",
    "active-switched:A->B:generation-1",
    "readiness-failed:B:generation-1",
    "active-restored:B->A:generation-1",
    "candidate-cleared:B:generation-1",
)


class SimulationError(RuntimeError):
    """The deterministic acceptance scenario did not preserve an invariant."""


class SimulatedTransportError(RuntimeError):
    """An injected in-memory transport failure."""


class _SeededClock:
    def __init__(self, seed: int) -> None:
        generator = random.Random(seed)
        self._current = datetime(2090, 1, 1, tzinfo=UTC) + timedelta(
            seconds=generator.randrange(20 * 365 * 24 * 60 * 60)
        )

    def instant(self) -> datetime:
        value = self._current
        self._current += timedelta(milliseconds=1)
        return value

    def deadline(self) -> datetime:
        return self.instant() + timedelta(minutes=5)


class _InMemoryTransport:
    def __init__(self, node_ids: tuple[str, ...], seed: int) -> None:
        self._certificates = {
            node_id: _digest(seed, node_id, "certificate") for node_id in node_ids
        }
        self._disconnect_after_result: set[str] = set()

    def certificate(self, node_id: str) -> str:
        return self._certificates[node_id]

    def inject_disconnect(self, node_id: str) -> None:
        self._disconnect_after_result.add(node_id)

    def exchange(
        self,
        *,
        target_node: str,
        certificate: str,
        claim: AgentClaim,
        registry: OperationRegistry,
        context: OperationContext,
    ):
        self._authenticate(target_node, certificate)
        execution = registry.execute(claim, context)
        if target_node in self._disconnect_after_result:
            self._disconnect_after_result.remove(target_node)
            raise SimulatedTransportError("simulated disconnect after execution")
        return execution

    def deliver_result(
        self,
        *,
        target_node: str,
        certificate: str,
        result: AgentResult,
        state: AgentStateStore,
    ) -> None:
        self._authenticate(target_node, certificate)
        state.finish(result)

    def _authenticate(self, target_node: str, certificate: str) -> None:
        if self._certificates.get(target_node) != certificate:
            raise SimulatedTransportError("simulated certificate rejection")


class _Probe:
    def collect(self, deadline: datetime) -> Mapping[str, object]:
        return {"simulated": True, "status": "ok"}


class _ReleaseHost:
    """In-memory A/B release effect boundary used by the real registry."""

    def __init__(self, fault: str | None = None) -> None:
        self.slots: dict[str, str | None] = {"A": _BASE_RELEASE, "B": None}
        self.active_slot = "A"
        self.previous_slot: str | None = None
        self.pending_slot: str | None = None
        self.pending_generation = 0
        self.transition_history: list[str] = []
        self.install_calls = 0
        self.durable_mutations = 0
        self._fault = fault

    @property
    def active_digest(self) -> str:
        digest = self.slots[self.active_slot]
        if digest is None:
            raise SimulationError("simulated active slot is empty")
        return digest

    def install(self, request: ReleaseRequest, deadline) -> ReleaseEvidence:
        self.install_calls += 1
        self._stage(request)
        if self._fault == "bad-artifact":
            self._record("artifact-validation-rejected", self.pending_slot)
            self._clear_candidate()
            raise ReleaseInstallError("simulated artifact validation failure")
        self._record("artifact-validated", self.pending_slot)
        self._activate_candidate()
        if self._fault == "failed-activation":
            self._record("readiness-failed", self.active_slot)
            self._restore_previous()
            self._clear_candidate()
            raise ReleaseInstallError("simulated activation failure")
        self._record("readiness-passed", self.active_slot)
        self._commit_candidate()
        return self._evidence(request, "installed")

    def inspect(self, request: ReleaseRequest, deadline) -> ReleaseInspection:
        if self.active_digest == request.target_digest:
            return ReleaseInspection(
                ReleaseDisposition.COMPLETED,
                self._evidence(request, "already-installed"),
            )
        return ReleaseInspection(ReleaseDisposition.READY)

    def activate_before_crash(self, request: ReleaseRequest) -> None:
        self._stage(request)
        self._record("artifact-validated", self.pending_slot)
        self._activate_candidate()
        self._record("readiness-passed", self.active_slot)
        self._commit_candidate()

    def _stage(self, request: ReleaseRequest) -> None:
        if self.pending_slot is not None:
            raise SimulationError("simulated candidate is already pending")
        candidate = "B" if self.active_slot == "A" else "A"
        self.pending_generation += 1
        self.pending_slot = candidate
        self.slots[candidate] = request.target_digest
        self._record("candidate-staged", candidate)

    def _activate_candidate(self) -> None:
        if self.pending_slot is None:
            raise SimulationError("simulated candidate is not pending")
        previous = self.active_slot
        candidate = self.pending_slot
        self.previous_slot = previous
        self._record("activation-attempted", f"{previous}->{candidate}")
        self.active_slot = candidate
        self.durable_mutations += 1
        self._record("active-switched", f"{previous}->{candidate}")

    def _restore_previous(self) -> None:
        if self.previous_slot is None:
            raise SimulationError("simulated previous slot is unavailable")
        failed = self.active_slot
        restored = self.previous_slot
        self.active_slot = restored
        self.previous_slot = None
        self.durable_mutations += 1
        self._record("active-restored", f"{failed}->{restored}")

    def _clear_candidate(self) -> None:
        if self.pending_slot is None:
            raise SimulationError("simulated candidate is not pending")
        candidate = self.pending_slot
        if candidate == self.active_slot:
            raise SimulationError("simulated active candidate cannot be cleared")
        self.slots[candidate] = None
        self.pending_slot = None
        self._record("candidate-cleared", candidate)

    def _commit_candidate(self) -> None:
        if self.pending_slot != self.active_slot:
            raise SimulationError("simulated active candidate does not match pending")
        candidate = self.pending_slot
        self.pending_slot = None
        self._record("candidate-committed", candidate)

    def _record(self, action: str, subject: str | None) -> None:
        if subject is None:
            raise SimulationError("simulated transition subject is unavailable")
        self.transition_history.append(
            f"{action}:{subject}:generation-{self.pending_generation}"
        )

    @staticmethod
    def _evidence(request: ReleaseRequest, status: str) -> ReleaseEvidence:
        return ReleaseEvidence(
            status,
            request.target_digest,
            request.oci_manifest_digest,
            request.adapter_id,
        )


def acceptance_argument_parser() -> argparse.ArgumentParser:
    """Build the shared parser for deterministic lifecycle acceptance."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260803)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--allow-large-fleet",
        action="store_true",
        help=(
            "explicitly allow more than "
            f"{DEFAULT_CLI_NODE_SAFETY_THRESHOLD} simulated nodes"
        ),
    )
    return parser


def validate_cli_fleet_size(
    *,
    nodes: int,
    allow_large_fleet: bool,
    safety_threshold: int = DEFAULT_CLI_NODE_SAFETY_THRESHOLD,
) -> None:
    """Refuse accidental CLI scale while preserving an explicit unlimited mode."""
    if not isinstance(safety_threshold, int) or isinstance(safety_threshold, bool):
        raise TypeError("safety threshold must be an integer")
    if safety_threshold < 1:
        raise ValueError("safety threshold must be positive")
    if nodes > safety_threshold and not allow_large_fleet:
        raise ValueError(
            f"node count exceeds the default safety threshold of {safety_threshold}; "
            "pass --allow-large-fleet to proceed explicitly"
        )


def canonical_report(report: Mapping[str, Any]) -> bytes:
    """Encode simulator evidence as canonical newline-terminated JSON."""
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def simulate_agent_lifecycle(*, nodes: int, seed: int = 20260803) -> dict[str, Any]:
    """Exercise the failure matrix against real agent state and dispatch.

    ``nodes`` intentionally has no fleet-size ceiling. Runtime and storage are
    linear in the requested count, while the acceptance plan covers 1 and 16.
    """
    if not isinstance(nodes, int) or isinstance(nodes, bool):
        raise TypeError("nodes must be an integer")
    if nodes < 1:
        raise ValueError("nodes must be a positive integer")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an integer")

    clock = _SeededClock(seed)
    node_ids = tuple(_node_id(seed, index) for index in range(nodes))
    transport = _InMemoryTransport(node_ids, seed)
    counts = {
        "bad-artifact": {
            "artifact_rejections": 0,
            "candidate_activations": 0,
            "candidate_cleanups": 0,
            "injected": 0,
            "pending_candidates_after_fault": 0,
            "rollbacks": 0,
            "safe_outcomes": 0,
            "transition_sequence": [],
            "transition_sequence_mismatches": 0,
        },
        "bad-certificate": {
            "injected": 0,
            "rejections": 0,
            "durable_mutations": 0,
        },
        "crash": {"injected": 0, "recoveries": 0},
        "disconnect": {"injected": 0, "recoveries": 0},
        "failed-activation": {
            "activation_failures": 0,
            "candidate_activations": 0,
            "candidate_cleanups": 0,
            "injected": 0,
            "pending_candidates_after_fault": 0,
            "readiness_failures": 0,
            "rollbacks": 0,
            "safe_outcomes": 0,
            "transition_sequence": [],
            "transition_sequence_mismatches": 0,
        },
        "stale-fence": {
            "injected": 0,
            "claim_rejections": 0,
            "result_rejections": 0,
            "durable_mutations": 0,
        },
    }
    duplicate_mutations = 0
    cross_node_claims_accepted = 0

    with tempfile.TemporaryDirectory(prefix="dgx-agent-simulator-") as raw_root:
        root = Path(raw_root)
        for index, node_id in enumerate(node_ids):
            certificate = transport.certificate(node_id)

            disconnect_claim = _release_claim(seed, index, "disconnect", node_id, clock)
            disconnect_host = _ReleaseHost()
            disconnect_state = AgentStateStore(root / f"{index}-disconnect")
            disconnect_context = _context(node_id, disconnect_state, disconnect_host)
            transport.inject_disconnect(node_id)
            counts["disconnect"]["injected"] += 1
            try:
                transport.exchange(
                    target_node=node_id,
                    certificate=certificate,
                    claim=disconnect_claim,
                    registry=OperationRegistry(),
                    context=disconnect_context,
                )
            except SimulatedTransportError:
                pass
            else:
                raise SimulationError("disconnect injection was not observed")
            restarted = _context(
                node_id,
                AgentStateStore(root / f"{index}-disconnect"),
                disconnect_host,
            )
            replay = transport.exchange(
                target_node=node_id,
                certificate=certificate,
                claim=disconnect_claim,
                registry=OperationRegistry(),
                context=restarted,
            )
            restarted.state.acknowledge(replay.result)
            if replay.replayed and disconnect_host.install_calls == 1:
                counts["disconnect"]["recoveries"] += 1
            duplicate_mutations += max(0, disconnect_host.install_calls - 1)

            crash_claim = _release_claim(seed, index, "crash", node_id, clock)
            crash_host = _ReleaseHost()
            crash_state = AgentStateStore(root / f"{index}-crash")
            crash_state.begin(crash_claim)
            crash_host.activate_before_crash(ReleaseRequest.parse(crash_claim.payload))
            counts["crash"]["injected"] += 1
            crash_context = _context(
                node_id,
                AgentStateStore(root / f"{index}-crash"),
                crash_host,
            )
            recovered = transport.exchange(
                target_node=node_id,
                certificate=certificate,
                claim=crash_claim,
                registry=OperationRegistry(),
                context=crash_context,
            )
            crash_context.state.acknowledge(recovered.result)
            if recovered.replayed and crash_host.install_calls == 0:
                counts["crash"]["recoveries"] += 1
            duplicate_mutations += max(0, crash_host.durable_mutations - 1)

            stale_claim = _release_claim(seed, index, "stale", node_id, clock)
            stale_state = AgentStateStore(root / f"{index}-stale")
            stale_record = stale_state.begin(stale_claim)
            stale_host = _ReleaseHost()
            counts["stale-fence"]["injected"] += 1
            stale_fenced_claim = _replace_fence(
                stale_claim, _uuid(seed, index, "stale-fence")
            )
            try:
                transport.exchange(
                    target_node=node_id,
                    certificate=certificate,
                    claim=stale_fenced_claim,
                    registry=OperationRegistry(),
                    context=_context(node_id, stale_state, stale_host),
                )
            except AgentStateConflict:
                counts["stale-fence"]["claim_rejections"] += 1
            stale_result = _result(stale_fenced_claim, "succeeded")
            try:
                transport.deliver_result(
                    target_node=node_id,
                    certificate=certificate,
                    result=stale_result,
                    state=stale_state,
                )
            except AgentStateConflict:
                counts["stale-fence"]["result_rejections"] += 1
            if (
                stale_state.lookup_exact(stale_claim) != stale_record
                or stale_host.install_calls != 0
                or stale_host.durable_mutations != 0
            ):
                counts["stale-fence"]["durable_mutations"] += 1

            foreign_claim = _release_claim(
                seed,
                index,
                "cross-node",
                _node_id(seed, nodes + index),
                clock,
            )
            try:
                transport.exchange(
                    target_node=node_id,
                    certificate=certificate,
                    claim=foreign_claim,
                    registry=OperationRegistry(),
                    context=_context(
                        node_id,
                        AgentStateStore(root / f"{index}-cross-node"),
                        _ReleaseHost(),
                    ),
                )
            except AgentProtocolError:
                pass
            else:
                cross_node_claims_accepted += 1

            bad_certificate_claim = _release_claim(
                seed, index, "bad-certificate", node_id, clock
            )
            bad_certificate_state = AgentStateStore(
                root / f"{index}-bad-certificate"
            )
            bad_certificate_host = _ReleaseHost()
            counts["bad-certificate"]["injected"] += 1
            try:
                transport.exchange(
                    target_node=node_id,
                    certificate=_digest(seed, node_id, "untrusted-certificate"),
                    claim=bad_certificate_claim,
                    registry=OperationRegistry(),
                    context=_context(
                        node_id,
                        bad_certificate_state,
                        bad_certificate_host,
                    ),
                )
            except SimulatedTransportError:
                counts["bad-certificate"]["rejections"] += 1
            if (
                bad_certificate_state.recover_active() is not None
                or bad_certificate_state.recover_pending() is not None
                or bad_certificate_host.install_calls != 0
                or bad_certificate_host.durable_mutations != 0
            ):
                counts["bad-certificate"]["durable_mutations"] += 1

            for fault in ("bad-artifact", "failed-activation"):
                fault_claim = _release_claim(seed, index, fault, node_id, clock)
                fault_host = _ReleaseHost(fault)
                fault_context = _context(
                    node_id,
                    AgentStateStore(root / f"{index}-{fault}"),
                    fault_host,
                )
                counts[fault]["injected"] += 1
                outcome = transport.exchange(
                    target_node=node_id,
                    certificate=certificate,
                    claim=fault_claim,
                    registry=OperationRegistry(),
                    context=fault_context,
                )
                fault_context.state.acknowledge(outcome.result)
                history = tuple(fault_host.transition_history)
                _observe_transition_sequence(counts[fault], history)
                failed_safely = (
                    outcome.result.state == "failed"
                    and outcome.result.result.get("error_code")
                    == "release_install_failed"
                    and fault_host.active_digest == _BASE_RELEASE
                    and fault_host.active_slot == "A"
                    and fault_host.slots["B"] is None
                )
                counts[fault]["pending_candidates_after_fault"] += int(
                    fault_host.pending_slot is not None
                )
                counts[fault]["candidate_activations"] += history.count(
                    "active-switched:A->B:generation-1"
                )
                counts[fault]["candidate_cleanups"] += history.count(
                    "candidate-cleared:B:generation-1"
                )
                counts[fault]["rollbacks"] += history.count(
                    "active-restored:B->A:generation-1"
                )
                if fault == "bad-artifact":
                    counts[fault]["artifact_rejections"] += history.count(
                        "artifact-validation-rejected:B:generation-1"
                    )
                    transition_ok = history == _BAD_ARTIFACT_TRANSITIONS
                else:
                    counts[fault]["activation_failures"] += int(
                        outcome.result.state == "failed"
                    )
                    counts[fault]["readiness_failures"] += history.count(
                        "readiness-failed:B:generation-1"
                    )
                    transition_ok = history == _FAILED_ACTIVATION_TRANSITIONS
                counts[fault]["safe_outcomes"] += int(
                    failed_safely and transition_ok
                )

    faults = {
        fault: {"evidence_kind": "simulated", **counts[fault]} for fault in _FAULTS
    }
    invariants = {
        "artifact_rejections_without_activation": counts["bad-artifact"][
            "safe_outcomes"
        ],
        "bad_update_rollbacks": counts["failed-activation"]["rollbacks"],
        "bad_update_safe_outcomes": (
            counts["bad-artifact"]["safe_outcomes"]
            + counts["failed-activation"]["safe_outcomes"]
        ),
        "crash_recoveries": counts["crash"]["recoveries"],
        "cross_node_claims_accepted": cross_node_claims_accepted,
        "duplicate_mutations": duplicate_mutations,
        "reconnect_recoveries": counts["disconnect"]["recoveries"],
        "stale_results_accepted": (
            nodes - counts["stale-fence"]["result_rejections"]
        ),
    }
    report = {
        "schema_version": 1,
        "evidence_kind": "simulated",
        "environment": "deterministic-in-memory-transport",
        "physical_sparks_exercised": False,
        "seed": seed,
        "simulated_at": clock.instant().isoformat().replace("+00:00", "Z"),
        "node_count": nodes,
        "node_ids": list(node_ids),
        "faults": faults,
        "invariants": invariants,
    }
    report["status"] = "passed" if lifecycle_evidence_passes(report) else "failed"
    return report


def lifecycle_evidence_passes(report: Mapping[str, Any]) -> bool:
    """Return whether simulated evidence satisfies the complete acceptance gate."""
    nodes = report.get("node_count")
    if not isinstance(nodes, int) or isinstance(nodes, bool) or nodes < 1:
        return False
    expected_faults = {
        "bad-artifact": {
            "artifact_rejections": nodes,
            "candidate_activations": 0,
            "candidate_cleanups": nodes,
            "evidence_kind": "simulated",
            "injected": nodes,
            "pending_candidates_after_fault": 0,
            "rollbacks": 0,
            "safe_outcomes": nodes,
            "transition_sequence": list(_BAD_ARTIFACT_TRANSITIONS),
            "transition_sequence_mismatches": 0,
        },
        "bad-certificate": {
            "durable_mutations": 0,
            "evidence_kind": "simulated",
            "injected": nodes,
            "rejections": nodes,
        },
        "crash": {
            "evidence_kind": "simulated",
            "injected": nodes,
            "recoveries": nodes,
        },
        "disconnect": {
            "evidence_kind": "simulated",
            "injected": nodes,
            "recoveries": nodes,
        },
        "failed-activation": {
            "activation_failures": nodes,
            "candidate_activations": nodes,
            "candidate_cleanups": nodes,
            "evidence_kind": "simulated",
            "injected": nodes,
            "pending_candidates_after_fault": 0,
            "readiness_failures": nodes,
            "rollbacks": nodes,
            "safe_outcomes": nodes,
            "transition_sequence": list(_FAILED_ACTIVATION_TRANSITIONS),
            "transition_sequence_mismatches": 0,
        },
        "stale-fence": {
            "claim_rejections": nodes,
            "durable_mutations": 0,
            "evidence_kind": "simulated",
            "injected": nodes,
            "result_rejections": nodes,
        },
    }
    expected_invariants = {
        "artifact_rejections_without_activation": nodes,
        "bad_update_rollbacks": nodes,
        "bad_update_safe_outcomes": nodes * 2,
        "crash_recoveries": nodes,
        "cross_node_claims_accepted": 0,
        "duplicate_mutations": 0,
        "reconnect_recoveries": nodes,
        "stale_results_accepted": 0,
    }
    return (
        report.get("schema_version") == 1
        and report.get("evidence_kind") == "simulated"
        and report.get("environment") == "deterministic-in-memory-transport"
        and report.get("physical_sparks_exercised") is False
        and report.get("faults") == expected_faults
        and report.get("invariants") == expected_invariants
    )


def _observe_transition_sequence(
    evidence: dict[str, Any], history: tuple[str, ...]
) -> None:
    observed = evidence["transition_sequence"]
    if not isinstance(observed, list):
        raise SimulationError("simulated transition evidence is invalid")
    if not observed:
        observed.extend(history)
    elif observed != list(history):
        mismatches = evidence["transition_sequence_mismatches"]
        if not isinstance(mismatches, int) or isinstance(mismatches, bool):
            raise SimulationError("simulated transition mismatch count is invalid")
        evidence["transition_sequence_mismatches"] = mismatches + 1


def _context(
    node_id: str, state: AgentStateStore, releases: _ReleaseHost
) -> OperationContext:
    return OperationContext(node_id, state, _Probe(), releases)


def _release_claim(
    seed: int,
    index: int,
    scenario: str,
    node_id: str,
    clock: _SeededClock,
) -> AgentClaim:
    payload = {
        "schema_version": 1,
        "target_name": f"runtime-{index}-{scenario}",
        "oci_manifest_digest": "sha256:" + _digest(seed, index, scenario, "manifest"),
        "target_digest": _digest(seed, index, scenario, "target"),
        "provenance_digest": _digest(seed, index, scenario, "provenance"),
        "adapter_id": "spark-runtime-v1",
    }
    return AgentClaim(
        schema_version=1,
        job_id=_uuid(seed, index, scenario, "job"),
        operation_id=_uuid(seed, index, scenario, "operation"),
        attempt=1,
        fence=_uuid(seed, index, scenario, "fence"),
        node_id=node_id,
        operation=AgentOperation.RELEASE_INSTALL,
        base_commit=_sha1(seed, "base-commit"),
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        deadline=clock.deadline(),
    )


def _replace_fence(claim: AgentClaim, fence: str) -> AgentClaim:
    return AgentClaim(
        schema_version=claim.schema_version,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=fence,
        node_id=claim.node_id,
        operation=claim.operation,
        base_commit=claim.base_commit,
        payload_digest=claim.payload_digest,
        payload=claim.payload,
        deadline=claim.deadline,
    )


def _result(claim: AgentClaim, state: str) -> AgentResult:
    return AgentResult(
        schema_version=claim.schema_version,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state=state,
        result={"status": "ok"},
    )


def _node_id(seed: int, index: int) -> str:
    return "spk_" + _digest(seed, index, "node")[:32]


def _uuid(*parts: object) -> str:
    return str(uuid5(NAMESPACE_URL, ":".join(str(part) for part in parts)))


def _digest(*parts: object) -> str:
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def _sha1(*parts: object) -> str:
    return hashlib.sha1(
        ":".join(str(part) for part in parts).encode(), usedforsecurity=False
    ).hexdigest()
