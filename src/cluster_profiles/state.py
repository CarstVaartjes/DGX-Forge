"""Atomic local controller state and safe cross-process switch locking."""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

_STATUSES = frozenset(
    ("stopped", "transitioning", "active", "degraded", "stopped-after-reboot")
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FENCED_RECOVERY = (
    "incomplete transition is quarantined; manual recovery required: inspect and "
    "stop remote workloads, persist a safe stopped or degraded state, then remove "
    "transition.fence; model and output data are preserved"
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
    return datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: str, source: Path) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise StateFormatError(f"malformed timestamp in {source}") from error
    if parsed.tzinfo is None:
        raise StateFormatError(f"timestamp in {source} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ControllerState:
    """Schema-versioned state for the developer-machine controller."""

    status: str
    active_profile: str | None
    target_profile: str | None
    restore_profile: str | None
    last_error: str | None
    active_profile_sha256: str | None = None
    active_definition_sha256: Mapping[str, str] = field(default_factory=dict)
    boot_ids: Mapping[str, str] = field(default_factory=dict)
    schema_version: int = 1
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported controller state schema version")
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported controller status: {self.status}")
        if self.active_profile_sha256 is not None and not _SHA256.fullmatch(
            self.active_profile_sha256
        ):
            raise ValueError("active profile fingerprint must be a lowercase SHA-256")
        if not isinstance(self.active_definition_sha256, Mapping):
            raise TypeError("active_definition_sha256 must be a mapping")
        if any(
            not isinstance(definition_id, str)
            or not definition_id
            or not isinstance(fingerprint, str)
            or not _SHA256.fullmatch(fingerprint)
            for definition_id, fingerprint in self.active_definition_sha256.items()
        ):
            raise ValueError(
                "active definition fingerprints must be lowercase SHA-256 values"
            )
        if self.active_profile is None and (
            self.active_profile_sha256 is not None or self.active_definition_sha256
        ):
            raise ValueError("stopped state cannot retain active fingerprints")
        if self.active_profile is not None and self.active_profile_sha256 is None:
            raise ValueError("an active profile requires its lowercase SHA-256 fingerprint")
        if not isinstance(self.boot_ids, Mapping):
            raise TypeError("boot_ids must be a mapping")
        if any(
            not isinstance(node, str) or not isinstance(boot_id, str)
            for node, boot_id in self.boot_ids.items()
        ):
            raise TypeError("boot_ids must map strings to strings")
        _parse_timestamp(self.updated_at, Path("controller state"))
        object.__setattr__(
            self,
            "active_definition_sha256",
            MappingProxyType(dict(sorted(self.active_definition_sha256.items()))),
        )
        object.__setattr__(self, "boot_ids", MappingProxyType(dict(self.boot_ids)))

    @classmethod
    def stopped(cls, *, boot_ids: Mapping[str, str] | None = None) -> ControllerState:
        return cls(
            status="stopped",
            active_profile=None,
            target_profile=None,
            restore_profile=None,
            last_error=None,
            active_profile_sha256=None,
            active_definition_sha256={},
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
            "active_profile_sha256": self.active_profile_sha256,
            "active_definition_sha256": dict(self.active_definition_sha256),
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
            "active_profile_sha256",
            "active_definition_sha256",
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
                active_profile_sha256=data["active_profile_sha256"],
                active_definition_sha256=data["active_definition_sha256"],
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
        self.transition_fence_path = self.directory / "transition.fence"
        self.stale_lock_seconds = stale_lock_seconds

    def load(self) -> ControllerState:
        if self.transition_fence_path.exists():
            return self._quarantined_state()
        if not self.state_path.exists():
            return ControllerState.stopped()
        try:
            with self.state_path.open(encoding="utf-8") as state_file:
                data = json.load(state_file)
        except (OSError, json.JSONDecodeError) as error:
            raise StateFormatError(f"cannot load {self.state_path}: {error}") from error
        state = ControllerState.from_dict(data, self.state_path)
        if self.transition_fence_path.exists():
            return self._quarantined_state()
        return state

    def save(self, state: ControllerState) -> None:
        if not isinstance(state, ControllerState):
            raise TypeError("state must be a ControllerState")
        self._ensure_directory()
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
            self._fsync_directory(self.directory)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def begin_transition(self) -> None:
        """Durably quarantine state publication before remote mutation."""
        self._ensure_directory()
        try:
            descriptor = os.open(
                self.transition_fence_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as error:
            raise StateError(
                "transition fence exists; manual recovery required"
            ) from error
        metadata = LockMetadata(os.getpid(), socket.gethostname(), _utc_now())
        with os.fdopen(descriptor, "w", encoding="utf-8") as fence_file:
            json.dump(
                metadata.to_dict(),
                fence_file,
                sort_keys=True,
                separators=(",", ":"),
            )
            fence_file.write("\n")
            fence_file.flush()
            os.fsync(fence_file.fileno())
        self._fsync_directory(self.directory)

    def finish_transition(self, state: ControllerState) -> None:
        """Durably save a final state before clearing its transition fence."""
        if state.status == "transitioning":
            raise ValueError("cannot finish a transition with transitioning state")
        self.save(state)
        self.transition_fence_path.unlink()
        try:
            self._fsync_directory(self.directory)
        except OSError:
            # The final state was already durably committed before unlink. If
            # the deletion is lost on a crash, the stale fence fails closed.
            pass

    def _ensure_directory(self) -> None:
        missing: list[Path] = []
        candidate = self.directory
        while not candidate.exists():
            missing.append(candidate)
            candidate = candidate.parent
        for directory in reversed(missing):
            directory.mkdir(exist_ok=True)
            self._fsync_directory(directory.parent)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _quarantined_state() -> ControllerState:
        return ControllerState(
            status="degraded",
            active_profile=None,
            target_profile=None,
            restore_profile=None,
            last_error=_FENCED_RECOVERY,
        )

    @contextmanager
    def acquire(self) -> Iterator[ControllerState]:
        self._ensure_directory()
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
            local_host = socket.gethostname()
            if metadata.host != local_host:
                raise LockNotStale(
                    f"lock belongs to different host {metadata.host}; "
                    "inspect it on that controller host"
                )
            if _pid_is_live(metadata.pid):
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
