from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from types import SimpleNamespace

import pytest
import time

from dgx_agent_protocol import AgentClaim, AgentOperation, AgentProtocolError, canonical_message

from dgx_agent.operations import (
    InspectionDisposition,
    OperationContext,
    OperationRegistry,
    UnsupportedOperation,
)
from dgx_agent.state import AgentStateConflict, AgentStateStore


NODE_ID = "spk_0123456789abcdef0123456789abcdef"


def claim(
    *,
    operation: AgentOperation = AgentOperation.NODE_PROBE,
    payload=None,
    node_id: str = NODE_ID,
    deadline: datetime | None = None,
    job_id: str = "11111111-1111-4111-8111-111111111111",
    operation_id: str = "22222222-2222-4222-8222-222222222222",
    fence: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
) -> AgentClaim:
    body = {} if payload is None else payload
    return AgentClaim(
        schema_version=1,
        job_id=job_id,
        operation_id=operation_id,
        attempt=1,
        fence=fence,
        node_id=node_id,
        operation=operation,
        base_commit="a" * 40,
        payload_digest=hashlib.sha256(canonical_message(body)).hexdigest(),
        payload=body,
        deadline=deadline or datetime.now(UTC) + timedelta(minutes=1),
    )


class NeverProbe:
    def collect(self, deadline):
        raise AssertionError("closed registry reached the probe")


class RecordingProbe:
    def __init__(self, evidence=None, error: Exception | None = None) -> None:
        self.evidence = evidence or {"platform": {"status": "ok"}}
        self.error = error
        self.deadlines: list[datetime] = []

    def collect(self, deadline):
        self.deadlines.append(deadline)
        if self.error is not None:
            raise self.error
        return self.evidence


def context(tmp_path) -> OperationContext:
    return OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=NeverProbe(),
    )


def test_unknown_duck_typed_or_known_unimplemented_operation_never_dispatches(tmp_path) -> None:
    registry = OperationRegistry()
    operation_context = context(tmp_path)

    with pytest.raises(UnsupportedOperation):
        registry.execute(
            SimpleNamespace(operation="system.exec", payload={}),  # type: ignore[arg-type]
            operation_context,
        )
    with pytest.raises(UnsupportedOperation):
        registry.execute(
            claim(operation=AgentOperation.RELEASE_INSTALL),
            operation_context,
        )


def test_node_probe_rejects_every_nonempty_payload_before_dispatch(tmp_path) -> None:
    with pytest.raises(AgentProtocolError):
        OperationRegistry().execute(
            claim(payload={"selector": "device_identity"}),
            context(tmp_path),
        )


def test_wrong_node_and_expired_claim_fail_before_state_or_probe(tmp_path) -> None:
    probe = RecordingProbe()
    store = AgentStateStore(tmp_path / "state")
    operation_context = OperationContext(node_id=NODE_ID, state=store, probe=probe)

    with pytest.raises(AgentProtocolError, match="node"):
        OperationRegistry().execute(
            claim(node_id="spk_ffffffffffffffffffffffffffffffff"), operation_context
        )
    with pytest.raises(AgentProtocolError, match="deadline"):
        OperationRegistry().execute(
            claim(deadline=datetime.now(UTC) - timedelta(seconds=1)), operation_context
        )

    assert probe.deadlines == []
    assert store.recover_active() is None


def test_success_is_canonical_persisted_and_replayed_without_recollection(tmp_path) -> None:
    probe = RecordingProbe({"z": 2, "a": {"status": "ok"}})
    store = AgentStateStore(tmp_path / "state")
    operation_context = OperationContext(node_id=NODE_ID, state=store, probe=probe)
    request = claim()

    first = OperationRegistry().execute(request, operation_context)
    second = OperationRegistry().execute(request, operation_context)

    assert first.result.state == "succeeded"
    assert first.result.result == {"status": "ok", "evidence": {"a": {"status": "ok"}, "z": 2}}
    assert first.canonical_result == canonical_message(first.result)
    assert first.replayed is False
    assert second.result == first.result
    assert second.canonical_result == first.canonical_result
    assert second.replayed is True
    assert len(probe.deadlines) == 1


def test_terminal_result_replays_after_deadline_without_local_execution(tmp_path) -> None:
    probe = RecordingProbe()
    operation_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=probe,
    )
    request = claim(deadline=datetime.now(UTC) + timedelta(milliseconds=30))
    first = OperationRegistry().execute(request, operation_context)
    time.sleep(0.04)

    replay = OperationRegistry().execute(request, operation_context)

    assert replay.result == first.result
    assert replay.replayed is True
    assert len(probe.deadlines) == 1

def test_acknowledged_terminal_result_inspects_and_replays_after_restart_and_expiry(
    tmp_path,
) -> None:
    state_root = tmp_path / "state"
    probe = RecordingProbe()
    request = claim(deadline=datetime.now(UTC) + timedelta(milliseconds=30))
    first_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(state_root),
        probe=probe,
    )
    first = OperationRegistry().execute(request, first_context)
    first_context.state.acknowledge(first.result)
    time.sleep(0.04)
    restarted = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(state_root),
        probe=NeverProbe(),
    )

    inspection = OperationRegistry().inspect(request, restarted)
    replay = OperationRegistry().execute(request, restarted)

    assert inspection.disposition is InspectionDisposition.COMPLETED
    assert inspection.result == first.result
    assert inspection.canonical_result == first.canonical_result
    assert replay.result == first.result
    assert replay.canonical_result == first.canonical_result
    assert replay.replayed is True
    assert len(probe.deadlines) == 1

    changed = claim(deadline=datetime.now(UTC) + timedelta(minutes=1))
    with pytest.raises(AgentStateConflict):
        OperationRegistry().inspect(changed, restarted)
    with pytest.raises(AgentStateConflict):
        OperationRegistry().execute(changed, restarted)


def test_inspection_is_read_only_for_interrupted_and_completed_probe(tmp_path) -> None:
    request = claim()
    probe = RecordingProbe()
    store = AgentStateStore(tmp_path / "state")
    store.begin(request)
    operation_context = OperationContext(node_id=NODE_ID, state=store, probe=probe)
    registry = OperationRegistry()

    interrupted = registry.inspect(request, operation_context)
    assert interrupted.disposition is InspectionDisposition.SAFE_TO_RETRY
    assert interrupted.result is None
    assert probe.deadlines == []

    execution = registry.execute(request, operation_context)
    completed = registry.inspect(request, operation_context)
    assert completed.disposition is InspectionDisposition.COMPLETED
    assert completed.result == execution.result
    assert completed.canonical_result == execution.canonical_result
    assert len(probe.deadlines) == 1


def test_inspection_and_execution_reject_a_different_unresolved_claim(tmp_path) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())
    other = claim(
        job_id="33333333-3333-4333-8333-333333333333",
        operation_id="44444444-4444-4444-8444-444444444444",
        fence="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    probe = RecordingProbe()
    operation_context = OperationContext(node_id=NODE_ID, state=store, probe=probe)

    with pytest.raises(AgentStateConflict):
        OperationRegistry().inspect(other, operation_context)
    with pytest.raises(AgentStateConflict):
        OperationRegistry().execute(other, operation_context)
    assert probe.deadlines == []


def test_probe_exception_persists_only_stable_redacted_failure(tmp_path) -> None:
    sentinel = "secret-token-in-/tmp/private.log"
    probe = RecordingProbe(error=RuntimeError(sentinel))
    operation_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=probe,
    )

    execution = OperationRegistry().execute(claim(), operation_context)

    assert execution.result.state == "failed"
    assert execution.result.result == {
        "status": "failed",
        "error_code": "probe_failed",
    }
    assert sentinel.encode() not in execution.canonical_result
    assert len(probe.deadlines) == 1


def test_unrecognized_exception_error_code_cannot_enter_persisted_result(tmp_path) -> None:
    class MaliciousError(RuntimeError):
        error_code = "secret_selected_error_code"

    operation_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=RecordingProbe(error=MaliciousError("raw secret /tmp/value")),
    )

    execution = OperationRegistry().execute(claim(), operation_context)

    assert execution.result.result == {"status": "failed", "error_code": "probe_failed"}
    assert b"secret" not in execution.canonical_result
