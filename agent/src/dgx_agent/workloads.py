"""Compiled typed workload-adapter request boundary."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol

from .deadlines import DeadlineBindingError, MonotonicDeadline
from .probe import BoundedProcessRunner, ProcessRequest
from .releases import (
    ReleaseDescriptor,
    ReleaseMember,
    ReleaseRequest,
    verify_installed_release_fd,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_COMMON_FIELDS = frozenset(
    {"schema_version", "workload_id", "release_digest", "adapter_id"}
)


class WorkloadValidationError(ValueError):
    """A workload request or adapter result is invalid."""


class WorkloadExecutionError(RuntimeError):
    """A compiled workload adapter failed without exposing process output."""

    error_code = "workload_failed"


class WorkloadAction(StrEnum):
    PREPARE = "prepare"
    START = "start"
    STOP = "stop"
    HEALTH = "health"
    VERIFY = "verify"


class WorkloadDisposition(StrEnum):
    READY = "ready"
    SAFE_TO_RETRY = "safe-to-retry"
    COMPLETED = "completed"
    COMPENSATE = "compensate"
    OPERATOR_INTERVENTION = "operator-intervention"


_ACTION_FIELD = {
    WorkloadAction.PREPARE: "profile_digest",
    WorkloadAction.START: "preparation_digest",
    WorkloadAction.STOP: None,
    WorkloadAction.HEALTH: None,
    WorkloadAction.VERIFY: "expected_digest",
}
_EXECUTION_STATUSES = {
    WorkloadAction.PREPARE: frozenset({"prepared"}),
    WorkloadAction.START: frozenset({"started"}),
    WorkloadAction.STOP: frozenset({"stopped"}),
    WorkloadAction.HEALTH: frozenset({"healthy", "unhealthy"}),
    WorkloadAction.VERIFY: frozenset({"verified"}),
}


@dataclass(frozen=True)
class WorkloadRequest:
    schema_version: int
    action: WorkloadAction
    workload_id: str
    release_digest: str
    adapter_id: str
    operation_digest: str | None

    @classmethod
    def parse(
        cls, action: WorkloadAction, document: Mapping[str, Any]
    ) -> WorkloadRequest:
        if type(action) is not WorkloadAction or not isinstance(document, Mapping):
            raise WorkloadValidationError("workload action is invalid")
        action_field = _ACTION_FIELD[action]
        expected = _COMMON_FIELDS | ({action_field} if action_field else set())
        if set(document) != expected:
            raise WorkloadValidationError("workload request fields are invalid")
        if document["schema_version"] != 1 or isinstance(
            document["schema_version"], bool
        ):
            raise WorkloadValidationError("workload request version is invalid")
        workload_id = _token(document["workload_id"], "workload ID")
        adapter_id = _token(document["adapter_id"], "adapter ID")
        release_digest = _digest(document["release_digest"], "release digest")
        operation_digest = (
            _digest(document[action_field], action_field.replace("_", " "))
            if action_field
            else None
        )
        return cls(1, action, workload_id, release_digest, adapter_id, operation_digest)


@dataclass(frozen=True)
class WorkloadEvidence:
    status: str
    action: WorkloadAction
    workload_id: str
    release_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _token(self.status, "workload status")
        if type(self.action) is not WorkloadAction:
            raise WorkloadValidationError("workload evidence action is invalid")
        _token(self.workload_id, "workload ID")
        _digest(self.release_digest, "release digest")
        _digest(self.evidence_digest, "evidence digest")

    def to_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "action": self.action.value,
            "workload_id": self.workload_id,
            "release_digest": self.release_digest,
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True)
class WorkloadInspection:
    disposition: WorkloadDisposition
    evidence: WorkloadEvidence | None = None


@dataclass(frozen=True)
class CompiledAdapterPolicy:
    """A locally reviewed mapping from an adapter ID to one signed executable."""

    adapter_id: str
    executable_relative_path: str
    timeout_seconds: int
    output_limit_bytes: int
    allow_unprivileged_test_files: bool = False

    def __post_init__(self) -> None:
        _token(self.adapter_id, "adapter ID")
        path = PurePosixPath(self.executable_relative_path)
        if (
            not self.executable_relative_path
            or path.is_absolute()
            or str(path) != self.executable_relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise WorkloadValidationError("adapter executable path is invalid")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 300
        ):
            raise WorkloadValidationError("adapter timeout is invalid")
        if (
            not isinstance(self.output_limit_bytes, int)
            or isinstance(self.output_limit_bytes, bool)
            or not 1 <= self.output_limit_bytes <= 256 * 1024
        ):
            raise WorkloadValidationError("adapter output limit is invalid")


class WorkloadOperations:
    """Execute only compiled adapters authorized by an installed release receipt."""

    def __init__(
        self,
        releases_root: Path,
        release_trust: WorkloadReleaseTrustBoundary,
    ) -> None:
        self._initialize(releases_root, _PRODUCTION_POLICIES, release_trust)

    @classmethod
    def _for_test(
        cls,
        releases_root: Path,
        policies: Mapping[str, CompiledAdapterPolicy],
        release_trust: WorkloadReleaseTrustBoundary,
    ) -> WorkloadOperations:
        instance = object.__new__(cls)
        instance._initialize(releases_root, policies, release_trust)
        return instance

    def _initialize(
        self,
        releases_root: Path,
        policies: Mapping[str, CompiledAdapterPolicy],
        release_trust: WorkloadReleaseTrustBoundary,
    ) -> None:
        root = Path(releases_root)
        if not root.is_absolute():
            raise WorkloadValidationError("release root is invalid")
        checked: dict[str, CompiledAdapterPolicy] = {}
        for adapter_id, policy in policies.items():
            if adapter_id != policy.adapter_id or adapter_id in checked:
                raise WorkloadValidationError("adapter registry is invalid")
            checked[adapter_id] = policy
        self._releases_root = root
        self._policies = MappingProxyType(checked)
        if not callable(getattr(release_trust, "authorize", None)):
            raise WorkloadValidationError("release trust boundary is invalid")
        self._release_trust = release_trust
        self._runner = BoundedProcessRunner()

    @property
    def adapter_ids(self) -> tuple[str, ...]:
        return tuple(self._policies)

    def execute(
        self,
        request: WorkloadRequest,
        deadline: datetime | MonotonicDeadline,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
    ) -> WorkloadEvidence:
        _operation_binding(job_id, operation_id, attempt, fence)
        try:
            fixed_deadline = MonotonicDeadline.bind(deadline)
            fixed_deadline.check()
        except DeadlineBindingError as error:
            raise WorkloadExecutionError("workload deadline has elapsed") from error
        descriptor, policy, executable_fd, release_fd = self._open_adapter(
            request, fixed_deadline
        )
        try:
            outcome = self._run(
                request,
                policy,
                executable_fd,
                release_fd,
                self._execute_arguments(
                    request, job_id, operation_id, attempt, fence
                ),
                fixed_deadline,
            )
            if outcome.returncode != 0:
                raise WorkloadExecutionError("compiled workload adapter failed")
            document = _adapter_document(
                outcome.stdout,
                {
                    "schema_version", "status", "evidence_digest", "job_id",
                    "operation_id", "attempt", "fence",
                },
            )
            if (
                document["job_id"] != job_id
                or document["operation_id"] != operation_id
                or type(document["attempt"]) is not int
                or document["attempt"] != attempt
                or document["fence"] != fence
            ):
                raise WorkloadValidationError("adapter result operation binding does not match")
            if document["status"] not in _EXECUTION_STATUSES[request.action]:
                raise WorkloadValidationError("adapter status is invalid for action")
            return WorkloadEvidence(
                _token(document["status"], "workload status"),
                request.action,
                request.workload_id,
                descriptor.target_digest,
                _digest(document["evidence_digest"], "evidence digest"),
            )
        except (WorkloadValidationError, WorkloadExecutionError):
            raise
        except Exception as error:
            raise WorkloadExecutionError("compiled workload adapter failed") from error
        finally:
            os.close(executable_fd)
            os.close(release_fd)

    def inspect(
        self,
        request: WorkloadRequest,
        deadline: datetime | MonotonicDeadline,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
    ) -> WorkloadInspection:
        executable_fd = -1
        try:
            _operation_binding(job_id, operation_id, attempt, fence)
            fixed_deadline = MonotonicDeadline.bind(deadline)
            descriptor, policy, executable_fd, release_fd = self._open_adapter(
                request, fixed_deadline
            )
            outcome = self._run(
                request,
                policy,
                executable_fd,
                release_fd,
                self._inspection_arguments(
                    request, job_id, operation_id, attempt, fence
                ),
                fixed_deadline,
            )
            if outcome.returncode != 0:
                raise WorkloadExecutionError("compiled workload adapter inspection failed")
            document = _adapter_document(
                outcome.stdout,
                {
                    "schema_version", "disposition", "evidence_digest",
                    "job_id", "operation_id", "attempt", "fence",
                },
            )
            if (
                document["job_id"] != job_id
                or document["operation_id"] != operation_id
                or type(document["attempt"]) is not int
                or document["attempt"] != attempt
                or document["fence"] != fence
            ):
                raise WorkloadValidationError("adapter inspection fence does not match")
            try:
                disposition = WorkloadDisposition(document["disposition"])
            except (TypeError, ValueError) as error:
                raise WorkloadValidationError("adapter disposition is invalid") from error
            evidence = WorkloadEvidence(
                "inspected",
                request.action,
                request.workload_id,
                descriptor.target_digest,
                _digest(document["evidence_digest"], "evidence digest"),
            )
            return WorkloadInspection(disposition, evidence)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError):
            return WorkloadInspection(WorkloadDisposition.OPERATOR_INTERVENTION)
        finally:
            if executable_fd >= 0:
                os.close(executable_fd)
            if 'release_fd' in locals():
                os.close(release_fd)

    def _open_adapter(
        self, request: WorkloadRequest, deadline: MonotonicDeadline
    ) -> tuple[ReleaseDescriptor, CompiledAdapterPolicy, int, int]:
        if type(request) is not WorkloadRequest:
            raise WorkloadValidationError("workload request is invalid")
        policy = self._policies.get(request.adapter_id)
        if policy is None:
            raise WorkloadValidationError("workload adapter is not reviewed")
        release = self._releases_root / request.release_digest
        release_fd = -1
        try:
            _workload_deadline(deadline)
            release_fd = os.open(
                release,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            _workload_deadline(deadline)
            receipt_descriptor = verify_installed_release_fd(
                release_fd, deadline
            )
            _workload_deadline(deadline)
            descriptor = self._release_trust.authorize(
                ReleaseRequest(
                    1,
                    receipt_descriptor.target_name,
                    receipt_descriptor.oci_manifest_digest,
                    receipt_descriptor.target_digest,
                    receipt_descriptor.provenance_digest,
                    receipt_descriptor.adapter_id,
                ),
                deadline,
            )
            _workload_deadline(deadline)
            if descriptor != receipt_descriptor:
                raise WorkloadValidationError(
                    "installed release receipt is not signed authorization"
                )
        except WorkloadExecutionError:
            if release_fd >= 0:
                os.close(release_fd)
            raise
        except Exception as error:
            if release_fd >= 0:
                os.close(release_fd)
            _workload_deadline(deadline)
            raise WorkloadValidationError("workload release is not installed") from error
        try:
            if (
                descriptor.target_digest != request.release_digest
                or descriptor.adapter_id != request.adapter_id
            ):
                raise WorkloadValidationError("workload release does not match request")
            member = next(
                (
                    item
                    for item in descriptor.members
                    if item.path == policy.executable_relative_path
                ),
                None,
            )
            if member is None or member.mode != 0o500:
                raise WorkloadValidationError("workload adapter is not executable")
            descriptor_fd = _snapshot_release_member(release_fd, member, deadline)
            _workload_deadline(deadline)
        except Exception:
            os.close(release_fd)
            raise
        return descriptor, policy, descriptor_fd, release_fd

    def _run(
        self,
        request: WorkloadRequest,
        policy: CompiledAdapterPolicy,
        executable_fd: int,
        release_fd: int,
        arguments: tuple[str, ...],
        deadline: MonotonicDeadline,
    ):
        remaining = deadline.remaining()
        if remaining <= 0:
            raise WorkloadExecutionError("workload deadline has elapsed")
        request_value = ProcessRequest.fixed(
            argv=(f"/proc/self/fd/{executable_fd}", *arguments),
            cwd=Path(f"/proc/self/fd/{release_fd}"),
            timeout_seconds=min(float(policy.timeout_seconds), remaining),
            output_limit_bytes=policy.output_limit_bytes,
            executable_fd=executable_fd,
            absolute_deadline=deadline.absolute_monotonic,
            additional_fds=(release_fd,),
        )
        return self._runner.run(request_value)

    @staticmethod
    def _execute_arguments(
        request: WorkloadRequest,
        job_id: str | None = None,
        operation_id: str | None = None,
        attempt: int | None = None,
        fence: str | None = None,
    ) -> tuple[str, ...]:
        values = [request.action.value, "--workload-id", request.workload_id]
        field = _ACTION_FIELD[request.action]
        if field is not None:
            values.extend((f"--{field.replace('_', '-')}", request.operation_digest or ""))
        if job_id is not None:
            assert operation_id is not None and attempt is not None and fence is not None
            values.extend(
                (
                    "--job-id", job_id, "--operation-id", operation_id,
                    "--attempt", str(attempt), "--fence", fence,
                )
            )
        return tuple(values)

    @staticmethod
    def _inspection_arguments(
        request: WorkloadRequest,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
    ) -> tuple[str, ...]:
        execute = WorkloadOperations._execute_arguments(request)
        return (
            "inspect", "--action", execute[0], *execute[1:],
            "--job-id", job_id, "--operation-id", operation_id,
            "--attempt", str(attempt), "--fence", fence,
        )


def _adapter_document(raw: bytes, fields: set[str]) -> dict[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadValidationError("adapter result is invalid") from error
    if not isinstance(document, dict) or set(document) != fields:
        raise WorkloadValidationError("adapter result fields are invalid")
    if document["schema_version"] != 1 or isinstance(document["schema_version"], bool):
        raise WorkloadValidationError("adapter result version is invalid")
    return document


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise WorkloadValidationError("adapter result contains duplicate fields")
        document[key] = value
    return document


def _operation_binding(
    job_id: str | None,
    operation_id: str | None,
    attempt: int,
    fence: str,
) -> None:
    uuid = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
    )
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= 2**31 - 1
        or not isinstance(fence, str)
        or not uuid.fullmatch(fence)
        or (
            not isinstance(job_id, str)
            or not uuid.fullmatch(job_id)
            or not isinstance(operation_id, str)
            or not uuid.fullmatch(operation_id)
        )
    ):
        raise WorkloadValidationError("workload operation binding is invalid")


def _snapshot_release_member(
    root_fd: int, member: ReleaseMember, deadline: MonotonicDeadline
) -> int:
    """Snapshot one signed executable below a pinned installed-release dirfd."""
    _workload_deadline(deadline)
    directory_fd = os.dup(root_fd)
    source_fd = -1
    snapshot_fd = -1
    try:
        _workload_deadline(deadline)
        parts = PurePosixPath(member.path).parts
        for component in parts[:-1]:
            _workload_deadline(deadline)
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
            _workload_deadline(deadline)
        _workload_deadline(deadline)
        source_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        _workload_deadline(deadline)
        before = os.fstat(source_fd)
        _workload_deadline(deadline)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != member.uid
            or before.st_gid != member.gid
            or (before.st_mode & 0o7777) != member.mode
            or before.st_size != member.size
        ):
            raise WorkloadValidationError("workload adapter metadata is invalid")
        _workload_deadline(deadline)
        snapshot_fd = os.memfd_create(
            "dgx-workload-adapter", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        _workload_deadline(deadline)
        digest = hashlib.sha256()
        remaining = member.size
        while remaining:
            _workload_deadline(deadline)
            data = os.read(source_fd, min(64 * 1024, remaining))
            _workload_deadline(deadline)
            if not data:
                raise WorkloadValidationError("workload adapter changed")
            digest.update(data)
            offset = 0
            while offset < len(data):
                _workload_deadline(deadline)
                written = os.write(snapshot_fd, data[offset:])
                _workload_deadline(deadline)
                if written <= 0:
                    raise WorkloadValidationError(
                        "workload adapter snapshot was incomplete"
                    )
                offset += written
            remaining -= len(data)
        _workload_deadline(deadline)
        after = os.fstat(source_fd)
        _workload_deadline(deadline)
        if (
            digest.hexdigest() != member.sha256
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or before.st_size != after.st_size
        ):
            raise WorkloadValidationError("workload adapter changed")
        _workload_deadline(deadline)
        fcntl.fcntl(
            snapshot_fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE,
        )
        _workload_deadline(deadline)
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        _workload_deadline(deadline)
        result, snapshot_fd = snapshot_fd, -1
        return result
    except WorkloadValidationError:
        raise
    except OSError as error:
        raise WorkloadValidationError("workload adapter is unsafe") from error
    finally:
        if snapshot_fd >= 0:
            os.close(snapshot_fd)
        if source_fd >= 0:
            os.close(source_fd)
        os.close(directory_fd)


def _workload_deadline(deadline: MonotonicDeadline) -> None:
    try:
        deadline.check()
    except DeadlineBindingError as error:
        raise WorkloadExecutionError("workload deadline has elapsed") from error


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise WorkloadValidationError(f"{name} is invalid")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise WorkloadValidationError(f"{name} is invalid")
    return value


_PRODUCTION_POLICIES = MappingProxyType(
    {
        "spark-runtime-v1": CompiledAdapterPolicy(
            "spark-runtime-v1", "bin/runtime-adapter", 60, 64 * 1024
        )
    }
)


class WorkloadReleaseTrustBoundary(Protocol):
    def authorize(
        self, request: ReleaseRequest, deadline: MonotonicDeadline
    ) -> ReleaseDescriptor: ...
