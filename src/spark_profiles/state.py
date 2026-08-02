"""Atomic local controller state and safe cross-process switch locking."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import socket
import tempfile
from types import MappingProxyType
from typing import Iterator, Mapping


_STATUSES = frozenset(
    ("stopped", "transitioning", "active", "degraded", "stopped-after-reboot")
)


class StateError(RuntimeError):
    """Base class for persistent controller-state failures."""


class StateFormatError(StateError):
    """Raised when persisted state or lock metadata is malformed."""


class LockBusy(StateError):
    """Raised when another controller process holds the switch lock."""


class LockNotStale(StateError):
    """Raised when an explicit lock break is unsafe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: str, source: Path) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise StateFormatError(f"malformed timestamp in {source}") from error
    if parsed.tzinfo is None:
        raise StateFormatError(f"timestamp in {source} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ControllerState:
    """Schema-versioned state for the developer-machine controller."""

    status: str
    active_profile: str | None
    target_profile: str | None
    restore_profile: str | None
    last_error: str | None
    boot_ids: Mapping[str, str] = field(default_factory=dict)
    schema_version: int = 1
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported controller state schema version")
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported controller status: {self.status}")
        if not isinstance(self.boot_ids, Mapping):
            raise TypeError("boot_ids must be a mapping")
        if any(
            not isinstance(node, str) or not isinstance(boot_id, str)
            for node, boot_id in self.boot_ids.items()
        ):
            raise TypeError("boot_ids must map strings to strings")
        _parse_timestamp(self.updated_at, Path("controller state"))
        object.__setattr__(self, "boot_ids", MappingProxyType(dict(self.boot_ids)))

    @classmethod
    def stopped(cls, *, boot_ids: Mapping[str, str] | None = None) -> ControllerState:
        return cls(
            status="stopped",
            active_profile=None,
            target_profile=None,
            restore_profile=None,
            last_error=None,
            boot_ids=boot_ids or {},
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "active_profile": self.active_profile,
            "target_profile": self.target_profile,
            "restore_profile": self.restore_profile,
            "last_error": self.last_error,
            "boot_ids": dict(self.boot_ids),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: object, source: Path) -> ControllerState:
        if not isinstance(data, dict):
            raise StateFormatError(f"{source} must contain a JSON object")
        required = {
            "schema_version",
            "status",
            "active_profile",
            "target_profile",
            "restore_profile",
            "last_error",
            "boot_ids",
            "updated_at",
        }
        if set(data) != required:
            raise StateFormatError(f"{source} has invalid controller state fields")
        nullable_strings = (
            "active_profile",
            "target_profile",
            "restore_profile",
            "last_error",
        )
        if any(
            data[name] is not None and not isinstance(data[name], str)
            for name in nullable_strings
        ):
            raise StateFormatError(f"{source} has invalid profile or error fields")
        try:
            return cls(
                schema_version=data["schema_version"],
                status=data["status"],
                active_profile=data["active_profile"],
                target_profile=data["target_profile"],
                restore_profile=data["restore_profile"],
                last_error=data["last_error"],
                boot_ids=data["boot_ids"],
                updated_at=data["updated_at"],
            )
        except (TypeError, ValueError) as error:
            raise StateFormatError(f"invalid controller state in {source}: {error}") from error


@dataclass(frozen=True)
class LockMetadata:
    pid: int
    host: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object, source: Path) -> LockMetadata:
        if not isinstance(data, dict) or set(data) != {"pid", "host", "created_at"}:
            raise StateFormatError(f"malformed lock metadata in {source}")
        if (
            not isinstance(data["pid"], int)
            or isinstance(data["pid"], bool)
            or data["pid"] <= 0
            or not isinstance(data["host"], str)
            or not data["host"]
            or not isinstance(data["created_at"], str)
        ):
            raise StateFormatError(f"malformed lock metadata in {source}")
        _parse_timestamp(data["created_at"], source)
        return cls(**data)


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StateStore:
    """Persist state and serialize cluster transitions on the local host."""

    def __init__(self, directory: Path, *, stale_lock_seconds: int = 900) -> None:
        if stale_lock_seconds <= 0:
            raise ValueError("stale lock threshold must be positive")
        self.directory = Path(directory)
        self.state_path = self.directory / "state.json"
        self.lock_path = self.directory / "switch.lock"
        self.stale_lock_seconds = stale_lock_seconds

    def load(self) -> ControllerState:
        if not self.state_path.exists():
            return ControllerState.stopped()
        try:
            with self.state_path.open(encoding="utf-8") as state_file:
                data = json.load(state_file)
        except (OSError, json.JSONDecodeError) as error:
            raise StateFormatError(f"cannot load {self.state_path}: {error}") from error
        return ControllerState.from_dict(data, self.state_path)

    def save(self, state: ControllerState) -> None:
        if not isinstance(state, ControllerState):
            raise TypeError("state must be a ControllerState")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.directory, prefix=".state.json.", suffix=".tmp"
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(state.to_dict(), state_file, sort_keys=True, separators=(",", ":"))
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.state_path)
            temporary_path = None
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @contextmanager
    def acquire(self) -> Iterator[ControllerState]:
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LockBusy(f"switch lock is held: {self.lock_path}") from error
            metadata = LockMetadata(os.getpid(), socket.gethostname(), _utc_now())
            lock_file.seek(0)
            lock_file.truncate()
            json.dump(metadata.to_dict(), lock_file, sort_keys=True, separators=(",", ":"))
            lock_file.write("\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
            yield self.load()
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()

    def break_stale_lock(self) -> bool:
        """Remove only an unlocked, old lock whose recorded local PID is dead."""
        if not self.lock_path.exists():
            return False
        lock_file = self.lock_path.open("r+", encoding="utf-8")
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LockBusy(f"switch lock is currently held: {self.lock_path}") from error
            try:
                metadata = LockMetadata.from_dict(json.load(lock_file), self.lock_path)
            except json.JSONDecodeError as error:
                raise StateFormatError(f"malformed lock metadata in {self.lock_path}") from error
            if metadata.host == socket.gethostname() and _pid_is_live(metadata.pid):
                raise LockNotStale(f"lock records live PID {metadata.pid}")
            created = _parse_timestamp(metadata.created_at, self.lock_path)
            now = _parse_timestamp(_utc_now(), Path("current time"))
            age = (now - created).total_seconds()
            if age < self.stale_lock_seconds:
                raise LockNotStale(
                    f"lock is younger than {self.stale_lock_seconds} seconds"
                )
            self.lock_path.unlink()
            return True
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()
