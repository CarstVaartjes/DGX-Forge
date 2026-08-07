"""Durable fenced operation journal for the GPU node-local package engine."""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..package_operations import OperationBinding

_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PHASE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_DATABASE_NAME = "package-state.sqlite3"
_TABLES = frozenset(
    {
        "operations",
        "reservations",
        "components",
        "partials",
        "derived_objects",
        "generations",
        "generation_objects",
        "leases",
        "gc_intents",
        "gc_candidates",
    }
)


class PackageStateError(RuntimeError):
    """Package state is unavailable, corrupt, or structurally unsafe."""


class PackageStateConflict(PackageStateError):
    """A mutation disagrees with the durable operation fence or journal."""


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    attempt: int
    fence: str
    phase: str
    cancel_requested: bool


@dataclass(frozen=True)
class GenerationRecord:
    generation_id: str
    deployment_id: str
    release_digest: str
    state: str
    object_digests: tuple[str, ...]


@dataclass(frozen=True)
class GcIntentRecord:
    intent_id: str
    target_bytes: int
    dry_run: bool
    state: str


@dataclass(frozen=True)
class GcCandidateRecord:
    digest: str
    size: int
    state: str


class PackageState:
    """SQLite-backed package state with exact operation-fence ownership."""

    def __init__(self, root: Path) -> None:
        self._root = _absolute(Path(root), "package state root")
        self._database = self._root / _DATABASE_NAME
        root_fd = _ensure_root(self._root)
        try:
            initialization_lock = _open_initialization_lock(root_fd)
        finally:
            os.close(root_fd)
        try:
            fcntl.flock(initialization_lock, fcntl.LOCK_EX)
            root_fd = _open_root(self._root)
            try:
                created = _ensure_database(root_fd)
            finally:
                os.close(root_fd)
            try:
                with self._connect(validate_schema=not created) as connection:
                    if created:
                        connection.executescript(_SCHEMA)
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                    self._validate_schema(connection)
            except PackageStateError:
                raise
            except (OSError, sqlite3.DatabaseError) as error:
                raise PackageStateError("package state database is corrupt") from error
        finally:
            os.close(initialization_lock)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def database_path(self) -> Path:
        return self._database

    def begin_operation(
        self,
        binding: OperationBinding,
        *,
        phase: str = "preflight",
    ) -> OperationRecord:
        _binding(binding)
        _phase(phase)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT job_id, node_id, attempt, fence, phase, cancel_requested "
                "FROM operations WHERE operation_id = ?",
                (binding.operation_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO operations "
                    "(operation_id, job_id, node_id, attempt, fence, phase, "
                    "cancel_requested) VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        binding.operation_id,
                        binding.job_id,
                        binding.node_id,
                        binding.attempt,
                        binding.fence,
                        phase,
                    ),
                )
                return OperationRecord(
                    binding.operation_id,
                    binding.attempt,
                    binding.fence,
                    phase,
                    False,
                )
            if row["job_id"] != binding.job_id or row["node_id"] != binding.node_id:
                raise PackageStateConflict("operation identity disagrees with journal")
            if binding.attempt < row["attempt"]:
                raise PackageStateConflict("operation attempt is stale")
            if binding.attempt == row["attempt"]:
                if binding.fence != row["fence"]:
                    raise PackageStateConflict("operation fence disagrees with journal")
                return _operation_record(binding.operation_id, row)
            connection.execute(
                "UPDATE operations SET attempt = ?, fence = ?, phase = ?, "
                "cancel_requested = 0 WHERE operation_id = ?",
                (binding.attempt, binding.fence, phase, binding.operation_id),
            )
            connection.execute(
                "DELETE FROM reservations WHERE operation_id = ?",
                (binding.operation_id,),
            )
            return OperationRecord(
                binding.operation_id,
                binding.attempt,
                binding.fence,
                phase,
                False,
            )

    def operation(self, binding: OperationBinding) -> OperationRecord:
        _binding(binding)
        with self._connect() as connection:
            row = self._assert_binding(connection, binding)
            return _operation_record(binding.operation_id, row)

    def set_phase(self, binding: OperationBinding, phase: str) -> OperationRecord:
        _binding(binding)
        _phase(phase)
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            connection.execute(
                "UPDATE operations SET phase = ? WHERE operation_id = ?",
                (phase, binding.operation_id),
            )
        return self.operation(binding)

    def request_cancel(self, binding: OperationBinding) -> None:
        _binding(binding)
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            connection.execute(
                "UPDATE operations SET cancel_requested = 1 WHERE operation_id = ?",
                (binding.operation_id,),
            )

    def record_generation(
        self,
        binding: OperationBinding,
        *,
        deployment_id: str,
        generation_id: str,
        release_digest: str,
        object_digests: tuple[str, ...],
        state: str,
    ) -> None:
        _binding(binding)
        _identifier(deployment_id, "deployment ID")
        _identifier(generation_id, "generation ID")
        _digest(release_digest, "release digest")
        _generation_state(state)
        if not object_digests or len(object_digests) != len(set(object_digests)):
            raise ValueError("generation object digests are invalid")
        for digest in object_digests:
            _digest(digest, "generation object digest")
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            existing = connection.execute(
                "SELECT operation_id, attempt, fence FROM generations "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if existing is not None and (
                existing["operation_id"] != binding.operation_id
                or existing["attempt"] != binding.attempt
                or existing["fence"] != binding.fence
            ):
                raise PackageStateConflict("generation ownership is invalid")
            connection.execute(
                "INSERT INTO generations "
                "(generation_id, deployment_id, release_digest, state, "
                "operation_id, attempt, fence) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(generation_id) DO UPDATE SET "
                "deployment_id=excluded.deployment_id, "
                "release_digest=excluded.release_digest, state=excluded.state",
                (
                    generation_id,
                    deployment_id,
                    release_digest,
                    state,
                    binding.operation_id,
                    binding.attempt,
                    binding.fence,
                ),
            )
            connection.execute(
                "DELETE FROM generation_objects WHERE generation_id = ?",
                (generation_id,),
            )
            connection.executemany(
                "INSERT INTO generation_objects (generation_id, object_digest) "
                "VALUES (?, ?)",
                ((generation_id, digest) for digest in object_digests),
            )

    def acquire_lease(
        self,
        binding: OperationBinding,
        *,
        lease_id: str,
        generation_id: str,
        expires_at_ns: int,
    ) -> None:
        _binding(binding)
        _uuid4(lease_id, "lease ID")
        _identifier(generation_id, "generation ID")
        if type(expires_at_ns) is not int or expires_at_ns < 1:
            raise ValueError("lease expiry is invalid")
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            generation = connection.execute(
                "SELECT 1 FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None:
                raise PackageStateConflict("lease generation is unknown")
            connection.execute(
                "INSERT INTO leases "
                "(lease_id, generation_id, expires_at_ns, operation_id, attempt, fence) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(lease_id) DO UPDATE SET "
                "expires_at_ns=excluded.expires_at_ns "
                "WHERE operation_id=excluded.operation_id "
                "AND attempt=excluded.attempt AND fence=excluded.fence",
                (
                    lease_id,
                    generation_id,
                    expires_at_ns,
                    binding.operation_id,
                    binding.attempt,
                    binding.fence,
                ),
            )

    def generation(self, generation_id: str) -> GenerationRecord | None:
        _identifier(generation_id, "generation ID")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generation_id, deployment_id, release_digest, state "
                "FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            return None if row is None else _generation_record(connection, row)

    def generations(
        self, deployment_id: str | None = None
    ) -> tuple[GenerationRecord, ...]:
        if deployment_id is not None:
            _identifier(deployment_id, "deployment ID")
        with self._connect() as connection:
            if deployment_id is None:
                rows = connection.execute(
                    "SELECT generation_id, deployment_id, release_digest, state "
                    "FROM generations ORDER BY generation_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT generation_id, deployment_id, release_digest, state "
                    "FROM generations WHERE deployment_id = ? ORDER BY generation_id",
                    (deployment_id,),
                ).fetchall()
            return tuple(_generation_record(connection, row) for row in rows)

    def transition_generation(
        self,
        binding: OperationBinding,
        *,
        generation_id: str,
        expected_states: frozenset[str],
        state: str,
    ) -> GenerationRecord:
        _binding(binding)
        _identifier(generation_id, "generation ID")
        _state_set(expected_states, "generation expected states")
        _generation_state(state)
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            row = connection.execute(
                "SELECT generation_id, deployment_id, release_digest, state "
                "FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if row is None:
                raise PackageStateConflict("generation is unknown")
            if row["state"] != state and row["state"] not in expected_states:
                raise PackageStateConflict("generation state changed concurrently")
            connection.execute(
                "UPDATE generations SET state=?, operation_id=?, attempt=?, fence=? "
                "WHERE generation_id=?",
                (
                    state,
                    binding.operation_id,
                    binding.attempt,
                    binding.fence,
                    generation_id,
                ),
            )
            row = dict(row)
            row["state"] = state
            return _generation_record(connection, row)

    def reachable_objects(self, *, now_ns: int) -> set[str]:
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("reachability time is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT go.object_digest FROM generation_objects go "
                "JOIN generations g ON g.generation_id = go.generation_id "
                "LEFT JOIN leases l ON l.generation_id = g.generation_id "
                "WHERE g.state IN "
                "('staging', 'validated', 'active', 'retained', "
                "'staged', 'rollback', 'pinned') "
                "OR l.expires_at_ns >= ?",
                (now_ns,),
            )
            return {row[0] for row in rows}

    def generation_for_release(
        self, deployment_id: str, release_digest: str
    ) -> GenerationRecord | None:
        _identifier(deployment_id, "deployment ID")
        _digest(release_digest, "release digest")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generation_id, deployment_id, release_digest, state "
                "FROM generations WHERE deployment_id = ? AND release_digest = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (deployment_id, release_digest),
            ).fetchone()
            return None if row is None else _generation_record(connection, row)

    def active_generation(self, deployment_id: str) -> GenerationRecord | None:
        _identifier(deployment_id, "deployment ID")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generation_id, deployment_id, release_digest, state "
                "FROM generations WHERE deployment_id = ? AND state = 'active' "
                "ORDER BY rowid DESC LIMIT 1",
                (deployment_id,),
            ).fetchone()
            return None if row is None else _generation_record(connection, row)

    def has_live_lease(self, generation_id: str, *, now_ns: int) -> bool:
        """Return whether a process lease still protects a generation.

        Lease rows are deliberately retained until their owning operation
        reaps them.  Removal and GC must therefore evaluate expiry at the
        point of the decision instead of treating the mere presence of a row
        as a live process.  The comparison matches :meth:`reachable_objects`
        (`>=`) so a lease remains valid through its exact expiry instant.
        """
        _identifier(generation_id, "generation ID")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("lease time is invalid")
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM leases WHERE generation_id = ? "
                    "AND expires_at_ns >= ? LIMIT 1",
                    (generation_id, now_ns),
                ).fetchone()
                is not None
            )

    def has_generation_reference(
        self,
        release_digest: str,
        *,
        excluding_generation: str | None = None,
        now_ns: int,
    ) -> bool:
        """Return whether another generation still needs a release tree.

        Materialization is keyed by release digest while the journal identity
        is deployment/generation scoped.  A release tree must not be removed
        when another deployment shares it, including a failed generation
        that still has a live process lease.  This is the state-side half of
        the remove/GC reachability boundary; the caller performs filesystem
        cleanup only after this check succeeds.
        """
        _digest(release_digest, "release digest")
        if excluding_generation is not None:
            _identifier(excluding_generation, "generation ID")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("lease time is invalid")
        with self._connect() as connection:
            clauses = ["g.release_digest = ?"]
            values: list[object] = [release_digest]
            if excluding_generation is not None:
                clauses.append("g.generation_id <> ?")
                values.append(excluding_generation)
            query = (
                "SELECT 1 FROM generations g "
                "WHERE "
                + " AND ".join(clauses)
                + " AND (g.state NOT IN ('failed', 'quarantined', 'inactive') "
                "OR EXISTS (SELECT 1 FROM leases l WHERE l.generation_id = "
                "g.generation_id AND l.expires_at_ns >= ?)) LIMIT 1"
            )
            values.append(now_ns)
            return connection.execute(query, tuple(values)).fetchone() is not None

    def activate_generation(
        self,
        binding: OperationBinding,
        generation_id: str,
    ) -> GenerationRecord | None:
        _binding(binding)
        _identifier(generation_id, "generation ID")
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            target = connection.execute(
                "SELECT deployment_id, state FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if target is None or target["state"] not in {
                "validated",
                "retained",
                "active",
            }:
                raise PackageStateConflict("generation is not activatable")
            previous = connection.execute(
                "SELECT generation_id, deployment_id, release_digest, state "
                "FROM generations WHERE deployment_id = ? AND state = 'active' "
                "AND generation_id <> ?",
                (target["deployment_id"], generation_id),
            ).fetchone()
            if previous is not None:
                connection.execute(
                    "UPDATE generations SET state = 'retained', operation_id = ?, "
                    "attempt = ?, fence = ? WHERE generation_id = ?",
                    (
                        binding.operation_id,
                        binding.attempt,
                        binding.fence,
                        previous["generation_id"],
                    ),
                )
            connection.execute(
                "UPDATE generations SET state = 'active', operation_id = ?, "
                "attempt = ?, fence = ? WHERE generation_id = ?",
                (
                    binding.operation_id,
                    binding.attempt,
                    binding.fence,
                    generation_id,
                ),
            )
            return (
                None if previous is None else _generation_record(connection, previous)
            )

    def record_gc_intent(
        self,
        binding: OperationBinding,
        *,
        intent_id: str,
        target_bytes: int,
        dry_run: bool,
    ) -> None:
        _binding(binding)
        _uuid4(intent_id, "GC intent ID")
        if (
            type(target_bytes) is not int
            or target_bytes < 1
            or type(dry_run) is not bool
        ):
            raise ValueError("GC intent is invalid")
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            existing = connection.execute(
                "SELECT target_bytes, dry_run, state, operation_id, attempt, fence "
                "FROM gc_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["target_bytes"] != target_bytes
                    or bool(existing["dry_run"]) != dry_run
                    or existing["operation_id"] != binding.operation_id
                    or existing["attempt"] != binding.attempt
                    or existing["fence"] != binding.fence
                ):
                    raise PackageStateConflict("GC intent replay is inconsistent")
                return
            connection.execute(
                "INSERT INTO gc_intents "
                "(intent_id, target_bytes, dry_run, state, operation_id, attempt, fence) "
                "VALUES (?, ?, ?, 'planned', ?, ?, ?)",
                (
                    intent_id,
                    target_bytes,
                    int(dry_run),
                    binding.operation_id,
                    binding.attempt,
                    binding.fence,
                ),
            )

    def gc_intent(
        self, binding: OperationBinding, intent_id: str
    ) -> GcIntentRecord | None:
        _binding(binding)
        _uuid4(intent_id, "GC intent ID")
        with self._connect() as connection:
            self._assert_binding(connection, binding)
            row = connection.execute(
                "SELECT intent_id, target_bytes, dry_run, state, operation_id, "
                "attempt, fence FROM gc_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                return None
            _gc_owner(row, binding)
            return _gc_intent_record(row)

    def transition_gc_intent(
        self,
        binding: OperationBinding,
        *,
        intent_id: str,
        expected_states: frozenset[str],
        state: str,
    ) -> GcIntentRecord:
        _binding(binding)
        _uuid4(intent_id, "GC intent ID")
        _gc_state(state)
        _gc_state_set(expected_states)
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            row = connection.execute(
                "SELECT intent_id, target_bytes, dry_run, state, operation_id, "
                "attempt, fence FROM gc_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise PackageStateConflict("GC intent is unknown")
            _gc_owner(row, binding)
            if row["state"] != state and row["state"] not in expected_states:
                raise PackageStateConflict("GC intent state changed concurrently")
            connection.execute(
                "UPDATE gc_intents SET state = ? WHERE intent_id = ?",
                (state, intent_id),
            )
            row = dict(row)
            row["state"] = state
            return _gc_intent_record(row)

    def plan_gc_candidates(
        self,
        binding: OperationBinding,
        intent_id: str,
        candidates: tuple[tuple[str, int], ...],
    ) -> tuple[GcCandidateRecord, ...]:
        _binding(binding)
        _uuid4(intent_id, "GC intent ID")
        normalized = _gc_candidates(candidates)
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            intent = connection.execute(
                "SELECT operation_id, attempt, fence FROM gc_intents "
                "WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise PackageStateConflict("GC intent is unknown")
            _gc_owner(intent, binding)
            existing = _gc_candidate_rows(connection, intent_id)
            if existing:
                expected = tuple((item.digest, item.size) for item in existing)
                if expected != normalized:
                    raise PackageStateConflict("GC candidate plan is immutable")
                return existing
            connection.executemany(
                "INSERT INTO gc_candidates (intent_id, digest, size, state) "
                "VALUES (?, ?, ?, 'pending')",
                ((intent_id, digest, size) for digest, size in normalized),
            )
            return _gc_candidate_rows(connection, intent_id)

    def list_gc_candidates(
        self, binding: OperationBinding, intent_id: str
    ) -> tuple[GcCandidateRecord, ...]:
        _binding(binding)
        _uuid4(intent_id, "GC intent ID")
        with self._connect() as connection:
            self._assert_binding(connection, binding)
            intent = connection.execute(
                "SELECT operation_id, attempt, fence FROM gc_intents "
                "WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise PackageStateConflict("GC intent is unknown")
            _gc_owner(intent, binding)
            return _gc_candidate_rows(connection, intent_id)

    def mark_gc_candidate(
        self,
        binding: OperationBinding,
        intent_id: str,
        digest: str,
        *,
        state: str,
    ) -> GcCandidateRecord:
        _binding(binding)
        _uuid4(intent_id, "GC intent ID")
        _digest(digest, "GC candidate digest")
        if state not in {"pending", "deleted", "skipped"}:
            raise ValueError("GC candidate state is invalid")
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            intent = connection.execute(
                "SELECT operation_id, attempt, fence FROM gc_intents "
                "WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise PackageStateConflict("GC intent is unknown")
            _gc_owner(intent, binding)
            row = connection.execute(
                "SELECT digest, size, state FROM gc_candidates "
                "WHERE intent_id = ? AND digest = ?",
                (intent_id, digest),
            ).fetchone()
            if row is None:
                raise PackageStateConflict("GC candidate is unknown")
            if row["state"] != state and row["state"] != "pending":
                raise PackageStateConflict("GC candidate state changed concurrently")
            connection.execute(
                "UPDATE gc_candidates SET state = ? WHERE intent_id = ? AND digest = ?",
                (state, intent_id, digest),
            )
            return GcCandidateRecord(digest, row["size"], state)

    def record_derived(
        self,
        binding: OperationBinding,
        derivation_digest: str,
        object_digest: str,
    ) -> None:
        _binding(binding)
        _digest(derivation_digest, "derivation digest")
        _digest(object_digest, "derived object digest")
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            row = connection.execute(
                "SELECT object_digest, operation_id, attempt, fence "
                "FROM derived_objects WHERE derivation_digest = ?",
                (derivation_digest,),
            ).fetchone()
            if row is not None and (
                row["object_digest"] != object_digest
                or row["operation_id"] != binding.operation_id
                or row["attempt"] != binding.attempt
                or row["fence"] != binding.fence
            ):
                raise PackageStateConflict("derived object mapping is immutable")
            connection.execute(
                "INSERT INTO derived_objects "
                "(derivation_digest, object_digest, operation_id, attempt, fence) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(derivation_digest) DO NOTHING",
                (
                    derivation_digest,
                    object_digest,
                    binding.operation_id,
                    binding.attempt,
                    binding.fence,
                ),
            )

    def lookup_derived(self, derivation_digest: str) -> str | None:
        _digest(derivation_digest, "derivation digest")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT object_digest FROM derived_objects WHERE derivation_digest = ?",
                (derivation_digest,),
            ).fetchone()
            return None if row is None else row["object_digest"]

    def forget_object(self, binding: OperationBinding, object_digest: str) -> None:
        _binding(binding)
        _digest(object_digest, "object digest")
        with self.transaction() as connection:
            self._assert_binding(connection, binding)
            connection.execute(
                "DELETE FROM derived_objects WHERE object_digest = ?",
                (object_digest,),
            )
            connection.execute(
                "DELETE FROM components WHERE digest = ? AND state = 'complete'",
                (object_digest,),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def assert_binding(
        self, connection: sqlite3.Connection, binding: OperationBinding
    ) -> sqlite3.Row:
        return self._assert_binding(connection, binding)

    def _assert_binding(
        self, connection: sqlite3.Connection, binding: OperationBinding
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT job_id, node_id, attempt, fence, phase, cancel_requested "
            "FROM operations "
            "WHERE operation_id = ?",
            (binding.operation_id,),
        ).fetchone()
        if row is None:
            raise PackageStateConflict("operation is not journaled")
        if row["job_id"] != binding.job_id or row["node_id"] != binding.node_id:
            raise PackageStateConflict("operation identity disagrees with journal")
        if binding.attempt < row["attempt"]:
            raise PackageStateConflict("operation attempt is stale")
        if binding.attempt != row["attempt"] or binding.fence != row["fence"]:
            raise PackageStateConflict("operation fence disagrees with journal")
        return row

    @contextmanager
    def _connect(self, *, validate_schema: bool = True) -> Iterator[sqlite3.Connection]:
        root_fd = _open_root(self._root)
        try:
            _validate_database(root_fd)
        finally:
            os.close(root_fd)
        try:
            connection = sqlite3.connect(
                self._database,
                timeout=10,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA journal_mode = DELETE")
            if validate_schema:
                result = connection.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise PackageStateError("package state database is corrupt")
            yield connection
        except sqlite3.DatabaseError as error:
            raise PackageStateError("package state database is corrupt") from error
        finally:
            if "connection" in locals():
                connection.close()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if type(version) is not int or version != _SCHEMA_VERSION:
            raise PackageStateError("package state schema version is invalid")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not _TABLES <= tables:
            raise PackageStateError("package state database schema is incomplete")


def _operation_record(operation_id: str, row: sqlite3.Row) -> OperationRecord:
    return OperationRecord(
        operation_id=operation_id,
        attempt=row["attempt"],
        fence=row["fence"],
        phase=row["phase"],
        cancel_requested=bool(row["cancel_requested"]),
    )


def _generation_record(connection: sqlite3.Connection, row) -> GenerationRecord:
    objects = tuple(
        item[0]
        for item in connection.execute(
            "SELECT object_digest FROM generation_objects WHERE generation_id = ? "
            "ORDER BY object_digest",
            (row["generation_id"],),
        )
    )
    return GenerationRecord(
        generation_id=row["generation_id"],
        deployment_id=row["deployment_id"],
        release_digest=row["release_digest"],
        state=row["state"],
        object_digests=objects,
    )


def _gc_intent_record(row) -> GcIntentRecord:
    return GcIntentRecord(
        intent_id=row["intent_id"],
        target_bytes=row["target_bytes"],
        dry_run=bool(row["dry_run"]),
        state=row["state"],
    )


def _gc_owner(row, binding: OperationBinding) -> None:
    if (
        row["operation_id"] != binding.operation_id
        or row["attempt"] != binding.attempt
        or row["fence"] != binding.fence
    ):
        raise PackageStateConflict("GC intent ownership is invalid")


def _gc_candidate_rows(
    connection: sqlite3.Connection, intent_id: str
) -> tuple[GcCandidateRecord, ...]:
    return tuple(
        GcCandidateRecord(row["digest"], row["size"], row["state"])
        for row in connection.execute(
            "SELECT digest, size, state FROM gc_candidates WHERE intent_id = ? "
            "ORDER BY digest",
            (intent_id,),
        )
    )


def _binding(value: OperationBinding) -> None:
    if not isinstance(value, OperationBinding):
        raise TypeError("operation binding is invalid")
    _uuid4(value.job_id, "job ID")
    _uuid4(value.operation_id, "operation ID")
    _uuid4(value.fence, "operation fence")
    if type(value.attempt) is not int or not 1 <= value.attempt <= 999_999_999:
        raise ValueError("operation attempt is invalid")
    if (
        not isinstance(value.node_id, str)
        or re.fullmatch(r"spk_[0-9a-f]{32}", value.node_id) is None
    ):
        raise ValueError("operation node ID is invalid")


def _uuid4(value: object, label: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{label} is invalid")
    return str(parsed)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _phase(value: object) -> str:
    if not isinstance(value, str) or _PHASE.fullmatch(value) is None:
        raise ValueError("operation phase is invalid")
    return value


def _generation_state(value: object) -> str:
    if value not in {
        "staging",
        "validated",
        "active",
        "retained",
        "failed",
        "quarantined",
        "staged",
        "rollback",
        "inactive",
        "pinned",
    }:
        raise ValueError("generation state is invalid")
    return str(value)


def _state_set(value: object, label: str) -> frozenset[str]:
    if not isinstance(value, frozenset) or not value:
        raise ValueError(f"{label} are invalid")
    for item in value:
        _generation_state(item)
    return value


def _gc_state(value: object) -> str:
    if value not in {"planned", "running", "completed", "cancelled"}:
        raise ValueError("GC intent state is invalid")
    return str(value)


def _gc_state_set(value: object) -> frozenset[str]:
    if not isinstance(value, frozenset) or not value:
        raise ValueError("GC expected states are invalid")
    for item in value:
        _gc_state(item)
    return value


def _gc_candidates(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, tuple) or not value:
        raise ValueError("GC candidates are invalid")
    result: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("GC candidates are invalid")
        digest, size = item
        _digest(digest, "GC candidate digest")
        if type(size) is not int or size < 1:
            raise ValueError("GC candidate size is invalid")
        result.append((digest, size))
    normalized = tuple(sorted(result))
    if len(normalized) != len({digest for digest, _size in normalized}):
        raise ValueError("GC candidates are duplicated")
    return normalized


def _absolute(path: Path, label: str) -> Path:
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageStateError(f"{label} must be an absolute normalized path")
    return path


def _trusted_owner(metadata: os.stat_result) -> bool:
    return metadata.st_uid in {0, os.geteuid()}


def _validate_root(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not _trusted_owner(metadata)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PackageStateError("package state root is unsafe")


def _open_root(root: Path) -> int:
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise PackageStateError("package state root is unsafe") from error
    try:
        _validate_root(os.fstat(descriptor))
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _ensure_root(root: Path) -> int:
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise PackageStateError(
            "package state root cannot be created safely"
        ) from error
    return _open_root(root)


def _validate_database(root_fd: int) -> os.stat_result:
    try:
        metadata = os.stat(_DATABASE_NAME, dir_fd=root_fd, follow_symlinks=False)
    except OSError as error:
        raise PackageStateError("package state database is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not _trusted_owner(metadata)
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PackageStateError("package state database is unsafe")
    return metadata


def _ensure_database(root_fd: int) -> bool:
    try:
        descriptor = os.open(
            _DATABASE_NAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
    except FileExistsError:
        _validate_database(root_fd)
        return False
    except OSError as error:
        raise PackageStateError(
            "package state database cannot be created safely"
        ) from error
    else:
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(root_fd)
        return True


def _open_initialization_lock(root_fd: int) -> int:
    try:
        descriptor = os.open(
            "package-state.init.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
    except OSError as error:
        raise PackageStateError(
            "package state initialization lock is unsafe"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not _trusted_owner(metadata)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PackageStateError("package state initialization lock is unsafe")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


_SCHEMA = """
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    fence TEXT NOT NULL,
    phase TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL CHECK (cancel_requested IN (0, 1))
) STRICT;
CREATE TABLE reservations (
    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    fence TEXT NOT NULL,
    bytes_reserved INTEGER NOT NULL CHECK (bytes_reserved >= 0)
) STRICT;
CREATE TABLE components (
    digest TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('partial', 'complete')),
    relative_name TEXT,
    operation_id TEXT,
    attempt INTEGER,
    fence TEXT
) STRICT;
CREATE TABLE partials (
    digest TEXT PRIMARY KEY REFERENCES components(digest) ON DELETE CASCADE,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    attempt INTEGER NOT NULL,
    fence TEXT NOT NULL,
    partial_name TEXT NOT NULL UNIQUE,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    bytes_written INTEGER NOT NULL DEFAULT 0 CHECK (bytes_written >= 0),
    validator_etag TEXT,
    validator_last_modified TEXT
) STRICT;
CREATE TABLE derived_objects (
    derivation_digest TEXT PRIMARY KEY,
    object_digest TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    attempt INTEGER NOT NULL,
    fence TEXT NOT NULL
) STRICT;
CREATE TABLE generations (
    generation_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    release_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'staging', 'validated', 'active', 'retained', 'failed', 'quarantined',
        'staged', 'rollback', 'inactive', 'pinned'
    )),
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    attempt INTEGER NOT NULL,
    fence TEXT NOT NULL
) STRICT;
CREATE TABLE generation_objects (
    generation_id TEXT NOT NULL REFERENCES generations(generation_id) ON DELETE CASCADE,
    object_digest TEXT NOT NULL,
    PRIMARY KEY (generation_id, object_digest)
) WITHOUT ROWID, STRICT;
CREATE TABLE leases (
    lease_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL REFERENCES generations(generation_id) ON DELETE CASCADE,
    expires_at_ns INTEGER NOT NULL CHECK (expires_at_ns >= 1),
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    attempt INTEGER NOT NULL,
    fence TEXT NOT NULL
) STRICT;
CREATE TABLE gc_intents (
    intent_id TEXT PRIMARY KEY,
    target_bytes INTEGER NOT NULL CHECK (target_bytes >= 1),
    dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN ('planned', 'running', 'completed', 'cancelled')),
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    attempt INTEGER NOT NULL,
    fence TEXT NOT NULL
) STRICT;
CREATE TABLE gc_candidates (
    intent_id TEXT NOT NULL REFERENCES gc_intents(intent_id) ON DELETE CASCADE,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 1),
    state TEXT NOT NULL CHECK (state IN ('pending', 'deleted', 'skipped')),
    PRIMARY KEY (intent_id, digest)
) WITHOUT ROWID, STRICT;
"""
