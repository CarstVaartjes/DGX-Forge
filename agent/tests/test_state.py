from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dgx_agent import state as state_module
from dgx_agent.state import AgentStateConflict, AgentStateError, AgentStateStore
from dgx_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProgress,
    AgentResult,
    canonical_message,
)

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


def progress(
    *,
    fence: str = FENCE_A,
    value: int = 1,
    job_id: str = JOB_ID,
    operation_id: str = OPERATION_ID,
    attempt: int = 1,
    node_id: str = NODE_ID,
    schema_version: int = 1,
    deadline: datetime = DEADLINE,
) -> AgentProgress:
    return AgentProgress(
        schema_version=schema_version,
        job_id=job_id,
        operation_id=operation_id,
        attempt=attempt,
        fence=fence,
        node_id=node_id,
        deadline=deadline,
        progress={"completed": value},
    )


def result(*, fence: str = FENCE_A, value: str = "ok", job_id: str = JOB_ID, attempt: int = 1, deadline: datetime = DEADLINE) -> AgentResult:
    return AgentResult(
        schema_version=1,
        job_id=job_id,
        operation_id=OPERATION_ID,
        attempt=attempt,
        fence=fence,
        node_id=NODE_ID,
        deadline=deadline,
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


def test_terminal_result_is_recovered_and_blocks_new_work_until_exact_acknowledgment(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = AgentStateStore(root)
    terminal = result()
    store.begin(claim())
    finished = store.finish(terminal)

    reopened = AgentStateStore(root)
    assert reopened.recover_active() is None
    assert reopened.recover_pending() == finished
    with pytest.raises(AgentStateConflict):
        reopened.begin(
            claim(
                job_id="33333333-3333-4333-8333-333333333333",
                operation_id="44444444-4444-4444-8444-444444444444",
                fence=FENCE_B,
            )
        )
    with pytest.raises(AgentStateConflict):
        reopened.acknowledge(result(value="wrong"))

    reopened.acknowledge(terminal)
    assert AgentStateStore(root).recover_pending() is None
    assert AgentStateStore(root).acknowledge(terminal).acknowledged_at is not None
    assert AgentStateStore(root).begin(
        claim(
            job_id="33333333-3333-4333-8333-333333333333",
            operation_id="44444444-4444-4444-8444-444444444444",
            fence=FENCE_B,
        )
    ).state == "active"


def test_fence_never_reuses_and_attempts_only_increase_after_terminal_ack(tmp_path: Path) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim(attempt=2))
    store.finish(result(attempt=2))
    store.acknowledge(result(attempt=2))

    with pytest.raises(AgentStateConflict):
        store.begin(claim(attempt=1, fence=FENCE_B))
    with pytest.raises(AgentStateConflict):
        store.begin(
            claim(
                job_id="33333333-3333-4333-8333-333333333333",
                operation_id="44444444-4444-4444-8444-444444444444",
                fence=FENCE_A,
            )
        )


@pytest.mark.parametrize(
    "statement",
    [
        "PRAGMA user_version = 2",
        "UPDATE attempts SET finished_at = '2030-01-01T00:00:00+00:00'",
        "UPDATE attempts SET progress_sequence = 1, progress_json = NULL",
        "UPDATE attempts SET created_at = 'not-a-timestamp'",
        "UPDATE attempts SET updated_at = '2020-01-01T00:00:00+00:00'",
    ],
)
def test_schema_and_record_corruption_fail_closed(tmp_path: Path, statement: str) -> None:
    root = tmp_path / "state"
    store = AgentStateStore(root)
    store.begin(claim())
    with sqlite3.connect(_database(root)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(statement)

    with pytest.raises(AgentStateError):
        AgentStateStore(root).recover_active()


def test_state_files_are_private_while_wal_is_live(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = AgentStateStore(root)
    connection = store._connection()
    try:
        paths = [_database(root), root / "agent-state.sqlite3-wal", root / "agent-state.sqlite3-shm"]
        for path in paths:
            assert path.exists()
            assert path.stat().st_uid == os.geteuid()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        connection.close()


def test_repeated_fresh_root_initialization_is_race_free(tmp_path: Path) -> None:
    for iteration in range(12):
        root = tmp_path / f"state-{iteration}"
        barrier = threading.Barrier(8)

        def initialize(
            _: int,
            *,
            _barrier: threading.Barrier = barrier,
            _root: Path = root,
        ) -> None:
            _barrier.wait()
            AgentStateStore(_root)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(initialize, range(8)))
        assert AgentStateStore(root).recover_active() is None


def test_fresh_root_initialization_is_race_free_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "process-state"
    script = """
from concurrent.futures import ProcessPoolExecutor
import os
from pathlib import Path
from dgx_agent.state import AgentStateStore

def initialize(_):
    AgentStateStore(Path(os.environ['AGENT_STATE_TEST_ROOT']))

if __name__ == '__main__':
    with ProcessPoolExecutor(max_workers=6) as pool:
        list(pool.map(initialize, range(18)))
"""
    environment = os.environ.copy()
    environment["AGENT_STATE_TEST_ROOT"] = str(root)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert AgentStateStore(root).recover_active() is None


def test_state_rejects_writable_nonsticky_ancestor(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe-parent"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(AgentStateError):
        AgentStateStore(unsafe / "state")


def test_state_rejects_broad_root_and_fifo_sidecar_without_blocking(tmp_path: Path) -> None:
    with pytest.raises(AgentStateError):
        AgentStateStore(Path("/"))

    root = tmp_path / "state"
    AgentStateStore(root)
    wal = root / "agent-state.sqlite3-wal"
    if wal.exists():
        wal.unlink()
    os.mkfifo(wal)
    with pytest.raises(AgentStateError):
        AgentStateStore(root)


def test_constraintless_versioned_schema_is_rejected_with_typed_error(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    database = _database(root)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE attempts(node_id,job_id,operation_id,attempt,fence,state,claim_json,progress_sequence,progress_json,result_json,created_at,updated_at,finished_at,acknowledged_at)")
        connection.execute("PRAGMA user_version=1")
    database.chmod(0o600)

    with pytest.raises(AgentStateError):
        AgentStateStore(root)


@pytest.mark.parametrize("object_sql", ["CREATE VIEW attempts AS SELECT 1 AS node_id", "CREATE TABLE unrelated(value)"])
def test_malformed_versioned_schema_objects_raise_typed_error(tmp_path: Path, object_sql: str) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    database = _database(root)
    with sqlite3.connect(database) as connection:
        connection.execute(object_sql)
        connection.execute("PRAGMA user_version=1")
    database.chmod(0o600)
    with pytest.raises(AgentStateError):
        AgentStateStore(root)


def test_schema_comment_cannot_spoof_required_constraints(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    database = _database(root)
    columns = "node_id,job_id,operation_id,attempt,fence,state,claim_json,progress_sequence,progress_json,result_json,created_at,updated_at,finished_at,acknowledged_at"
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE attempts({columns} /* fence TEXT NOT NULL UNIQUE CHECK((progress_sequence=0 CHECK((state='active' */)")
        connection.execute("CREATE UNIQUE INDEX one_unresolved ON attempts(state) WHERE state='active' OR acknowledged_at IS NULL")
        connection.execute("PRAGMA user_version=1")
    database.chmod(0o600)
    with pytest.raises(AgentStateError):
        AgentStateStore(root)


def test_schema_missing_progress_nonnegative_check_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    database = _database(root)
    table = """CREATE TABLE attempts(
        node_id TEXT NOT NULL, job_id TEXT NOT NULL, operation_id TEXT NOT NULL,
        attempt INTEGER NOT NULL, fence TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK(state IN ('active','succeeded','failed','waiting-for-operator')),
        claim_json BLOB NOT NULL, progress_sequence INTEGER NOT NULL,
        progress_json BLOB, result_json BLOB, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, finished_at TEXT, acknowledged_at TEXT,
        PRIMARY KEY(node_id,job_id,operation_id,attempt),
        CHECK((progress_sequence=0 AND progress_json IS NULL) OR (progress_sequence>0 AND progress_json IS NOT NULL)),
        CHECK((state='active' AND result_json IS NULL AND finished_at IS NULL AND acknowledged_at IS NULL)
           OR (state!='active' AND result_json IS NOT NULL AND finished_at IS NOT NULL)))"""
    with sqlite3.connect(database) as connection:
        connection.execute(table)
        connection.execute("CREATE UNIQUE INDEX one_unresolved ON attempts((1)) WHERE state='active' OR acknowledged_at IS NULL")
        connection.execute("PRAGMA user_version=1")
    database.chmod(0o600)
    with pytest.raises(AgentStateError):
        AgentStateStore(root)


def test_heartbeat_accepts_and_persists_nondecreasing_deadline_extensions(
    tmp_path: Path,
) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())
    first_deadline = DEADLINE + timedelta(seconds=30)
    second_deadline = first_deadline + timedelta(seconds=30)

    first = store.heartbeat(progress(value=1, deadline=first_deadline))
    second_message = progress(value=2, deadline=second_deadline)
    second = AgentStateStore(tmp_path / "state").heartbeat(second_message)

    assert first.progress is not None and first.progress.deadline == first_deadline
    assert second.progress == second_message
    assert second.canonical_progress == canonical_message(second_message)
    assert AgentStateStore(tmp_path / "state").recover_active() == second


@pytest.mark.parametrize(
    "message",
    [
        progress(deadline=DEADLINE - timedelta(seconds=1)),
        progress(fence=FENCE_B, deadline=DEADLINE + timedelta(seconds=30)),
        progress(
            job_id="33333333-3333-4333-8333-333333333333",
            deadline=DEADLINE + timedelta(seconds=30),
        ),
        progress(attempt=2, deadline=DEADLINE + timedelta(seconds=30)),
        progress(
            operation_id="44444444-4444-4444-8444-444444444444",
            deadline=DEADLINE + timedelta(seconds=30),
        ),
        progress(
            node_id="spk_ffffffffffffffffffffffffffffffff",
            deadline=DEADLINE + timedelta(seconds=30),
        ),
    ],
)
def test_heartbeat_rejects_deadline_rollback_or_protocol_identity_change(
    tmp_path: Path, message: AgentProgress
) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())

    with pytest.raises(AgentStateConflict):
        store.heartbeat(message)
    assert store.recover_active().progress_sequence == 0


def test_heartbeat_rejects_deadline_rollback_relative_to_latest_progress(
    tmp_path: Path,
) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())
    latest = DEADLINE + timedelta(seconds=60)
    store.heartbeat(progress(deadline=latest))

    with pytest.raises(AgentStateConflict):
        store.heartbeat(progress(deadline=latest - timedelta(seconds=1)))
    assert store.recover_active().progress_sequence == 1


def test_result_deadline_still_must_match_original_claim(tmp_path: Path) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())
    store.heartbeat(progress(deadline=DEADLINE + timedelta(seconds=30)))

    with pytest.raises(AgentStateConflict):
        store.finish(result(deadline=DEADLINE + timedelta(seconds=30)))
    finished = store.finish(result())
    assert finished.result == result()


@pytest.mark.parametrize("method", ["heartbeat", "finish", "acknowledge"])
def test_backward_clock_never_commits_corrupt_timestamps(tmp_path: Path, monkeypatch, method: str) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())
    if method == "acknowledge":
        store.finish(result())
    monkeypatch.setattr("dgx_agent.state._now", lambda: "2020-01-01T00:00:00+00:00")
    if method == "heartbeat":
        store.heartbeat(progress())
    elif method == "finish":
        store.finish(result())
    else:
        store.acknowledge(result())
    reopened = AgentStateStore(tmp_path / "state")
    record = reopened.recover_active() if method == "heartbeat" else reopened.recover_pending()
    if method == "acknowledge":
        assert reopened.recover_active() is None
        assert reopened.recover_pending() is None
        with sqlite3.connect(_database(tmp_path / "state")) as connection:
            created_at, updated_at = connection.execute("SELECT created_at, updated_at FROM attempts").fetchone()
    else:
        assert record is not None
        created_at, updated_at = record.created_at, record.updated_at
    assert datetime.fromisoformat(updated_at) >= datetime.fromisoformat(created_at)


def test_sqlite_open_is_anchored_against_path_substitution(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    store = AgentStateStore(root)
    store.begin(claim())
    original = tmp_path / "original-state"
    real_connect = sqlite3.connect
    swapped = False

    def swapping_connect(database, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(original)
            root.mkdir(mode=0o700)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr("dgx_agent.state.sqlite3.connect", swapping_connect)
    assert store.recover_active().claim == claim()


def test_initialization_lock_has_deterministic_bounded_deadline(monkeypatch) -> None:
    attempts: list[int] = []
    waits: list[float] = []
    clock = iter([0.0, 1.0, 2.0, 5.0])

    def unavailable_lock(_descriptor: int, operation: int) -> None:
        attempts.append(operation)
        raise BlockingIOError(errno.EAGAIN, "busy")

    monkeypatch.setattr(state_module.fcntl, "flock", unavailable_lock)
    with pytest.raises(AgentStateError):
        state_module._acquire_initialization_lock(
            123,
            monotonic=lambda: next(clock),
            wait=waits.append,
        )

    assert len(attempts) == 2
    assert all(operation == state_module.fcntl.LOCK_EX | state_module.fcntl.LOCK_NB for operation in attempts)
    assert waits and all(0 < delay <= 0.05 for delay in waits)


def test_initialization_lock_retries_interrupted_system_call(monkeypatch) -> None:
    calls = 0

    def interrupted_once(_descriptor: int, _operation: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError(errno.EINTR, "interrupted")

    monkeypatch.setattr(state_module.fcntl, "flock", interrupted_once)
    state_module._acquire_initialization_lock(123, monotonic=lambda: 0.0, wait=lambda _delay: None)
    assert calls == 2


def test_repeated_lock_interruptions_remain_deadline_bounded(monkeypatch) -> None:
    attempts = 0
    waits: list[float] = []
    now = 0.0

    def advancing_clock() -> float:
        nonlocal now
        current = now
        now += 0.01
        return current

    def always_interrupted(_descriptor: int, _operation: int) -> None:
        nonlocal attempts
        attempts += 1
        raise InterruptedError(errno.EINTR, "interrupted")

    monkeypatch.setattr(state_module.fcntl, "flock", always_interrupted)
    monkeypatch.setattr(state_module, "_LOCK_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(AgentStateError):
        state_module._acquire_initialization_lock(
            123,
            monotonic=advancing_clock,
            wait=waits.append,
        )

    assert waits
    assert attempts <= 6
    assert all(0 < delay <= 0.05 for delay in waits)


def test_initialization_lock_does_not_retry_after_wait_overshoots_deadline(monkeypatch) -> None:
    attempts = 0
    clock = iter([0.0, 1.0, 5.0])

    def available_after_first_attempt(_descriptor: int, _operation: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BlockingIOError(errno.EAGAIN, "busy")

    monkeypatch.setattr(state_module.fcntl, "flock", available_after_first_attempt)
    with pytest.raises(AgentStateError):
        state_module._acquire_initialization_lock(
            123,
            monotonic=lambda: next(clock),
            wait=lambda _delay: None,
        )

    assert attempts == 1


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TRIGGER destroy_after_insert AFTER INSERT ON attempts BEGIN DELETE FROM attempts; END",
        "CREATE TRIGGER destroy_after_delete AFTER DELETE ON attempts BEGIN DELETE FROM attempts; END",
        "CREATE VIEW leaked_attempts AS SELECT * FROM attempts",
        "CREATE TABLE extra_state(value TEXT)",
        "CREATE INDEX extra_attempt_index ON attempts(job_id)",
    ],
)
def test_unexpected_schema_objects_are_rejected(tmp_path: Path, statement: str) -> None:
    root = tmp_path / "state"
    AgentStateStore(root)
    with sqlite3.connect(_database(root)) as connection:
        connection.execute(statement)

    with pytest.raises(AgentStateError):
        AgentStateStore(root)
