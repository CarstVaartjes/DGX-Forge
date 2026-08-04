"""Durable, fenced local state for one outbound agent attempt."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import stat
from datetime import UTC, datetime

from dgx_agent_protocol import (
    AgentClaim,
    AgentProgress,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)


_DATABASE_NAME = "agent-state.sqlite3"
_BUSY_TIMEOUT_MS = 5000


class AgentStateError(RuntimeError):
    """Persistent state is corrupt, unsafe, or unavailable."""


class AgentStateConflict(AgentStateError):
    """An attempt conflicts with durable local fencing state."""


@dataclass(frozen=True)
class AgentAttemptRecord:
    claim: AgentClaim
    fence: str
    state: str
    progress_sequence: int
    progress: AgentProgress | None
    result: AgentResult | None
    created_at: str
    updated_at: str
    finished_at: str | None
    canonical_claim: bytes
    canonical_progress: bytes | None
    canonical_result: bytes | None


class AgentStateStore:
    """SQLite-backed store which serializes the agent's single active attempt."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._database = self._root / _DATABASE_NAME
        _ensure_private_root(self._root)
        self._initialize()

    def begin(self, claim: AgentClaim) -> AgentAttemptRecord:
        claim_bytes = _canonical_claim(claim)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM attempts WHERE node_id = ? AND job_id = ? AND operation_id = ? AND attempt = ?",
                    (claim.node_id, claim.job_id, claim.operation_id, claim.attempt),
                ).fetchone()
                if existing is not None:
                    record = _record(existing)
                    if record.fence == claim.fence and record.canonical_claim == claim_bytes:
                        connection.commit()
                        return record
                    raise AgentStateConflict("attempt conflicts with persisted state")
                active = connection.execute(
                    "SELECT 1 FROM attempts WHERE state = 'active' LIMIT 1"
                ).fetchone()
                if active is not None:
                    raise AgentStateConflict("another attempt is active")
                now = _now()
                connection.execute(
                    """INSERT INTO attempts
                       (node_id, job_id, operation_id, attempt, fence, state, claim_json,
                        progress_sequence, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'active', ?, 0, ?, ?)""",
                    (claim.node_id, claim.job_id, claim.operation_id, claim.attempt, claim.fence, claim_bytes, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM attempts WHERE node_id = ? AND job_id = ? AND operation_id = ? AND attempt = ?",
                    (claim.node_id, claim.job_id, claim.operation_id, claim.attempt),
                ).fetchone()
                connection.commit()
                return _record(row)
        except AgentStateConflict:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state operation failed") from error

    def heartbeat(self, progress: AgentProgress) -> AgentAttemptRecord:
        progress_bytes = _canonical_progress(progress)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = _matching_row(connection, progress)
                record = _record(row)
                if record.state != "active":
                    raise AgentStateConflict("attempt is not active")
                now = _now()
                changed = connection.execute(
                    """UPDATE attempts SET progress_sequence = progress_sequence + 1,
                       progress_json = ?, updated_at = ?
                       WHERE node_id = ? AND job_id = ? AND operation_id = ? AND attempt = ?
                         AND fence = ? AND state = 'active'""",
                    (progress_bytes, now, progress.node_id, progress.job_id, progress.operation_id, progress.attempt, progress.fence),
                ).rowcount
                if changed != 1:
                    raise AgentStateConflict("attempt is no longer active")
                updated = _matching_row(connection, progress)
                connection.commit()
                return _record(updated)
        except AgentStateConflict:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state operation failed") from error

    def finish(self, result: AgentResult) -> AgentAttemptRecord:
        result_bytes = _canonical_result(result)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = _matching_row(connection, result)
                record = _record(row)
                if record.state != "active":
                    if record.canonical_result == result_bytes:
                        connection.commit()
                        return record
                    raise AgentStateConflict("terminal result conflicts with persisted state")
                now = _now()
                changed = connection.execute(
                    """UPDATE attempts SET state = ?, result_json = ?, updated_at = ?, finished_at = ?
                       WHERE node_id = ? AND job_id = ? AND operation_id = ? AND attempt = ?
                         AND fence = ? AND state = 'active'""",
                    (result.state, result_bytes, now, now, result.node_id, result.job_id, result.operation_id, result.attempt, result.fence),
                ).rowcount
                if changed != 1:
                    raise AgentStateConflict("attempt is no longer active")
                updated = _matching_row(connection, result)
                connection.commit()
                return _record(updated)
        except AgentStateConflict:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state operation failed") from error

    def recover_active(self) -> AgentAttemptRecord | None:
        try:
            with self._connection() as connection:
                rows = connection.execute("SELECT * FROM attempts WHERE state = 'active'").fetchall()
                if len(rows) > 1:
                    raise AgentStateError("state has multiple active attempts")
                return None if not rows else _record(rows[0])
        except AgentStateError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state operation failed") from error

    def _initialize(self) -> None:
        if self._database.exists() or self._database.is_symlink():
            _verify_database(self._database)
        else:
            try:
                descriptor = os.open(
                    self._database,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                os.close(descriptor)
            except FileExistsError:
                _verify_database(self._database)
            except OSError as error:
                raise AgentStateError("state database cannot be initialized") from error
        try:
            with self._connection() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS attempts (
                        node_id TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        fence TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('active', 'succeeded', 'failed', 'waiting-for-operator')),
                        claim_json BLOB NOT NULL,
                        progress_sequence INTEGER NOT NULL DEFAULT 0 CHECK(progress_sequence >= 0),
                        progress_json BLOB,
                        result_json BLOB,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT,
                        PRIMARY KEY (node_id, job_id, operation_id, attempt)
                    )"""
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON attempts(state) WHERE state = 'active'"
                )
            os.chmod(self._database, 0o600)
            _verify_database(self._database)
        except AgentStateError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state database cannot be initialized") from error

    def _connection(self) -> sqlite3.Connection:
        _verify_database_parent(self._root, self._database)
        try:
            connection = sqlite3.connect(self._database, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            return connection
        except sqlite3.Error as error:
            raise AgentStateError("state database cannot be opened") from error


def _ensure_private_root(root: Path) -> None:
    if not root.is_absolute():
        raise AgentStateError("state root must be absolute")
    current = Path("/")
    for part in root.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                # Another store created this path between lstat and mkdir; inspect it below.
                pass
            except OSError as error:
                raise AgentStateError("state root cannot be created") from error
            metadata = os.lstat(current)
        except OSError as error:
            raise AgentStateError("state root is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AgentStateError("state root must not traverse symlinks")
    mode = stat.S_IMODE(os.stat(root, follow_symlinks=False).st_mode)
    if mode & 0o077:
        raise AgentStateError("state root permissions are too permissive")


def _verify_database_parent(root: Path, database: Path) -> None:
    metadata = os.lstat(root)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentStateError("state root is unsafe")
    if database.exists() or database.is_symlink():
        _verify_database(database)


def _verify_database(database: Path) -> None:
    try:
        metadata = os.lstat(database)
    except OSError as error:
        raise AgentStateError("state database is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AgentStateError("state database is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AgentStateError("state database permissions are too permissive")


def _matching_row(connection: sqlite3.Connection, message: AgentProgress | AgentResult) -> sqlite3.Row:
    row = connection.execute(
        """SELECT * FROM attempts WHERE node_id = ? AND job_id = ? AND operation_id = ?
           AND attempt = ? AND fence = ?""",
        (message.node_id, message.job_id, message.operation_id, message.attempt, message.fence),
    ).fetchone()
    if row is None:
        raise AgentStateConflict("attempt identity or fence does not match")
    return row


def _record(row: sqlite3.Row) -> AgentAttemptRecord:
    try:
        claim_bytes = bytes(row["claim_json"])
        claim = AgentClaim.parse(json.loads(claim_bytes))
        if canonical_message(claim) != claim_bytes:
            raise AgentStateError("stored claim is not canonical")
        progress_bytes = None if row["progress_json"] is None else bytes(row["progress_json"])
        progress = None if progress_bytes is None else AgentProgress.parse(json.loads(progress_bytes))
        if progress is not None and canonical_message(progress) != progress_bytes:
            raise AgentStateError("stored progress is not canonical")
        result_bytes = None if row["result_json"] is None else bytes(row["result_json"])
        result = None if result_bytes is None else AgentResult.parse(json.loads(result_bytes))
        if result is not None and canonical_message(result) != result_bytes:
            raise AgentStateError("stored result is not canonical")
        if row["state"] == "active" and result is not None:
            raise AgentStateError("active attempt has terminal result")
        if row["state"] != "active" and (result is None or result.state != row["state"]):
            raise AgentStateError("terminal attempt is invalid")
        if not _matches_row(claim, row):
            raise AgentStateError("stored claim identity is invalid")
        if progress is not None and not _matches_row(progress, row):
            raise AgentStateError("stored progress identity is invalid")
        if result is not None and not _matches_row(result, row):
            raise AgentStateError("stored result identity is invalid")
        return AgentAttemptRecord(
            claim=claim,
            fence=row["fence"],
            state=row["state"],
            progress_sequence=row["progress_sequence"],
            progress=progress,
            result=result,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
            canonical_claim=claim_bytes,
            canonical_progress=progress_bytes,
            canonical_result=result_bytes,
        )
    except (AgentProtocolError, ValueError, TypeError, KeyError, UnicodeDecodeError) as error:
        raise AgentStateError("stored state is invalid") from error


def _matches_row(message: AgentClaim | AgentProgress | AgentResult, row: sqlite3.Row) -> bool:
    return (
        message.node_id == row["node_id"]
        and message.job_id == row["job_id"]
        and message.operation_id == row["operation_id"]
        and message.attempt == row["attempt"]
        and message.fence == row["fence"]
    )


def _canonical_claim(claim: AgentClaim) -> bytes:
    if not isinstance(claim, AgentClaim):
        raise AgentStateError("claim is invalid")
    return canonical_message(claim)


def _canonical_progress(progress: AgentProgress) -> bytes:
    if not isinstance(progress, AgentProgress):
        raise AgentStateError("progress is invalid")
    return canonical_message(progress)


def _canonical_result(result: AgentResult) -> bytes:
    if not isinstance(result, AgentResult):
        raise AgentStateError("result is invalid")
    return canonical_message(result)


def _now() -> str:
    return datetime.now(UTC).isoformat()
