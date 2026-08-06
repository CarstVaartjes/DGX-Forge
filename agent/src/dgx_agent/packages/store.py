"""Anchored immutable content storage with durable capacity reservations."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..deadlines import DeadlineBindingError
from .providers import Validators
from .state import (
    OperationBinding,
    PackageState,
    PackageStateConflict,
    PackageStateError,
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KIND = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_MAX_COMPONENT_BYTES = 16 * 1024**4


class PackageStoreError(RuntimeError):
    """The package store is unsafe, inconsistent, or cannot be mutated."""


class PackageCapacityError(PackageStoreError):
    """The requested bytes cannot be reserved without overcommit."""


@dataclass(frozen=True)
class ComponentDescriptor:
    digest: str
    size: int
    kind: str

    def __post_init__(self) -> None:
        _digest(self.digest)
        if type(self.size) is not int or not 0 <= self.size <= _MAX_COMPONENT_BYTES:
            raise ValueError("component size is invalid")
        if not isinstance(self.kind, str) or _KIND.fullmatch(self.kind) is None:
            raise ValueError("component kind is invalid")


@dataclass(frozen=True)
class Reservation:
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    bytes_reserved: int

    @property
    def reservation_id(self) -> str:
        return f"{self.operation_id}:{self.attempt}:{self.fence}"

    @property
    def binding(self) -> OperationBinding:
        return OperationBinding(
            job_id=self.job_id,
            operation_id=self.operation_id,
            attempt=self.attempt,
            fence=self.fence,
            node_id=self.node_id,
        )

    def with_binding(self, binding: OperationBinding) -> Reservation:
        if not isinstance(binding, OperationBinding):
            raise TypeError("reservation binding is invalid")
        return replace(
            self,
            job_id=binding.job_id,
            operation_id=binding.operation_id,
            attempt=binding.attempt,
            fence=binding.fence,
            node_id=binding.node_id,
        )


@dataclass(frozen=True)
class DownloadRecord:
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    component: str
    digest: str
    size: int
    kind: str
    partial_name: str
    device: int
    inode: int
    ctime_ns: int
    bytes_completed: int
    validators: Validators
    state: str

    @property
    def expected_size(self) -> int:
        return self.size

    @property
    def bytes_written(self) -> int:
        return self.bytes_completed

    @property
    def binding(self) -> OperationBinding:
        return OperationBinding(
            job_id=self.job_id,
            operation_id=self.operation_id,
            attempt=self.attempt,
            fence=self.fence,
            node_id=self.node_id,
        )

    def with_fence(self, fence: str) -> DownloadRecord:
        return replace(self, fence=fence)


@dataclass(frozen=True)
class StoreObject:
    digest: str
    size: int
    kind: str
    relative_name: str


class ContentStore:
    """Content-addressed store rooted beneath one private agent directory."""

    def __init__(
        self,
        root: Path,
        *,
        capacity_bytes: int | None = None,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        if capacity_bytes is not None and (
            type(capacity_bytes) is not int
            or not 1 <= capacity_bytes <= _MAX_COMPONENT_BYTES
        ):
            raise ValueError("package store capacity is invalid")
        if crash_hook is not None and not callable(crash_hook):
            raise TypeError("package store crash hook is invalid")
        try:
            self._state = PackageState(Path(root))
        except PackageStateError as error:
            raise PackageStoreError(str(error)) from error
        self._root = self._state.root
        self._capacity_bytes = capacity_bytes
        self._crash_hook = crash_hook or (lambda _phase: None)
        try:
            _ensure_layout(self._root)
            self._recover()
        except (OSError, PackageStateError, PackageStoreError) as error:
            if isinstance(error, PackageStoreError):
                raise
            raise PackageStoreError("package store layout is unsafe") from error

    @property
    def root(self) -> Path:
        return self._root

    @property
    def state(self) -> PackageState:
        return self._state

    def reserve(
        self,
        operation: OperationBinding,
        bytes_required: int,
    ) -> Reservation:
        if not isinstance(operation, OperationBinding):
            raise TypeError("package reservation operation is invalid")
        if (
            type(bytes_required) is not int
            or not 0 <= bytes_required <= _MAX_COMPONENT_BYTES
        ):
            raise ValueError("package reservation size is invalid")
        self._state.begin_operation(operation, phase="reserve")
        with self._state.transaction() as connection:
            self._state.assert_binding(connection, operation)
            existing = connection.execute(
                "SELECT attempt, fence, bytes_reserved FROM reservations "
                "WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["attempt"] != operation.attempt
                    or existing["fence"] != operation.fence
                    or existing["bytes_reserved"] != bytes_required
                ):
                    raise PackageStateConflict(
                        "capacity reservation disagrees with operation fence"
                    )
            else:
                committed = connection.execute(
                    "SELECT COALESCE(SUM(size), 0) FROM components "
                    "WHERE state = 'complete'"
                ).fetchone()[0]
                reserved = connection.execute(
                    "SELECT COALESCE(SUM(bytes_reserved), 0) FROM reservations"
                ).fetchone()[0]
                capacity = self._logical_capacity()
                if committed + reserved + bytes_required > capacity:
                    raise PackageCapacityError("package store capacity is insufficient")
                available = _available_bytes(self._root)
                if bytes_required > available:
                    raise PackageCapacityError("package store filesystem is full")
                connection.execute(
                    "INSERT INTO reservations "
                    "(operation_id, attempt, fence, bytes_reserved) VALUES (?, ?, ?, ?)",
                    (
                        operation.operation_id,
                        operation.attempt,
                        operation.fence,
                        bytes_required,
                    ),
                )
        return Reservation(
            job_id=operation.job_id,
            operation_id=operation.operation_id,
            attempt=operation.attempt,
            fence=operation.fence,
            node_id=operation.node_id,
            bytes_reserved=bytes_required,
        )

    def reserve_component(
        self,
        operation: OperationBinding,
        descriptor: object,
    ) -> Reservation:
        """Reserve a component while coalescing concurrent same-digest fetches.

        A normal reservation accounts for the complete component size.  When
        another operation already owns the same digest, no second capacity
        reservation is needed: the caller receives a zero-byte shared
        reservation and waits for the owner's verified promotion.  A stale
        partial with no owner reservation is adopted by the new fence and its
        remaining bytes are reserved normally.
        """

        if not isinstance(operation, OperationBinding):
            raise TypeError("package reservation operation is invalid")
        descriptor, component_name = _component_descriptor(descriptor)
        self._state.begin_operation(operation, phase="reserve")
        with self._state.transaction() as connection:
            self._state.assert_binding(connection, operation)
            existing_reservation = connection.execute(
                "SELECT attempt, fence, bytes_reserved FROM reservations "
                "WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            if existing_reservation is not None:
                if (
                    existing_reservation["attempt"] != operation.attempt
                    or existing_reservation["fence"] != operation.fence
                ):
                    raise PackageStateConflict(
                        "capacity reservation disagrees with operation fence"
                    )
                return Reservation(
                    operation.job_id,
                    operation.operation_id,
                    operation.attempt,
                    operation.fence,
                    operation.node_id,
                    existing_reservation["bytes_reserved"],
                )
            component = connection.execute(
                "SELECT name, size, kind, state, operation_id, attempt, fence "
                "FROM components WHERE digest = ?",
                (descriptor.digest,),
            ).fetchone()
            if component is not None:
                if (
                    component["name"] != component_name
                    or component["size"] != descriptor.size
                    or component["kind"] != descriptor.kind
                ):
                    raise PackageStoreError(
                        "component digest metadata is inconsistent"
                    )
                if component["state"] == "complete":
                    bytes_reserved = 0
                else:
                    owner = connection.execute(
                        "SELECT operation_id, attempt, fence, bytes_reserved "
                        "FROM reservations WHERE operation_id = ?",
                        (component["operation_id"],),
                    ).fetchone()
                    if owner is not None:
                        # The owner already accounts for the bytes on disk;
                        # coalesce this request rather than overcommitting.
                        bytes_reserved = 0
                    else:
                        # A crashed owner released its reservation.  Adopt its
                        # partial under this fence and account for its size.
                        committed = connection.execute(
                            "SELECT COALESCE(SUM(size), 0) FROM components "
                            "WHERE state = 'complete'"
                        ).fetchone()[0]
                        reserved = connection.execute(
                            "SELECT COALESCE(SUM(bytes_reserved), 0) "
                            "FROM reservations"
                        ).fetchone()[0]
                        if committed + reserved + descriptor.size > self._logical_capacity():
                            raise PackageCapacityError(
                                "package store capacity is insufficient"
                            )
                        if descriptor.size > _available_bytes(self._root):
                            raise PackageCapacityError("package store filesystem is full")
                        bytes_reserved = descriptor.size
                        connection.execute(
                            "UPDATE components SET operation_id=?, attempt=?, fence=? "
                            "WHERE digest=?",
                            (
                                operation.operation_id,
                                operation.attempt,
                                operation.fence,
                                descriptor.digest,
                            ),
                        )
                        connection.execute(
                            "UPDATE partials SET operation_id=?, attempt=?, fence=? "
                            "WHERE digest=?",
                            (
                                operation.operation_id,
                                operation.attempt,
                                operation.fence,
                                descriptor.digest,
                            ),
                        )
                connection.execute(
                    "INSERT INTO reservations "
                    "(operation_id, attempt, fence, bytes_reserved) VALUES (?, ?, ?, ?)",
                    (
                        operation.operation_id,
                        operation.attempt,
                        operation.fence,
                        bytes_reserved,
                    ),
                )
                return Reservation(
                    operation.job_id,
                    operation.operation_id,
                    operation.attempt,
                    operation.fence,
                    operation.node_id,
                    bytes_reserved,
                )
            committed = connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM components "
                "WHERE state = 'complete'"
            ).fetchone()[0]
            reserved = connection.execute(
                "SELECT COALESCE(SUM(bytes_reserved), 0) FROM reservations"
            ).fetchone()[0]
            capacity = self._logical_capacity()
            if committed + reserved + descriptor.size > capacity:
                raise PackageCapacityError("package store capacity is insufficient")
            available = _available_bytes(self._root)
            if descriptor.size > available:
                raise PackageCapacityError("package store filesystem is full")
            connection.execute(
                "INSERT INTO reservations "
                "(operation_id, attempt, fence, bytes_reserved) VALUES (?, ?, ?, ?)",
                (
                    operation.operation_id,
                    operation.attempt,
                    operation.fence,
                    descriptor.size,
                ),
            )
            return Reservation(
                operation.job_id,
                operation.operation_id,
                operation.attempt,
                operation.fence,
                operation.node_id,
                descriptor.size,
            )

    def wait_for_component(
        self,
        operation: OperationBinding,
        digest: str,
        size: int,
        cancelled: Callable[[], bool],
        *,
        deadline: object | None = None,
        timeout_seconds: float = 30.0,
    ) -> StoreObject | None:
        """Wait for another operation to promote a shared component.

        The method never reads or returns partial bytes.  A bounded timeout
        lets the caller classify an owner outage as retryable and recover on a
        subsequent fenced attempt.
        """

        if not isinstance(operation, OperationBinding):
            raise TypeError("package wait operation is invalid")
        digest = _raw_digest(digest)
        if type(size) is not int or size < 0:
            raise ValueError("package wait size is invalid")
        if not callable(cancelled):
            raise TypeError("package wait cancellation callback is invalid")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("package wait timeout is invalid")
        started = time.monotonic()
        while time.monotonic() - started < timeout_seconds:
            if cancelled():
                return None
            if deadline is not None and hasattr(deadline, "check"):
                try:
                    deadline.check()
                except DeadlineBindingError:
                    return None
            cached = self.lookup(digest, size)
            if cached is not None:
                return cached
            time.sleep(0.02)
        return self.lookup(digest, size)

    def release_reservation(self, reservation: Reservation) -> None:
        if not isinstance(reservation, Reservation):
            raise TypeError("package reservation is invalid")
        with self._state.transaction() as connection:
            self._state.assert_binding(connection, reservation.binding)
            row = connection.execute(
                "SELECT attempt, fence, bytes_reserved FROM reservations "
                "WHERE operation_id = ?",
                (reservation.operation_id,),
            ).fetchone()
            if row is None:
                return
            if (
                row["attempt"] != reservation.attempt
                or row["fence"] != reservation.fence
                or row["bytes_reserved"] != reservation.bytes_reserved
            ):
                raise PackageStateConflict(
                    "capacity reservation disagrees with operation fence"
                )
            connection.execute(
                "DELETE FROM reservations WHERE operation_id = ?",
                (reservation.operation_id,),
            )

    def begin_component(
        self,
        reservation: Reservation,
        descriptor: object,
    ) -> DownloadRecord:
        if not isinstance(reservation, Reservation):
            raise TypeError("package reservation is invalid")
        descriptor, component_name = _component_descriptor(descriptor)
        partial_created: str | None = None
        try:
            with self._state.transaction() as connection:
                self._state.assert_binding(connection, reservation.binding)
                row = connection.execute(
                    "SELECT attempt, fence, bytes_reserved FROM reservations "
                    "WHERE operation_id = ?",
                    (reservation.operation_id,),
                ).fetchone()
                if row is None or (
                    row["attempt"] != reservation.attempt
                    or row["fence"] != reservation.fence
                    or row["bytes_reserved"] != reservation.bytes_reserved
                ):
                    raise PackageStateConflict(
                        "package reservation ownership is invalid"
                    )
                component = connection.execute(
                    "SELECT name, size, kind, state, relative_name, operation_id, "
                    "attempt, fence "
                    "FROM components WHERE digest = ?",
                    (descriptor.digest,),
                ).fetchone()
                if component is not None:
                    if (
                        component["size"] != descriptor.size
                        or component["kind"] != descriptor.kind
                    ):
                        raise PackageStoreError(
                            "component digest metadata is inconsistent"
                        )
                    if component["state"] == "complete":
                        return _complete_record(reservation, descriptor, component_name)
                    partial = connection.execute(
                        "SELECT operation_id, attempt, fence, partial_name, device, inode, "
                        "ctime_ns, bytes_written, validator_etag, validator_last_modified "
                        "FROM partials WHERE digest = ?",
                        (descriptor.digest,),
                    ).fetchone()
                    if partial is None:
                        raise PackageStoreError("component partial journal is missing")
                    if (
                        partial["operation_id"] == reservation.operation_id
                        and partial["attempt"] == reservation.attempt
                        and partial["fence"] == reservation.fence
                    ):
                        return _partial_record(
                            reservation, descriptor, component_name, partial
                        )
                    if (
                        partial["operation_id"] == reservation.operation_id
                        and partial["attempt"] < reservation.attempt
                    ):
                        adopted = _partial_record(
                            reservation, descriptor, component_name, partial
                        )
                        descriptor_fd = self._open_partial(adopted, writable=False)
                        os.close(descriptor_fd)
                        connection.execute(
                            "UPDATE partials SET attempt=?, fence=? WHERE digest=?",
                            (
                                reservation.attempt,
                                reservation.fence,
                                descriptor.digest,
                            ),
                        )
                        connection.execute(
                            "UPDATE components SET attempt=?, fence=? WHERE digest=?",
                            (
                                reservation.attempt,
                                reservation.fence,
                                descriptor.digest,
                            ),
                        )
                        return adopted
                    return DownloadRecord(
                        **_record_identity(reservation, descriptor, component_name),
                        partial_name="",
                        device=0,
                        inode=0,
                        ctime_ns=0,
                        bytes_completed=partial["bytes_written"],
                        validators=_validators(partial),
                        state="shared",
                    )
                if descriptor.size > reservation.bytes_reserved:
                    raise PackageCapacityError(
                        "component exceeds its capacity reservation"
                    )
                partial_name, device, inode, ctime_ns = self._create_partial(
                    reservation,
                    descriptor,
                )
                partial_created = partial_name
                connection.execute(
                    "INSERT INTO components "
                    "(digest, name, size, kind, state, relative_name, operation_id, "
                    "attempt, fence) VALUES (?, ?, ?, ?, 'partial', NULL, ?, ?, ?)",
                    (
                        descriptor.digest,
                        component_name,
                        descriptor.size,
                        descriptor.kind,
                        reservation.operation_id,
                        reservation.attempt,
                        reservation.fence,
                    ),
                )
                connection.execute(
                    "INSERT INTO partials "
                    "(digest, operation_id, attempt, fence, partial_name, device, inode, "
                    "ctime_ns, bytes_written, validator_etag, validator_last_modified) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)",
                    (
                        descriptor.digest,
                        reservation.operation_id,
                        reservation.attempt,
                        reservation.fence,
                        partial_name,
                        device,
                        inode,
                        ctime_ns,
                    ),
                )
            return DownloadRecord(
                **_record_identity(reservation, descriptor, component_name),
                partial_name=partial_name,
                device=device,
                inode=inode,
                ctime_ns=ctime_ns,
                bytes_completed=0,
                validators=Validators(),
                state="partial",
            )
        except Exception:
            if partial_created is not None:
                _unlink_optional(self._root / "partials", partial_created)
            raise

    def write_partial(
        self,
        record: DownloadRecord,
        content: bytes,
        *,
        offset: int = 0,
    ) -> DownloadRecord:
        if not isinstance(record, DownloadRecord) or record.state != "partial":
            raise PackageStoreError("component partial ownership is invalid")
        if not isinstance(content, bytes):
            raise TypeError("component partial content must be bytes")
        if type(offset) is not int or offset < 0 or offset + len(content) > record.size:
            raise ValueError("component partial offset is invalid")
        try:
            with self._state.transaction() as connection:
                self._state.assert_binding(connection, record.binding)
                partial = self._partial_row(connection, record)
                current = replace(
                    record,
                    ctime_ns=partial["ctime_ns"],
                    bytes_completed=partial["bytes_written"],
                    validators=_validators(partial),
                )
                if offset != partial["bytes_written"] and not (
                    offset == 0 and partial["bytes_written"] == 0
                ):
                    raise PackageStoreError("component partial offset is stale")
                descriptor = self._open_partial(current, writable=True)
                try:
                    if offset == 0:
                        os.ftruncate(descriptor, 0)
                    os.lseek(descriptor, offset, os.SEEK_SET)
                    view = memoryview(content)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    os.fsync(descriptor)
                    ctime_ns = os.fstat(descriptor).st_ctime_ns
                finally:
                    os.close(descriptor)
                completed = offset + len(content)
                connection.execute(
                    "UPDATE partials SET bytes_written = ?, ctime_ns = ? WHERE digest = ?",
                    (completed, ctime_ns, record.digest),
                )
        except PackageStateConflict:
            raise
        except PackageStoreError:
            raise
        except OSError as error:
            raise PackageStoreError(
                "component partial cannot be written safely"
            ) from error
        return replace(record, bytes_completed=completed, ctime_ns=ctime_ns)

    def append_partial(
        self,
        record: DownloadRecord,
        content: bytes,
        *,
        validators: Validators | None = None,
    ) -> DownloadRecord:
        current = self._refresh_record(record)
        updated = self.write_partial(current, content, offset=current.bytes_completed)
        if validators is not None:
            return self.checkpoint(updated, updated.bytes_completed, validators)
        return updated

    def checkpoint(
        self,
        record: DownloadRecord,
        bytes_completed: int,
        validators: Validators,
    ) -> DownloadRecord:
        if type(bytes_completed) is not int or not 0 <= bytes_completed <= record.size:
            raise ValueError("component checkpoint offset is invalid")
        if not isinstance(validators, Validators):
            raise TypeError("component checkpoint validators are invalid")
        with self._state.transaction() as connection:
            self._state.assert_binding(connection, record.binding)
            row = self._partial_row(connection, record)
            current = replace(
                record,
                ctime_ns=row["ctime_ns"],
                bytes_completed=row["bytes_written"],
                validators=_validators(row),
            )
            if row["bytes_written"] != bytes_completed:
                raise PackageStoreError("component checkpoint offset is stale")
            descriptor = self._open_partial(current, writable=False)
            try:
                if os.fstat(descriptor).st_size != bytes_completed:
                    raise PackageStoreError("component checkpoint size is invalid")
            finally:
                os.close(descriptor)
            connection.execute(
                "UPDATE partials SET validator_etag = ?, validator_last_modified = ? "
                "WHERE digest = ?",
                (validators.etag, validators.last_modified, record.digest),
            )
        return replace(current, validators=validators)

    def iter_partial(self, record: DownloadRecord):
        current = self._refresh_record(record)
        descriptor = self._open_partial(current, writable=False)
        try:
            remaining = current.bytes_completed
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise PackageStoreError("component partial is truncated")
                remaining -= len(chunk)
                yield chunk
            if os.read(descriptor, 1):
                raise PackageStoreError("component partial exceeds its checkpoint")
        finally:
            os.close(descriptor)

    def resume_component(
        self,
        binding: OperationBinding,
        digest: str,
    ) -> DownloadRecord:
        digest = _raw_digest(digest)
        with self._state.transaction() as connection:
            self._state.assert_binding(connection, binding)
            component = connection.execute(
                "SELECT name, size, kind, state FROM components WHERE digest = ?",
                (digest,),
            ).fetchone()
            if component is None or component["state"] != "partial":
                raise PackageStoreError("component partial is unavailable")
            row = connection.execute(
                "SELECT operation_id, attempt, fence, partial_name, device, inode, "
                "ctime_ns, bytes_written, validator_etag, validator_last_modified "
                "FROM partials WHERE digest = ?",
                (digest,),
            ).fetchone()
            if row is None:
                raise PackageStoreError("component partial journal is missing")
            reservation = Reservation(
                binding.job_id,
                binding.operation_id,
                binding.attempt,
                binding.fence,
                binding.node_id,
                component["size"],
            )
            descriptor = ComponentDescriptor(
                digest, component["size"], component["kind"]
            )
            return _partial_record(
                reservation,
                descriptor,
                component["name"],
                row,
            )

    def reset_partial(self, record: DownloadRecord, reason: str) -> DownloadRecord:
        return self._reset_partial(record, reason)

    def quarantine_partial(self, record: DownloadRecord, reason: str) -> DownloadRecord:
        return self._reset_partial(record, reason)

    def pause(self, record: DownloadRecord) -> None:
        current = self._refresh_record(record)
        descriptor = self._open_partial(current, writable=False)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def release(self, reservation: Reservation) -> None:
        self.release_reservation(reservation)

    def promote_component(
        self,
        record: DownloadRecord,
        verified_digest: str,
    ) -> StoreObject:
        if not isinstance(record, DownloadRecord):
            raise TypeError("component download record is invalid")
        _digest(verified_digest)
        if verified_digest != record.digest:
            raise PackageStoreError("verified component digest is inconsistent")
        if record.state == "complete":
            found = self.lookup(record.digest)
            if found is None:
                raise PackageStoreError("verified component is unavailable")
            return found
        if record.state != "partial":
            found = self.lookup(record.digest)
            if found is None:
                raise PackageStoreError("shared component download is incomplete")
            return found
        record = self._refresh_record(record)
        lock = self._digest_lock(record.digest)
        try:
            existing = self.lookup(record.digest)
            if existing is not None:
                self._complete_partial_journal(record, existing)
                return existing
            descriptor = self._open_partial(record, writable=False)
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_size != record.size:
                    raise PackageStoreError("component partial size is invalid")
                actual = _hash_descriptor(descriptor)
                if actual != record.digest:
                    raise PackageStoreError("component partial digest is invalid")
                os.fsync(descriptor)
                self._crash_hook("after-file-fsync")
                os.fchmod(descriptor, 0o444)
            finally:
                os.close(descriptor)
            objects_fd = _open_directory(self._root / "objects" / "sha256")
            partials_fd = _open_directory(self._root / "partials")
            try:
                try:
                    os.stat(record.digest, dir_fd=objects_fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.rename(
                        record.partial_name,
                        record.digest,
                        src_dir_fd=partials_fd,
                        dst_dir_fd=objects_fd,
                    )
                else:
                    _validate_object_at(
                        objects_fd,
                        record.digest,
                        size=record.size,
                        digest=record.digest,
                    )
                    os.unlink(record.partial_name, dir_fd=partials_fd)
                self._crash_hook("after-rename")
                os.fsync(objects_fd)
                os.fsync(partials_fd)
                self._crash_hook("after-directory-fsync")
            finally:
                os.close(partials_fd)
                os.close(objects_fd)
            stored = StoreObject(
                digest=record.digest,
                size=record.size,
                kind=record.kind,
                relative_name=f"objects/sha256/{record.digest}",
            )
            self._complete_partial_journal(record, stored, partial_missing=True)
            self._crash_hook("after-db-commit")
            return stored
        except PackageStateConflict:
            raise
        except PackageStoreError:
            raise
        except OSError as error:
            raise PackageStoreError("component promotion failed safely") from error
        finally:
            os.close(lock)

    def lookup(self, digest: str, size: int | None = None) -> StoreObject | None:
        digest = _raw_digest(digest)
        if size is not None and (type(size) is not int or size < 0):
            raise ValueError("component lookup size is invalid")
        with self._state.transaction() as connection:
            row = connection.execute(
                "SELECT size, kind, state, relative_name FROM components WHERE digest = ?",
                (digest,),
            ).fetchone()
            if row is None or row["state"] != "complete":
                return None
            if size is not None and row["size"] != size:
                raise PackageStoreError("verified component size is inconsistent")
            expected_name = f"objects/sha256/{digest}"
            if row["relative_name"] != expected_name:
                raise PackageStoreError("verified component path is invalid")
            objects_fd = _open_directory(self._root / "objects" / "sha256")
            try:
                _validate_object_at(
                    objects_fd,
                    digest,
                    size=row["size"],
                    digest=digest,
                )
            finally:
                os.close(objects_fd)
            return StoreObject(digest, row["size"], row["kind"], expected_name)

    def record_derived(
        self,
        binding: OperationBinding,
        derivation_digest: str,
        object_digest: str,
    ) -> None:
        stored = self.lookup(object_digest)
        if stored is None:
            raise PackageStoreError("derived store object is unavailable")
        self._state.record_derived(
            binding, _raw_digest(derivation_digest), stored.digest
        )

    def lookup_derived(self, derivation_digest: str) -> StoreObject | None:
        object_digest = self._state.lookup_derived(_raw_digest(derivation_digest))
        return None if object_digest is None else self.lookup(object_digest)

    def is_immutable(self, stored: StoreObject) -> bool:
        try:
            return self.lookup(stored.digest) == stored
        except (OSError, PackageStoreError):
            return False

    def quarantine(self, stored: StoreObject, binding: OperationBinding) -> None:
        if not isinstance(stored, StoreObject):
            raise TypeError("store object is invalid")
        self.quarantine_corrupt(binding, stored.digest)

    def quarantine_corrupt(
        self,
        binding: OperationBinding,
        digest: str,
    ) -> str:
        digest = _raw_digest(digest)
        self._state.operation(binding)
        if digest in self._state.reachable_objects(now_ns=time.time_ns()):
            raise PackageStoreError("reachable store object cannot be quarantined")
        lock = self._digest_lock(digest)
        try:
            source_fd = _open_directory(self._root / "objects" / "sha256")
            quarantine_fd = _open_directory(self._root / "quarantine")
            quarantine_name = f"{digest}.{secrets.token_hex(8)}.corrupt"
            try:
                metadata = os.stat(digest, dir_fd=source_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or not _trusted_owner(metadata)
                    or stat.S_IMODE(metadata.st_mode) != 0o444
                ):
                    raise PackageStoreError("corrupt store object path is unsafe")
                os.rename(
                    digest,
                    quarantine_name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=quarantine_fd,
                )
                os.fsync(source_fd)
                os.fsync(quarantine_fd)
            except OSError as error:
                raise PackageStoreError(
                    "corrupt store object cannot be quarantined"
                ) from error
            finally:
                os.close(quarantine_fd)
                os.close(source_fd)
            self.forget_corrupt(binding, digest)
            return f"quarantine/{quarantine_name}"
        finally:
            os.close(lock)

    def forget_corrupt(self, binding: OperationBinding, digest: str) -> None:
        digest = _raw_digest(digest)
        path = self._root / "objects" / "sha256" / digest
        if path.exists() or path.is_symlink():
            raise PackageStoreError("corrupt store object must be quarantined first")
        self._state.forget_object(binding, digest)

    def object_path(self, stored: StoreObject) -> Path:
        if not isinstance(stored, StoreObject):
            raise TypeError("store object is invalid")
        found = self.lookup(stored.digest)
        if found != stored:
            raise PackageStoreError("store object receipt is stale")
        return self._root / "objects" / "sha256" / stored.digest

    def _logical_capacity(self) -> int:
        return (
            self._capacity_bytes
            if self._capacity_bytes is not None
            else _available_bytes(self._root)
        )

    def _create_partial(
        self,
        reservation: Reservation,
        descriptor: ComponentDescriptor,
    ) -> tuple[str, int, int, int]:
        name = (
            f"{reservation.operation_id.replace('-', '')}."
            f"{reservation.attempt}.{descriptor.digest}.{secrets.token_hex(8)}.partial"
        )
        directory = _open_directory(self._root / "partials")
        try:
            try:
                descriptor_fd = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
            except OSError as error:
                raise PackageStoreError(
                    "component partial cannot be created safely"
                ) from error
            try:
                metadata = os.fstat(descriptor_fd)
                _validate_partial_metadata(metadata, size=0)
                os.fsync(descriptor_fd)
            finally:
                os.close(descriptor_fd)
            os.fsync(directory)
            return name, metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns
        finally:
            os.close(directory)

    def _open_partial(self, record: DownloadRecord, *, writable: bool) -> int:
        directory = _open_directory(self._root / "partials")
        try:
            flags = (
                (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            if not writable:
                flags |= os.O_NONBLOCK
            try:
                descriptor = os.open(record.partial_name, flags, dir_fd=directory)
            except OSError as error:
                raise PackageStoreError(
                    "component partial is missing or unsafe"
                ) from error
        finally:
            os.close(directory)
        try:
            metadata = os.fstat(descriptor)
            _validate_partial_metadata(metadata)
            if (
                metadata.st_dev != record.device
                or metadata.st_ino != record.inode
                or metadata.st_ctime_ns != record.ctime_ns
            ):
                raise PackageStoreError("component partial inode changed")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _refresh_record(self, record: DownloadRecord) -> DownloadRecord:
        if not isinstance(record, DownloadRecord) or record.state != "partial":
            raise PackageStoreError("component partial ownership is invalid")
        with self._state.transaction() as connection:
            self._state.assert_binding(connection, record.binding)
            row = self._partial_row(connection, record)
            current = replace(
                record,
                ctime_ns=row["ctime_ns"],
                bytes_completed=row["bytes_written"],
                validators=_validators(row),
            )
            descriptor = self._open_partial(current, writable=False)
            os.close(descriptor)
            return current

    def _reset_partial(self, record: DownloadRecord, reason: str) -> DownloadRecord:
        if not isinstance(reason, str) or _KIND.fullmatch(reason) is None:
            raise ValueError("partial quarantine reason is invalid")
        current = self._refresh_record(record)
        partials_fd = _open_directory(self._root / "partials")
        quarantine_fd = _open_directory(self._root / "quarantine")
        quarantine_name = f"{record.digest}.{reason}.{secrets.token_hex(8)}.partial"
        try:
            os.rename(
                current.partial_name,
                quarantine_name,
                src_dir_fd=partials_fd,
                dst_dir_fd=quarantine_fd,
            )
            os.fsync(partials_fd)
            os.fsync(quarantine_fd)
        finally:
            os.close(quarantine_fd)
            os.close(partials_fd)
        reservation = Reservation(
            current.job_id,
            current.operation_id,
            current.attempt,
            current.fence,
            current.node_id,
            current.size,
        )
        descriptor = ComponentDescriptor(current.digest, current.size, current.kind)
        partial_name, device, inode, ctime_ns = self._create_partial(
            reservation, descriptor
        )
        with self._state.transaction() as connection:
            self._state.assert_binding(connection, current.binding)
            row = self._partial_row(connection, current)
            if row["partial_name"] != current.partial_name:
                raise PackageStoreError("component partial ownership is invalid")
            connection.execute(
                "UPDATE partials SET partial_name=?, device=?, inode=?, ctime_ns=?, "
                "bytes_written=0, validator_etag=NULL, validator_last_modified=NULL "
                "WHERE digest=?",
                (partial_name, device, inode, ctime_ns, current.digest),
            )
        return replace(
            current,
            partial_name=partial_name,
            device=device,
            inode=inode,
            ctime_ns=ctime_ns,
            bytes_completed=0,
            validators=Validators(),
        )

    def _partial_row(self, connection, record: DownloadRecord):
        row = connection.execute(
            "SELECT operation_id, attempt, fence, partial_name, device, inode, "
            "ctime_ns, bytes_written, validator_etag, validator_last_modified "
            "FROM partials WHERE digest = ?",
            (record.digest,),
        ).fetchone()
        if row is None or any(
            (
                row["operation_id"] != record.operation_id,
                row["attempt"] != record.attempt,
                row["fence"] != record.fence,
                row["partial_name"] != record.partial_name,
                row["device"] != record.device,
                row["inode"] != record.inode,
            )
        ):
            raise PackageStoreError("component partial ownership is invalid")
        return row

    def _complete_partial_journal(
        self,
        record: DownloadRecord,
        stored: StoreObject,
        *,
        partial_missing: bool = False,
    ) -> None:
        with self._state.transaction() as connection:
            self._state.assert_binding(connection, record.binding)
            partial = connection.execute(
                "SELECT operation_id, attempt, fence FROM partials WHERE digest = ?",
                (record.digest,),
            ).fetchone()
            if partial is None:
                component = connection.execute(
                    "SELECT state FROM components WHERE digest = ?", (record.digest,)
                ).fetchone()
                if component is not None and component["state"] == "complete":
                    return
                raise PackageStoreError("component partial journal is missing")
            if (
                partial["operation_id"] != record.operation_id
                or partial["attempt"] != record.attempt
                or partial["fence"] != record.fence
            ):
                raise PackageStoreError("component partial ownership is invalid")
            if not partial_missing:
                _unlink_optional(self._root / "partials", record.partial_name)
            connection.execute(
                "DELETE FROM partials WHERE digest = ?", (record.digest,)
            )
            connection.execute(
                "UPDATE components SET state = 'complete', relative_name = ?, "
                "operation_id = NULL, attempt = NULL, fence = NULL WHERE digest = ?",
                (stored.relative_name, record.digest),
            )

    def _digest_lock(self, digest: str) -> int:
        directory = _open_directory(self._root / "locks")
        try:
            try:
                descriptor = os.open(
                    f"{digest}.lock",
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
            except OSError as error:
                raise PackageStoreError("component lock is unsafe") from error
        finally:
            os.close(directory)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or not _trusted_owner(metadata)
            ):
                raise PackageStoreError("component lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _recover(self) -> None:
        with self._state.transaction() as connection:
            rows = connection.execute(
                "SELECT c.digest, c.size, c.kind, p.partial_name, p.device, p.inode, "
                "p.ctime_ns "
                "FROM components c JOIN partials p ON p.digest = c.digest "
                "WHERE c.state = 'partial'"
            ).fetchall()
            for row in rows:
                object_path = self._root / "objects" / "sha256" / row["digest"]
                partial_path = self._root / "partials" / row["partial_name"]
                if object_path.exists():
                    objects_fd = _open_directory(object_path.parent)
                    try:
                        _validate_object_at(
                            objects_fd,
                            row["digest"],
                            size=row["size"],
                            digest=row["digest"],
                        )
                    finally:
                        os.close(objects_fd)
                    if partial_path.exists() or partial_path.is_symlink():
                        _unlink_optional(partial_path.parent, partial_path.name)
                    connection.execute(
                        "DELETE FROM partials WHERE digest = ?", (row["digest"],)
                    )
                    connection.execute(
                        "UPDATE components SET state='complete', relative_name=?, "
                        "operation_id=NULL, attempt=NULL, fence=NULL WHERE digest=?",
                        (f"objects/sha256/{row['digest']}", row["digest"]),
                    )
                    continue
                try:
                    metadata = os.lstat(partial_path)
                except OSError as error:
                    raise PackageStoreError(
                        "journaled component bytes are missing"
                    ) from error
                _validate_partial_metadata(metadata)
                if (
                    metadata.st_dev != row["device"]
                    or metadata.st_ino != row["inode"]
                    or metadata.st_ctime_ns != row["ctime_ns"]
                ):
                    raise PackageStoreError("journaled component partial inode changed")


def _record_identity(
    reservation: Reservation,
    descriptor: ComponentDescriptor,
    component_name: str,
) -> dict[str, object]:
    return {
        "job_id": reservation.job_id,
        "operation_id": reservation.operation_id,
        "attempt": reservation.attempt,
        "fence": reservation.fence,
        "node_id": reservation.node_id,
        "component": component_name,
        "digest": descriptor.digest,
        "size": descriptor.size,
        "kind": descriptor.kind,
    }


def _complete_record(
    reservation: Reservation,
    descriptor: ComponentDescriptor,
    component_name: str,
) -> DownloadRecord:
    return DownloadRecord(
        **_record_identity(reservation, descriptor, component_name),
        partial_name="",
        device=0,
        inode=0,
        ctime_ns=0,
        bytes_completed=descriptor.size,
        validators=Validators(),
        state="complete",
    )


def _partial_record(
    reservation,
    descriptor,
    component_name: str,
    row,
) -> DownloadRecord:
    return DownloadRecord(
        **_record_identity(reservation, descriptor, component_name),
        partial_name=row["partial_name"],
        device=row["device"],
        inode=row["inode"],
        ctime_ns=row["ctime_ns"],
        bytes_completed=row["bytes_written"],
        validators=_validators(row),
        state="partial",
    )


def _component_descriptor(value: object) -> tuple[ComponentDescriptor, str]:
    if isinstance(value, ComponentDescriptor):
        return value, value.digest
    digest = getattr(value, "digest", None)
    if isinstance(digest, str):
        digest = digest.removeprefix("sha256:")
    descriptor = ComponentDescriptor(
        digest=digest,
        size=getattr(value, "size", None),
        kind=getattr(value, "kind", None),
    )
    name = getattr(value, "name", descriptor.digest)
    if not isinstance(name, str) or not 1 <= len(name) <= 128:
        raise ValueError("component name is invalid")
    return descriptor, name


def _validators(row) -> Validators:
    return Validators(
        etag=row["validator_etag"],
        last_modified=row["validator_last_modified"],
    )


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("component digest is invalid")
    return value


def _raw_digest(value: object) -> str:
    if isinstance(value, str):
        value = value.removeprefix("sha256:")
    return _digest(value)


def _trusted_owner(metadata: os.stat_result) -> bool:
    return metadata.st_uid in {0, os.geteuid()}


def _validate_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not _trusted_owner(metadata)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PackageStoreError("package store directory is unsafe")


def _open_directory(path: Path) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise PackageStoreError("package store directory is unsafe") from error
    try:
        _validate_directory(os.fstat(descriptor))
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _ensure_layout(root: Path) -> None:
    root_fd = _open_directory(root)
    try:
        for name in ("objects", "partials", "locks", "quarantine"):
            try:
                os.mkdir(name, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            try:
                _validate_directory(os.fstat(child))
            finally:
                os.close(child)
        objects = os.open(
            "objects",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            try:
                os.mkdir("sha256", 0o700, dir_fd=objects)
            except FileExistsError:
                pass
            sha = os.open(
                "sha256",
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=objects,
            )
            try:
                _validate_directory(os.fstat(sha))
            finally:
                os.close(sha)
        finally:
            os.close(objects)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _validate_partial_metadata(
    metadata: os.stat_result,
    *,
    size: int | None = None,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not _trusted_owner(metadata)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (size is not None and metadata.st_size != size)
    ):
        raise PackageStoreError("component partial is unsafe")


def _validate_object_at(
    directory: int,
    name: str,
    *,
    size: int,
    digest: str,
) -> None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory,
        )
    except OSError as error:
        raise PackageStoreError("verified component is missing or unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not _trusted_owner(before)
            or stat.S_IMODE(before.st_mode) != 0o444
            or before.st_size != size
            or _hash_descriptor(descriptor) != digest
        ):
            raise PackageStoreError("verified component is unsafe or corrupt")
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after):
            raise PackageStoreError("verified component changed while being read")
    finally:
        os.close(descriptor)


def _hash_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _available_bytes(root: Path) -> int:
    values = os.statvfs(root)
    return values.f_bavail * values.f_frsize


def _unlink_optional(parent: Path, name: str) -> None:
    directory = _open_directory(parent)
    try:
        try:
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode):
            raise PackageStoreError("component partial path is unsafe")
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
