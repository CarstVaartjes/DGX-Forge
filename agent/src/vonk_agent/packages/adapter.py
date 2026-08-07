"""Digest-selected, catalog-free workload adapter ABI v1."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ..deadlines import DeadlineBindingError, MonotonicDeadline

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_RESULT_FIELDS = {
    "schema_version",
    "operation",
    "status",
    "evidence_digest",
    "job_id",
    "operation_id",
    "attempt",
    "fence",
    "release_digest",
    "generation",
}
_FIXED_ENVIRONMENT = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
    "PYTHONNOUSERSITE": "1",
}


class AdapterValidationError(ValueError):
    """Adapter content or ABI data failed closed validation."""


class AdapterExecutionError(RuntimeError):
    """Adapter execution failed without exposing adapter output."""


class AdapterOperation(StrEnum):
    PREPARE = "prepare"
    VERIFY = "verify"
    START = "start"
    HEALTH = "health"
    INFER = "infer"
    STOP = "stop"
    VERIFY_RELEASE = "verify-release"


@dataclass(frozen=True)
class AdapterArtifact:
    path: Path
    digest: str
    size: int

    def __post_init__(self) -> None:
        path = Path(self.path)
        if not path.is_absolute():
            raise AdapterValidationError("adapter path must be absolute")
        _digest(self.digest, "adapter digest")
        if (
            not isinstance(self.size, int)
            or isinstance(self.size, bool)
            or not 1 <= self.size <= 256 * 1024 * 1024
        ):
            raise AdapterValidationError("adapter size is invalid")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True)
class AdapterInvocation:
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    release_digest: str
    generation: str
    node_id: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.job_id, "job ID"),
            (self.operation_id, "operation ID"),
            (self.fence, "operation fence"),
        ):
            if not isinstance(value, str) or not _UUID.fullmatch(value):
                raise AdapterValidationError(f"{name} is invalid")
        if (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or not 1 <= self.attempt <= 2**31 - 1
        ):
            raise AdapterValidationError("attempt is invalid")
        _digest(self.release_digest, "release digest")
        _token(self.generation, "generation")
        if not isinstance(self.node_id, str) or not _NODE_ID.fullmatch(self.node_id):
            raise AdapterValidationError("node ID is invalid")


@dataclass(frozen=True)
class AdapterEvidence:
    operation: AdapterOperation
    status: str
    release_digest: str
    generation: str
    fence: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if type(self.operation) is not AdapterOperation:
            raise AdapterValidationError("adapter evidence operation is invalid")
        _token(self.status, "adapter status")
        _digest(self.release_digest, "release digest")
        _token(self.generation, "generation")
        if not isinstance(self.fence, str) or not _UUID.fullmatch(self.fence):
            raise AdapterValidationError("operation fence is invalid")
        _digest(self.evidence_digest, "evidence digest")


class AdapterProcess(Protocol):
    def run(
        self,
        executable_fd: int,
        cwd_fd: int,
        stdin: bytes,
        timeout_seconds: int,
        output_limit_bytes: int,
        deadline: MonotonicDeadline,
    ) -> bytes: ...


class AdapterExecutor:
    """Execute any signed adapter digest through one stable ABI."""

    def __init__(
        self,
        artifact: AdapterArtifact,
        generation_root: Path,
        *,
        process: AdapterProcess | None = None,
        timeout_seconds: int = 60,
        output_limit_bytes: int = 64 * 1024,
    ) -> None:
        if type(artifact) is not AdapterArtifact:
            raise AdapterValidationError("adapter artifact is invalid")
        root = Path(generation_root)
        if not root.is_absolute():
            raise AdapterValidationError("generation root must be absolute")
        try:
            relative = artifact.path.relative_to(root)
        except ValueError as error:
            raise AdapterValidationError(
                "adapter must be inside its generation"
            ) from error
        if any(
            part in {"", ".", ".."} for part in PurePosixPath(relative.as_posix()).parts
        ):
            raise AdapterValidationError("adapter relative path is invalid")
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 900
        ):
            raise AdapterValidationError("adapter timeout is invalid")
        if (
            not isinstance(output_limit_bytes, int)
            or isinstance(output_limit_bytes, bool)
            or not 1 <= output_limit_bytes <= 1024 * 1024
        ):
            raise AdapterValidationError("adapter output bound is invalid")
        self.artifact = artifact
        self.generation_root = root
        self._relative = relative
        self._process = process or _BoundedAdapterProcess()
        self._timeout_seconds = timeout_seconds
        self._output_limit_bytes = output_limit_bytes

    def execute(
        self,
        operation: AdapterOperation,
        invocation: AdapterInvocation,
        deadline: datetime | MonotonicDeadline,
    ) -> AdapterEvidence:
        if (
            type(operation) is not AdapterOperation
            or type(invocation) is not AdapterInvocation
        ):
            raise AdapterValidationError("adapter invocation is invalid")
        try:
            fixed_deadline = MonotonicDeadline.bind(deadline)
            fixed_deadline.check()
        except DeadlineBindingError as error:
            raise AdapterExecutionError("adapter deadline has elapsed") from error
        root_fd = -1
        executable_fd = -1
        try:
            root_fd = os.open(
                self.generation_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            executable_fd = _snapshot_adapter(
                root_fd,
                self._relative,
                self.artifact,
                fixed_deadline,
            )
            request = _canonical(
                {
                    "schema_version": 1,
                    "abi_version": 1,
                    "operation": operation.value,
                    "job_id": invocation.job_id,
                    "operation_id": invocation.operation_id,
                    "attempt": invocation.attempt,
                    "fence": invocation.fence,
                    "release_digest": invocation.release_digest,
                    "generation": invocation.generation,
                }
            )
            owned_executable_fd, executable_fd = executable_fd, -1
            result = self._process.run(
                owned_executable_fd,
                root_fd,
                request,
                self._timeout_seconds,
                self._output_limit_bytes,
                fixed_deadline,
            )
            return _parse_result(result, operation, invocation)
        except (AdapterValidationError, AdapterExecutionError):
            raise
        except Exception as error:
            raise AdapterExecutionError("adapter execution failed") from error
        finally:
            if executable_fd >= 0:
                os.close(executable_fd)
            if root_fd >= 0:
                os.close(root_fd)


def _snapshot_adapter(
    root_fd: int,
    relative: Path,
    artifact: AdapterArtifact,
    deadline: MonotonicDeadline,
) -> int:
    directory_fd = os.dup(root_fd)
    source_fd = -1
    snapshot_fd = -1
    try:
        parts = PurePosixPath(relative.as_posix()).parts
        for part in parts[:-1]:
            deadline.check()
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child
        deadline.check()
        source_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != artifact.size
            or before.st_mode & 0o022
            or not before.st_mode & 0o100
        ):
            raise AdapterValidationError("adapter metadata is unsafe")
        snapshot_fd = os.memfd_create(
            "dgx-package-adapter", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        os.fchmod(snapshot_fd, 0o500)
        digest = hashlib.sha256()
        remaining = artifact.size
        while remaining:
            deadline.check()
            data = os.read(source_fd, min(64 * 1024, remaining))
            if not data:
                raise AdapterValidationError("adapter changed while being opened")
            digest.update(data)
            view = memoryview(data)
            while view:
                written = os.write(snapshot_fd, view)
                if written <= 0:
                    raise AdapterValidationError("adapter snapshot is incomplete")
                view = view[written:]
            remaining -= len(data)
        after = os.fstat(source_fd)
        if digest.hexdigest() != artifact.digest or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AdapterValidationError("adapter digest or stable identity is invalid")
        fcntl.fcntl(
            snapshot_fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE,
        )
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        result, snapshot_fd = snapshot_fd, -1
        return result
    except DeadlineBindingError as error:
        raise AdapterExecutionError("adapter deadline has elapsed") from error
    except AdapterValidationError:
        raise
    except OSError as error:
        raise AdapterValidationError("adapter executable is unsafe") from error
    finally:
        if snapshot_fd >= 0:
            os.close(snapshot_fd)
        if source_fd >= 0:
            os.close(source_fd)
        os.close(directory_fd)


class _BoundedAdapterProcess:
    def run(
        self,
        executable_fd: int,
        cwd_fd: int,
        stdin: bytes,
        timeout_seconds: int,
        output_limit_bytes: int,
        deadline: MonotonicDeadline,
    ) -> bytes:
        effective_deadline = min(
            time.monotonic() + timeout_seconds, deadline.absolute()
        )
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        output = bytearray()
        total = 0
        try:
            process = subprocess.Popen(
                [f"/proc/self/fd/{executable_fd}"],
                executable=f"/proc/self/fd/{executable_fd}",
                cwd=f"/proc/self/fd/{cwd_fd}",
                env=_FIXED_ENVIRONMENT,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=(executable_fd, cwd_fd),
                start_new_session=True,
                text=False,
                bufsize=0,
            )
            assert process.stdin is not None
            process.stdin.write(stdin)
            process.stdin.close()
            assert process.stdout is not None and process.stderr is not None
            for stream, capture in ((process.stdout, True), (process.stderr, False)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, capture)
            while selector.get_map():
                remaining = effective_deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterExecutionError("adapter execution timed out")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    total += len(chunk)
                    if total > output_limit_bytes:
                        raise AdapterExecutionError("adapter output exceeded its bound")
                    if key.data:
                        output.extend(chunk)
            remaining = effective_deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterExecutionError("adapter execution timed out")
            returncode = process.wait(timeout=remaining)
            if returncode != 0:
                raise AdapterExecutionError("adapter process failed")
            return bytes(output)
        except subprocess.TimeoutExpired as error:
            raise AdapterExecutionError("adapter execution timed out") from error
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            selector.close()
            os.close(executable_fd)


def _parse_result(
    raw: bytes, operation: AdapterOperation, invocation: AdapterInvocation
) -> AdapterEvidence:
    if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
        raise AdapterValidationError("adapter result is invalid")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except AdapterValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterValidationError("adapter result is invalid") from error
    if (
        not isinstance(document, dict)
        or set(document) != _RESULT_FIELDS
        or _canonical(document) != raw
        or document["schema_version"] != 1
        or isinstance(document["schema_version"], bool)
    ):
        raise AdapterValidationError("adapter result fields are invalid")
    if (
        document["job_id"] != invocation.job_id
        or document["operation"] != operation.value
        or document["operation_id"] != invocation.operation_id
        or document["attempt"] != invocation.attempt
        or isinstance(document["attempt"], bool)
        or document["fence"] != invocation.fence
        or document["release_digest"] != invocation.release_digest
        or document["generation"] != invocation.generation
    ):
        raise AdapterValidationError("adapter result operation binding does not match")
    return AdapterEvidence(
        operation,
        document["status"],
        document["release_digest"],
        document["generation"],
        document["fence"],
        document["evidence_digest"],
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterValidationError("adapter result contains duplicate fields")
        result[key] = value
    return result


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise AdapterValidationError(f"{name} is invalid")
    return value


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise AdapterValidationError(f"{name} is invalid")
    return value
