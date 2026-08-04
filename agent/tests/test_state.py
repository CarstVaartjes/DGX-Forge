from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading

import pytest

from dgx_agent_protocol import AgentClaim, AgentOperation, AgentProgress, AgentResult, canonical_message

from dgx_agent.state import AgentStateConflict, AgentStateError, AgentStateStore


NODE_ID = "spk_0123456789abcdef0123456789abcdef"
JOB_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
FENCE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FENCE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)


def claim(
    *,
    fence: str = FENCE_A,
    job_id: str = JOB_ID,
    operation_id: str = OPERATION_ID,
    attempt: int = 1,
    payload: dict[str, object] | None = None,
) -> AgentClaim:
    payload = {} if payload is None else payload
    return AgentClaim(
        schema_version=1,
        job_id=job_id,
        operation_id=operation_id,
        attempt=attempt,
        fence=fence,
        node_id=NODE_ID,
        operation=AgentOperation.NODE_PROBE,
        base_commit="c" * 40,
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        deadline=DEADLINE,
    )


def progress(*, fence: str = FENCE_A, value: int = 1, job_id: str = JOB_ID) -> AgentProgress:
    return AgentProgress(
        schema_version=1,
        job_id=job_id,
        operation_id=OPERATION_ID,
        attempt=1,
        fence=fence,
        node_id=NODE_ID,
        deadline=DEADLINE,
        progress={"completed": value},
    )


def result(*, fence: str = FENCE_A, value: str = "ok", job_id: str = JOB_ID) -> AgentResult:
    return AgentResult(
        schema_version=1,
        job_id=job_id,
        operation_id=OPERATION_ID,
        attempt=1,
        fence=fence,
        node_id=NODE_ID,
        deadline=DEADLINE,
        state="succeeded",
        result={"outcome": value},
    )


def _database(root: Path) -> Path:
    return root / "agent-state.sqlite3"


def test_begin_persists_canonical_claim_and_returns_immutable_record(tmp_path: Path) -> None:
    root = tmp_path / "state"
    expected = claim()

    record = AgentStateStore(root).begin(expected)

    assert record.claim == expected
    assert record.fence == FENCE_A
    assert record.state == "active"
    assert record.progress_sequence == 0
    assert record.progress is None
    assert record.result is None
    assert record.created_at.endswith("+00:00")
    with pytest.raises(AttributeError):
        record.state = "succeeded"  # type: ignore[misc]
    with sqlite3.connect(_database(root)) as connection:
        stored = connection.execute("SELECT claim_json FROM attempts").fetchone()[0]
    assert stored == canonical_message(expected)


def test_state_survives_restart_and_rejects_new_fence(tmp_path: Path) -> None:
    root = tmp_path / "state"
    first = AgentStateStore(root).begin(claim(fence=FENCE_A))
    reopened = AgentStateStore(root)

    assert reopened.recover_active() == first
    with pytest.raises(AgentStateConflict):
        reopened.begin(claim(fence=FENCE_B))


def test_exact_begin_replay_is_idempotent_but_changed_claim_conflicts(tmp_path: Path) -> None:
    store = AgentStateStore(tmp_path / "state")
    original = claim()
    first = store.begin(original)

    assert store.begin(original) == first
    changed = claim(payload={"mode": "full"})
    with pytest.raises(AgentStateConflict):
        store.begin(changed)


def test_database_enforces_only_one_active_attempt_across_store_instances(tmp_path: Path) -> None:
    root = tmp_path / "state"
    AgentStateStore(root).begin(claim())
    second = claim(
        job_id="33333333-3333-4333-8333-333333333333",
        operation_id="44444444-4444-4444-8444-444444444444",
        fence=FENCE_B,
    )

    with pytest.raises(AgentStateConflict):
        AgentStateStore(root).begin(second)


def test_concurrent_begin_cannot_create_two_active_attempts(tmp_path: Path) -> None:
    root = tmp_path / "state"
    claims = [
        claim(),
        claim(
            job_id="33333333-3333-4333-8333-333333333333",
            operation_id="44444444-4444-4444-8444-444444444444",
            fence=FENCE_B,
        ),
    ]
    barrier = threading.Barrier(2)

    def start(item: AgentClaim):
        barrier.wait()
        try:
            return AgentStateStore(root).begin(item)
        except AgentStateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(start, claims))

    assert sum(record is not None for record in records) == 1
    assert AgentStateStore(root).recover_active() is not None


def test_heartbeat_matches_identity_and_sequence_is_monotonic_across_reopen(tmp_path: Path) -> None:
    root = tmp_path / "state"
    AgentStateStore(root).begin(claim())

    first = AgentStateStore(root).heartbeat(progress(value=10))
    second = AgentStateStore(root).heartbeat(progress(value=20))

    assert first.progress_sequence == 1
    assert second.progress_sequence == 2
    assert second.progress == progress(value=20)
    assert AgentStateStore(root).recover_active() == second


def test_heartbeat_persists_canonical_progress_bytes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    AgentStateStore(root).begin(claim())
    message = progress(value=10)

    AgentStateStore(root).heartbeat(message)

    with sqlite3.connect(_database(root)) as connection:
        stored = connection.execute("SELECT progress_json FROM attempts").fetchone()[0]
    assert stored == canonical_message(message)


@pytest.mark.parametrize(
    "message",
    [progress(fence=FENCE_B), progress(job_id="33333333-3333-4333-8333-333333333333")],
)
def test_heartbeat_rejects_mismatched_fence_or_identity(tmp_path: Path, message: AgentProgress) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())

    with pytest.raises(AgentStateConflict):
        store.heartbeat(message)


def test_concurrent_heartbeats_get_database_owned_sequences(tmp_path: Path) -> None:
    root = tmp_path / "state"
    AgentStateStore(root).begin(claim())
    barrier = threading.Barrier(4)

    def beat(value: int) -> int:
        barrier.wait()
        return AgentStateStore(root).heartbeat(progress(value=value)).progress_sequence

    with ThreadPoolExecutor(max_workers=4) as pool:
        sequences = list(pool.map(beat, range(4)))

    assert sorted(sequences) == [1, 2, 3, 4]
    assert AgentStateStore(root).recover_active().progress_sequence == 4


def test_finish_is_atomic_and_identical_terminal_replay_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = AgentStateStore(root)
    store.begin(claim())
    terminal = result()

    finished = store.finish(terminal)

    assert finished.state == "succeeded"
    assert finished.result == terminal
    assert finished.finished_at is not None
    assert store.recover_active() is None
    assert AgentStateStore(root).finish(terminal) == finished
    assert AgentStateStore(root).begin(claim()) == finished
    with sqlite3.connect(_database(root)) as connection:
        stored = connection.execute("SELECT result_json FROM attempts").fetchone()[0]
    assert stored == canonical_message(terminal)


def test_finish_rejects_mismatch_and_conflicting_terminal_result(tmp_path: Path) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())

    with pytest.raises(AgentStateConflict):
        store.finish(result(fence=FENCE_B))
    store.finish(result())
    with pytest.raises(AgentStateConflict):
        store.finish(result(value="changed"))


def test_completed_attempt_rejects_changed_fence_instead_of_rerunning(tmp_path: Path) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())
    store.finish(result())

    with pytest.raises(AgentStateConflict):
        store.begin(claim(fence=FENCE_B))


def test_rolled_back_transaction_never_appears_after_reopen(tmp_path: Path) -> None:
    root = tmp_path / "state"
    AgentStateStore(root)
    raw_claim = canonical_message(claim())
    connection = sqlite3.connect(_database(root), isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """INSERT INTO attempts
           (node_id, job_id, operation_id, attempt, fence, state, claim_json,
            progress_sequence, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, 0, ?, ?)""",
        (NODE_ID, JOB_ID, OPERATION_ID, 1, FENCE_A, raw_claim, "2030-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00"),
    )
    connection.rollback()
    connection.close()

    assert AgentStateStore(root).recover_active() is None


def test_corrupt_or_noncanonical_rows_fail_closed_without_echoing_contents(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = AgentStateStore(root)
    store.begin(claim())
    sentinel = b'{"malformed":"sensitive-value"}'
    with sqlite3.connect(_database(root)) as connection:
        connection.execute("UPDATE attempts SET claim_json = ?", (sentinel,))

    with pytest.raises(AgentStateError) as caught:
        AgentStateStore(root).recover_active()

    assert "sensitive-value" not in str(caught.value)


def test_state_root_database_permissions_and_pragmas_are_restrictive(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = AgentStateStore(root)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(_database(root).stat().st_mode) == 0o600
    with store._connection() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_state_rejects_permissive_or_symlinked_roots_and_database(tmp_path: Path) -> None:
    permissive = tmp_path / "permissive"
    permissive.mkdir(mode=0o755)
    with pytest.raises(AgentStateError):
        AgentStateStore(permissive)

    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(AgentStateError):
        AgentStateStore(linked)

    parent_actual = tmp_path / "parent-actual"
    parent_actual.mkdir(mode=0o700)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(parent_actual, target_is_directory=True)
    with pytest.raises(AgentStateError):
        AgentStateStore(parent_link / "state")

    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "database-target"
    target.write_bytes(b"")
    _database(root).symlink_to(target)
    with pytest.raises(AgentStateError):
        AgentStateStore(root)


def test_state_schema_and_errors_contain_no_enrollment_token_or_secret_value(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = AgentStateStore(root)
    store.begin(claim())
    with sqlite3.connect(_database(root)) as connection:
        schema = "\n".join(row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))

    assert "enrollment_token" not in schema.lower()
    assert "private_key" not in schema.lower()
    with pytest.raises(AgentStateConflict) as caught:
        store.begin(claim(fence=FENCE_B))
    assert "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" not in str(caught.value)
    assert "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" not in str(caught.value)
