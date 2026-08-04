"""Fenced, crash-safe local state for the outbound agent."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dgx_agent_protocol import (
    AgentClaim,
    AgentProgress,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)

_DATABASE_NAME = "agent-state.sqlite3"
_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5000
_LOCK_TIMEOUT_SECONDS = _BUSY_TIMEOUT_MS / 1000
_LOCK_RETRY_SECONDS = 0.05
_TABLE_SQL = """CREATE TABLE attempts (
    node_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    fence TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('active', 'succeeded', 'failed', 'waiting-for-operator')),
    claim_json BLOB NOT NULL,
    progress_sequence INTEGER NOT NULL CHECK(progress_sequence >= 0),
    progress_json BLOB,
    result_json BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    acknowledged_at TEXT,
    PRIMARY KEY (node_id, job_id, operation_id, attempt),
    CHECK ((progress_sequence = 0 AND progress_json IS NULL)
        OR (progress_sequence > 0 AND progress_json IS NOT NULL)),
    CHECK ((state = 'active' AND result_json IS NULL AND finished_at IS NULL AND acknowledged_at IS NULL)
        OR (state != 'active' AND result_json IS NOT NULL AND finished_at IS NOT NULL))
)"""
_UNRESOLVED_INDEX_SQL = """CREATE UNIQUE INDEX one_unresolved_attempt
ON attempts ((1)) WHERE state = 'active' OR acknowledged_at IS NULL"""
_EXPECTED_COLUMNS = (
    ("node_id", "TEXT", 1, 1),
    ("job_id", "TEXT", 1, 2),
    ("operation_id", "TEXT", 1, 3),
    ("attempt", "INTEGER", 1, 4),
    ("fence", "TEXT", 1, 0),
    ("state", "TEXT", 1, 0),
    ("claim_json", "BLOB", 1, 0),
    ("progress_sequence", "INTEGER", 1, 0),
    ("progress_json", "BLOB", 0, 0),
    ("result_json", "BLOB", 0, 0),
    ("created_at", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
    ("finished_at", "TEXT", 0, 0),
    ("acknowledged_at", "TEXT", 0, 0),
)


class AgentStateError(RuntimeError):
    """Durable state is unavailable, unsafe, or corrupt."""


class AgentStateConflict(AgentStateError):
    """A message conflicts with persisted fencing history."""


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
    acknowledged_at: str | None
    canonical_claim: bytes
    canonical_progress: bytes | None
    canonical_result: bytes | None


class _AnchoredConnection(sqlite3.Connection):
    root_descriptor: int = -1

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self.root_descriptor >= 0:
                os.close(self.root_descriptor)
                self.root_descriptor = -1


class AgentStateStore:
    """SQLite store enforcing one unresolved fenced attempt."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._initialize()

    def begin(self, claim: AgentClaim) -> AgentAttemptRecord:
        claim_bytes = _canonical(claim, AgentClaim)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM attempts WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=?",
                _identity(claim),
            ).fetchone()
            if existing is not None:
                record = _record(existing)
                if record.fence == claim.fence and record.canonical_claim == claim_bytes:
                    connection.commit()
                    return record
                raise AgentStateConflict("attempt conflicts with persisted state")
            if connection.execute("SELECT 1 FROM attempts WHERE fence=?", (claim.fence,)).fetchone():
                raise AgentStateConflict("fence was already used")
            highest = connection.execute(
                "SELECT MAX(attempt) FROM attempts WHERE node_id=? AND job_id=? AND operation_id=?",
                _identity(claim)[:3],
            ).fetchone()[0]
            if highest is not None and claim.attempt <= highest:
                raise AgentStateConflict("attempt is stale")
            unresolved = connection.execute(
                "SELECT 1 FROM attempts WHERE state='active' OR acknowledged_at IS NULL"
            ).fetchone()
            if unresolved is not None:
                raise AgentStateConflict("an attempt delivery is unresolved")
            now = _now()
            connection.execute(
                """INSERT INTO attempts
                   (node_id,job_id,operation_id,attempt,fence,state,claim_json,
                    progress_sequence,created_at,updated_at)
                   VALUES(?,?,?,?,?,'active',?,0,?,?)""",
                (*_identity(claim), claim.fence, claim_bytes, now, now),
            )
            row = connection.execute(
                "SELECT * FROM attempts WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=?",
                _identity(claim),
            ).fetchone()
            record = _record(row)
            connection.commit()
            return record
        except AgentStateConflict:
            raise
        except sqlite3.IntegrityError as error:
            raise AgentStateConflict("attempt conflicts with persisted state") from error
        except AgentStateError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state operation failed") from error
        finally:
            connection.close()

    def heartbeat(self, progress: AgentProgress) -> AgentAttemptRecord:
        progress_bytes = _canonical(progress, AgentProgress)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = _record(_matching_row(connection, progress))
            _require_protocol_match(current.claim, progress)
            if current.state != "active":
                raise AgentStateConflict("attempt is not active")
            now = _nondecreasing_timestamp(current.updated_at)
            connection.execute(
                """UPDATE attempts SET progress_sequence=progress_sequence+1,
                   progress_json=?,updated_at=? WHERE node_id=? AND job_id=?
                   AND operation_id=? AND attempt=? AND fence=? AND state='active'""",
                (progress_bytes, now, *_identity(progress), progress.fence),
            )
            record = _record(_matching_row(connection, progress))
            connection.commit()
            return record
        except AgentStateConflict:
            raise
        except AgentStateError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state operation failed") from error
        finally:
            connection.close()

    def finish(self, result: AgentResult) -> AgentAttemptRecord:
        result_bytes = _canonical(result, AgentResult)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = _record(_matching_row(connection, result))
            _require_protocol_match(current.claim, result)
            if current.state != "active":
                if current.canonical_result == result_bytes:
                    connection.commit()
                    return current
                raise AgentStateConflict("terminal result conflicts with persisted state")
            now = _nondecreasing_timestamp(current.updated_at)
            connection.execute(
                """UPDATE attempts SET state=?,result_json=?,finished_at=?,updated_at=?
                   WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=?
                   AND fence=? AND state='active'""",
                (result.state, result_bytes, now, now, *_identity(result), result.fence),
            )
            record = _record(_matching_row(connection, result))
            connection.commit()
            return record
        except AgentStateConflict:
            raise
        except AgentStateError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state operation failed") from error
        finally:
            connection.close()

    def acknowledge(self, result: AgentResult) -> AgentAttemptRecord:
        result_bytes = _canonical(result, AgentResult)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = _record(_matching_row(connection, result))
            _require_protocol_match(current.claim, result)
            if current.state == "active" or current.canonical_result != result_bytes:
                raise AgentStateConflict("terminal result conflicts with persisted state")
            if current.acknowledged_at is None:
                now = _nondecreasing_timestamp(current.updated_at)
                connection.execute(
                    """UPDATE attempts SET acknowledged_at=?,updated_at=?
                       WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=?
                       AND fence=? AND acknowledged_at IS NULL""",
                    (now, now, *_identity(result), result.fence),
                )
                current = _record(_matching_row(connection, result))
            connection.commit()
            return current
        except AgentStateConflict:
            raise
        except AgentStateError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise AgentStateError("state operation failed") from error
        finally:
            connection.close()

    def recover_active(self) -> AgentAttemptRecord | None:
        return self._recover("state='active'")

    def recover_pending(self) -> AgentAttemptRecord | None:
        return self._recover("state!='active' AND acknowledged_at IS NULL")

    def lookup_exact(self, claim: AgentClaim) -> AgentAttemptRecord | None:
        """Read an exact attempt, including acknowledged terminal history."""
        claim_bytes = _canonical(claim, AgentClaim)
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT * FROM attempts WHERE node_id=? AND job_id=? "
                "AND operation_id=? AND attempt=?",
                _identity(claim),
            ).fetchone()
            if row is None:
                return None
            record = _record(row)
            if record.fence != claim.fence or record.canonical_claim != claim_bytes:
                raise AgentStateConflict("attempt conflicts with persisted state")
            return record
        except (AgentStateConflict, AgentStateError):
            raise
        except sqlite3.Error as error:
            raise AgentStateError("state operation failed") from error
        finally:
            connection.close()

    def _recover(self, predicate: str) -> AgentAttemptRecord | None:
        connection = self._connection()
        try:
            rows = connection.execute(f"SELECT * FROM attempts WHERE {predicate}").fetchall()
            if len(rows) > 1:
                raise AgentStateError("state has multiple unresolved attempts")
            return None if not rows else _record(rows[0])
        except AgentStateError:
            raise
        except sqlite3.Error as error:
            raise AgentStateError("state operation failed") from error
        finally:
            connection.close()

    def _initialize(self) -> None:
        root_descriptor = _open_root(self._root, create=True)
        database_descriptor = -1
        connection: _AnchoredConnection | None = None
        lock_acquired = False
        try:
            database_descriptor = _open_database(root_descriptor, create=True)
            _acquire_initialization_lock(database_descriptor)
            lock_acquired = True
            _validate_state_files(root_descriptor)
            connection = _connect(os.dup(root_descriptor), initialize=True)
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'"
            ).fetchone()
            if version == 0 and table_exists is None:
                connection.execute(_TABLE_SQL)
                connection.execute(_UNRESOLVED_INDEX_SQL)
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            elif version != _SCHEMA_VERSION:
                raise AgentStateError("unsupported state schema")
            _validate_schema(connection)
            connection.commit()
            _validate_state_files(root_descriptor)
        except AgentStateError:
            raise
        except sqlite3.Error as error:
            raise AgentStateError("state schema is invalid") from error
        finally:
            if connection is not None:
                connection.close()
            if database_descriptor >= 0:
                if lock_acquired:
                    try:
                        fcntl.flock(database_descriptor, fcntl.LOCK_UN)
                    except OSError:
                        # Closing the descriptor below still releases the lock.
                        pass
                os.close(database_descriptor)
            os.close(root_descriptor)

    def _connection(self) -> _AnchoredConnection:
        root_descriptor = _open_root(self._root, create=False)
        try:
            database_descriptor = _open_database(root_descriptor, create=False)
            os.close(database_descriptor)
            _validate_state_files(root_descriptor)
        except Exception:
            os.close(root_descriptor)
            raise
        # Ownership of root_descriptor transfers to _connect at this point.
        connection = _connect(root_descriptor, initialize=False)
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                raise AgentStateError("unsupported state schema")
            _validate_schema(connection)
            _validate_state_files(connection.root_descriptor)
            return connection
        except Exception:
            connection.close()
            raise


def _connect(root_descriptor: int, *, initialize: bool) -> _AnchoredConnection:
    connection: _AnchoredConnection | None = None
    database = f"/proc/self/fd/{root_descriptor}/{_DATABASE_NAME}"
    try:
        connection = sqlite3.connect(
            database,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            factory=_AnchoredConnection,
        )
        connection.root_descriptor = root_descriptor
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        if initialize:
            current_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if current_mode.lower() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        else:
            os.close(root_descriptor)
        raise AgentStateError("state database cannot be opened") from error


def _acquire_initialization_lock(
    descriptor: int,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic() + _LOCK_TIMEOUT_SECONDS
    retry_error: OSError | None = None
    first_attempt = True
    while True:
        if not first_attempt:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AgentStateError("state initialization lock timed out") from retry_error
            wait(min(_LOCK_RETRY_SECONDS, remaining))
            if deadline - monotonic() <= 0:
                raise AgentStateError("state initialization lock timed out") from retry_error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EINTR, errno.EACCES, errno.EAGAIN}:
                raise AgentStateError("state initialization lock failed") from error
            retry_error = error
            first_attempt = False


def _open_root(root: Path, *, create: bool) -> int:
    if not root.is_absolute():
        raise AgentStateError("state root must be absolute")
    if not root.parts[1:]:
        raise AgentStateError("state root must not be the filesystem root")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        components = root.parts[1:]
        for index, component in enumerate(components):
            final = index == len(components) - 1
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if final:
                if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise AgentStateError("state root is unsafe")
            else:
                _validate_ancestor(metadata)
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise AgentStateError("state root is unavailable") from error
    except AgentStateError:
        os.close(descriptor)
        raise


def _validate_ancestor(metadata: os.stat_result) -> None:
    if metadata.st_uid not in {0, os.geteuid()}:
        raise AgentStateError("state ancestor ownership is unsafe")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022 and not (mode & stat.S_ISVTX):
        raise AgentStateError("state ancestor permissions are unsafe")


def _open_database(root_descriptor: int, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(_DATABASE_NAME, flags, 0o600, dir_fd=root_descriptor)
    except OSError as error:
        raise AgentStateError("state database is unavailable") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise AgentStateError("state database is unsafe")
    return descriptor


def _validate_state_files(root_descriptor: int) -> None:
    for name, required in (
        (_DATABASE_NAME, True),
        (f"{_DATABASE_NAME}-wal", False),
        (f"{_DATABASE_NAME}-shm", False),
    ):
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_descriptor,
            )
        except FileNotFoundError:
            if required:
                raise AgentStateError("state file is missing")
            continue
        except OSError as error:
            raise AgentStateError("state file is unsafe") from error
        try:
            try:
                metadata = os.fstat(descriptor)
            except OSError as error:
                raise AgentStateError("state file is unsafe") from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise AgentStateError("state file is unsafe")
        finally:
            os.close(descriptor)


def _normalize_sql(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentStateError("state schema is invalid")
    return value.strip()


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        schema_objects = {
            (row[0], row[1], row[2], row[3])
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master"
            )
        }
        expected_objects = {
            ("table", "attempts", "attempts", _TABLE_SQL),
            ("index", "one_unresolved_attempt", "attempts", _UNRESOLVED_INDEX_SQL),
            ("index", "sqlite_autoindex_attempts_1", "attempts", None),
            ("index", "sqlite_autoindex_attempts_2", "attempts", None),
        }
        if schema_objects != expected_objects:
            raise AgentStateError("state schema contains unexpected objects")
        table_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='attempts'"
        ).fetchone()
        index_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='one_unresolved_attempt'"
        ).fetchone()
        if table_row is None or index_row is None:
            raise AgentStateError("state schema is invalid")
        if _normalize_sql(table_row[0]) != _normalize_sql(_TABLE_SQL):
            raise AgentStateError("state schema is invalid")
        if _normalize_sql(index_row[0]) != _normalize_sql(_UNRESOLVED_INDEX_SQL):
            raise AgentStateError("state schema is invalid")
        columns = tuple(
            (row[1], row[2].upper(), row[3], row[5])
            for row in connection.execute("PRAGMA table_info(attempts)")
        )
        if columns != _EXPECTED_COLUMNS:
            raise AgentStateError("state schema is invalid")
        indexes = connection.execute("PRAGMA index_list(attempts)").fetchall()
        unresolved = [row for row in indexes if row[1] == "one_unresolved_attempt"]
        if len(unresolved) != 1 or unresolved[0][2] != 1 or unresolved[0][4] != 1:
            raise AgentStateError("state schema is invalid")
        unique_column_sets = {
            tuple(item[2] for item in connection.execute(f'PRAGMA index_info("{row[1]}")'))
            for row in indexes
            if row[2] == 1
        }
        if ("fence",) not in unique_column_sets:
            raise AgentStateError("state schema is invalid")
    except AgentStateError:
        raise
    except (sqlite3.Error, TypeError, IndexError) as error:
        raise AgentStateError("state schema is invalid") from error


def _identity(message: AgentClaim | AgentProgress | AgentResult) -> tuple[str, str, str, int]:
    return message.node_id, message.job_id, message.operation_id, message.attempt


def _matching_row(connection: sqlite3.Connection, message: AgentProgress | AgentResult) -> sqlite3.Row:
    row = connection.execute(
        """SELECT * FROM attempts WHERE node_id=? AND job_id=? AND operation_id=?
           AND attempt=? AND fence=?""",
        (*_identity(message), message.fence),
    ).fetchone()
    if row is None:
        raise AgentStateConflict("attempt identity or fence does not match")
    return row


def _require_protocol_match(claim: AgentClaim, message: AgentProgress | AgentResult) -> None:
    if (
        _identity(claim) != _identity(message)
        or claim.fence != message.fence
        or claim.schema_version != message.schema_version
        or claim.deadline != message.deadline
    ):
        raise AgentStateConflict("protocol message does not match persisted claim")


def _canonical(value: object, expected_type: type) -> bytes:
    if not isinstance(value, expected_type):
        raise AgentStateError("message is invalid")
    return canonical_message(value)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AgentStateError("stored timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AgentStateError("stored timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed) or parsed.isoformat() != value:
        raise AgentStateError("stored timestamp is invalid")
    return parsed


def _nondecreasing_timestamp(previous: str) -> str:
    previous_time = _parse_time(previous)
    candidate = _parse_time(_now())
    return max(previous_time, candidate).isoformat()


def _record(row: sqlite3.Row) -> AgentAttemptRecord:
    try:
        claim_bytes = bytes(row["claim_json"])
        claim = AgentClaim.parse(json.loads(claim_bytes))
        progress_bytes = None if row["progress_json"] is None else bytes(row["progress_json"])
        progress = None if progress_bytes is None else AgentProgress.parse(json.loads(progress_bytes))
        result_bytes = None if row["result_json"] is None else bytes(row["result_json"])
        result = None if result_bytes is None else AgentResult.parse(json.loads(result_bytes))
        if canonical_message(claim) != claim_bytes:
            raise AgentStateError("stored claim is not canonical")
        if progress is not None and canonical_message(progress) != progress_bytes:
            raise AgentStateError("stored progress is not canonical")
        if result is not None and canonical_message(result) != result_bytes:
            raise AgentStateError("stored result is not canonical")
        row_identity = tuple(row[name] for name in ("node_id", "job_id", "operation_id", "attempt"))
        if _identity(claim) != row_identity or claim.fence != row["fence"]:
            raise AgentStateError("stored claim identity is invalid")
        if progress is not None:
            _require_stored_protocol_match(claim, progress)
        if result is not None:
            _require_stored_protocol_match(claim, result)
        created = _parse_time(row["created_at"])
        updated = _parse_time(row["updated_at"])
        finished = None if row["finished_at"] is None else _parse_time(row["finished_at"])
        acknowledged = None if row["acknowledged_at"] is None else _parse_time(row["acknowledged_at"])
        sequence = row["progress_sequence"]
        state = row["state"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise AgentStateError("stored progress sequence is invalid")
        if (sequence == 0) != (progress is None):
            raise AgentStateError("stored progress is incoherent")
        if created > updated:
            raise AgentStateError("stored timestamps are incoherent")
        if finished is not None and (finished < created or finished > updated):
            raise AgentStateError("stored timestamps are incoherent")
        if acknowledged is not None and (finished is None or acknowledged < finished or acknowledged > updated):
            raise AgentStateError("stored timestamps are incoherent")
        if state == "active" and (result is not None or finished is not None or acknowledged is not None):
            raise AgentStateError("active attempt is incoherent")
        if state != "active" and (result is None or finished is None or result.state != state):
            raise AgentStateError("terminal attempt is incoherent")
        return AgentAttemptRecord(
            claim=claim,
            fence=row["fence"],
            state=state,
            progress_sequence=sequence,
            progress=progress,
            result=result,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
            acknowledged_at=row["acknowledged_at"],
            canonical_claim=claim_bytes,
            canonical_progress=progress_bytes,
            canonical_result=result_bytes,
        )
    except AgentStateError:
        raise
    except (AgentProtocolError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise AgentStateError("stored state is invalid") from error


def _require_stored_protocol_match(claim: AgentClaim, message: AgentProgress | AgentResult) -> None:
    try:
        _require_protocol_match(claim, message)
    except AgentStateConflict as error:
        raise AgentStateError("stored protocol identity is invalid") from error


def _now() -> str:
    return datetime.now(UTC).isoformat()
