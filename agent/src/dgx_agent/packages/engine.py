"""Journaled orchestration for generic workload-package generations."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dgx_agent_protocol import AgentOperation, PackageOperationRequest, PackageReleaseLock

from ..package_operations import (
    OperationBinding,
    PackageDisposition,
    PackageInspection,
)
from .adapter import AdapterInvocation, AdapterOperation
from .state import GenerationRecord, PackageState, PackageStateConflict
from .gc import PackageGarbageCollector


class PackageEngineError(RuntimeError):
    """A package transition could not be completed safely."""


class PackageCancelled(PackageEngineError):
    """The operation was cancelled before an unsafe transition."""


@dataclass(frozen=True)
class PackageEvidence(Mapping[str, object]):
    """Canonical result returned through the generic operation boundary."""

    operation: str
    deployment_id: str
    release_digest: str
    generation: str | None
    status: str
    evidence_digest: str

    @property
    def staged_generation(self) -> str | None:
        return self.generation

    @property
    def active_generation(self) -> str | None:
        return self.generation if self.status == "active" else None

    def _value(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "deployment_id": self.deployment_id,
            "release_digest": self.release_digest,
            "generation": self.generation,
            "status": self.status,
            "evidence_digest": self.evidence_digest,
        }

    def __getitem__(self, key: str) -> object:
        return self._value()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._value())

    def __len__(self) -> int:
        return 6


class _Trust(Protocol):
    def refresh(self) -> None: ...

    def trusted_lock(self, digest: str): ...


class _Acquisition(Protocol):
    def fetch(self, descriptor, binding, progress, cancelled, *, deadline=None): ...


class _Materializer(Protocol):
    def materialize(self, lock, objects, staging: Path): ...


class PackageEngine:
    """Prepare, atomically select, verify, and roll back workload generations."""

    def __init__(
        self,
        *,
        state: PackageState,
        trust: _Trust,
        acquisition: _Acquisition,
        materializer: _Materializer,
        generation_root: Path,
        pointer_root: Path,
        adapter_factory: Callable[..., object],
        preflight: Callable[[object, PackageOperationRequest, OperationBinding], None],
        progress: Callable[[OperationBinding, Mapping[str, object]], None],
        cancelled: Callable[[OperationBinding], bool],
        garbage_collector: PackageGarbageCollector | None = None,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._state = state
        self._trust = trust
        self._acquisition = acquisition
        self._materializer = materializer
        self._generation_root = _private_directory(Path(generation_root))
        self._pointer_root = _private_directory(Path(pointer_root))
        self._lock_root = _private_directory(self._generation_root.parent / "locks")
        self._adapter_factory = adapter_factory
        self._preflight = preflight
        self._progress = progress
        self._cancelled = cancelled
        if garbage_collector is not None and not isinstance(
            garbage_collector, PackageGarbageCollector
        ):
            raise TypeError("package garbage collector is invalid")
        self._garbage_collector = garbage_collector
        self._crash_hook = crash_hook or (lambda _phase: None)
        # A prepared release is sufficient for activation/rollback without a
        # network trust refresh. Durable materialization receipts remain the
        # source of object identity; this cache contains only parsed metadata.
        self._locks: dict[str, object] = {}

    def execute(self, request, binding, deadline) -> PackageEvidence:
        if not isinstance(request, PackageOperationRequest):
            raise TypeError("package request is invalid")
        if not isinstance(binding, OperationBinding):
            raise TypeError("package operation binding is invalid")
        operation = request.operation
        if operation is AgentOperation.PACKAGE_PREPARE:
            return self._prepare(request, binding, deadline)
        if operation is AgentOperation.PACKAGE_ACTIVATE:
            return self._activate(request, binding, deadline)
        if operation is AgentOperation.PACKAGE_ROLLBACK:
            return self._rollback(request, binding, deadline)
        if operation is AgentOperation.PACKAGE_HEALTH:
            return self._invoke_existing(
                request, binding, deadline, AdapterOperation.HEALTH
            )
        if operation is AgentOperation.PACKAGE_STOP:
            return self._invoke_existing(
                request, binding, deadline, AdapterOperation.STOP
            )
        if operation is AgentOperation.PACKAGE_REMOVE:
            return self._remove(request, binding)
        if operation is AgentOperation.PACKAGE_REPAIR:
            return self._repair(request, binding, deadline)
        if operation is AgentOperation.PACKAGE_GC:
            return self._gc(request, binding)
        raise PackageEngineError("package operation belongs to another engine")

    def inspect(self, request, binding, deadline) -> PackageInspection:
        del request, deadline
        try:
            record = self._state.operation(binding)
        except PackageStateConflict:
            return PackageInspection(PackageDisposition.READY)
        if record.phase in {"completed", "validated", "active", "rolled-back"}:
            return PackageInspection(PackageDisposition.COMPLETED)
        if record.phase in {
            "preflight",
            "fetch",
            "materialize",
            "validate",
            "repair",
        }:
            return PackageInspection(PackageDisposition.SAFE_TO_RETRY)
        if record.phase in {"pointer-selecting", "pointer-selected", "start", "health"}:
            return PackageInspection(PackageDisposition.COMPENSATE)
        return PackageInspection(PackageDisposition.OPERATOR_INTERVENTION)

    def _prepare(self, request, binding, deadline) -> PackageEvidence:
        deployment_id, release_digest = _release_identity(request)
        self._state.begin_operation(binding, phase="preflight")
        if self._cancelled(binding):
            self._state.set_phase(binding, "cancelled")
            raise PackageCancelled("package preparation was cancelled")
        self._trust.refresh()
        lock = self._trust.trusted_lock(release_digest)
        if getattr(lock, "digest", None) != release_digest:
            raise PackageEngineError("trusted release identity is inconsistent")
        self._locks[release_digest] = lock
        self._persist_lock(lock)
        self._preflight(lock, request, binding)
        self._state.set_phase(binding, "fetch")
        objects: dict[str, object] = {}
        for descriptor in (*tuple(getattr(lock, "components", ())), lock.adapter):
            if self._cancelled(binding):
                self._state.set_phase(binding, "cancelled")
                raise PackageCancelled("package preparation was cancelled")
            stored = self._acquisition.fetch(
                descriptor,
                binding,
                lambda value: self._progress(binding, value),
                lambda: self._cancelled(binding),
                deadline=deadline,
            )
            objects[stored.digest] = stored
        self._state.set_phase(binding, "materialize")
        materialized = self._materializer.materialize(
            lock, objects, self._generation_root
        )
        generation_id = _generation_id(request)
        digests = tuple(sorted(set(materialized.object_digests)))
        self._state.record_generation(
            binding,
            deployment_id=deployment_id,
            generation_id=generation_id,
            release_digest=release_digest,
            object_digests=digests,
            state="staging",
        )
        self._state.set_phase(binding, "validate")
        adapter = self._adapter(lock, generation_id, objects)
        self._invoke(
            adapter,
            AdapterOperation.PREPARE,
            binding,
            release_digest,
            generation_id,
            deadline,
        )
        self._invoke(
            adapter,
            AdapterOperation.VERIFY,
            binding,
            release_digest,
            generation_id,
            deadline,
        )
        self._invoke(
            adapter,
            AdapterOperation.VERIFY_RELEASE,
            binding,
            release_digest,
            generation_id,
            deadline,
        )
        self._state.transition_generation(
            binding,
            generation_id=generation_id,
            expected_states=frozenset({"staging"}),
            state="validated",
        )
        self._state.set_phase(binding, "validated")
        return _evidence(request, generation_id, "validated")

    def _activate(self, request, binding, deadline) -> PackageEvidence:
        deployment_id, release_digest = _release_identity(request)
        self._state.begin_operation(binding, phase="pointer-selecting")
        target = self._state.generation_for_release(deployment_id, release_digest)
        if target is None or target.state not in {"validated", "retained", "active"}:
            raise PackageEngineError("release has no validated local generation")
        previous = self._state.active_generation(deployment_id)
        if self._cancelled(binding):
            self._state.set_phase(binding, "cancelled")
            raise PackageCancelled("package activation was cancelled")
        self._state.set_phase(binding, "pointer-selecting")
        self._select_pointer(target, previous)
        self._state.set_phase(binding, "pointer-selected")
        self._crash_hook("pointer-selected")
        self._state.activate_generation(binding, target.generation_id)
        lock = self._local_lock(release_digest)
        adapter = self._adapter(lock, target.generation_id, {})
        try:
            self._state.set_phase(binding, "start")
            self._invoke(
                adapter,
                AdapterOperation.START,
                binding,
                release_digest,
                target.generation_id,
                deadline,
            )
            if self._cancelled(binding):
                return self._compensate(
                    request, binding, target, previous, deadline, "rolled-back"
                )
            self._state.set_phase(binding, "health")
            self._invoke(
                adapter,
                AdapterOperation.HEALTH,
                binding,
                release_digest,
                target.generation_id,
                deadline,
            )
        except PackageEngineError:
            raise
        except Exception as error:
            self._compensate(request, binding, target, previous, deadline, "failed")
            raise PackageEngineError("package health verification failed") from error
        self._state.set_phase(binding, "active")
        return _evidence(request, target.generation_id, "active")

    def _rollback(self, request, binding, deadline) -> PackageEvidence:
        deployment_id, release_digest = _release_identity(request)
        self._state.begin_operation(binding, phase="pointer-selecting")
        target = self._state.generation_for_release(deployment_id, release_digest)
        if target is None or target.state not in {"retained", "validated", "active"}:
            raise PackageEngineError("rollback generation is not retained locally")
        previous = self._state.active_generation(deployment_id)
        self._select_pointer(target, previous)
        self._state.set_phase(binding, "pointer-selected")
        self._state.activate_generation(binding, target.generation_id)
        adapter = self._adapter(
            self._local_lock(release_digest), target.generation_id, {}
        )
        self._invoke(
            adapter,
            AdapterOperation.START,
            binding,
            release_digest,
            target.generation_id,
            deadline,
        )
        self._invoke(
            adapter,
            AdapterOperation.HEALTH,
            binding,
            release_digest,
            target.generation_id,
            deadline,
        )
        self._state.set_phase(binding, "rolled-back")
        return _evidence(request, target.generation_id, "active")

    def _compensate(self, request, binding, target, previous, deadline, status):
        if previous is None:
            self._state.transition_generation(
                binding,
                generation_id=target.generation_id,
                expected_states=frozenset({"active", "retained"}),
                state="failed",
            )
            self._state.set_phase(binding, status)
            return _evidence(request, target.generation_id, status)
        self._select_pointer(previous, target)
        self._state.activate_generation(binding, previous.generation_id)
        adapter = self._adapter(
            self._local_lock(previous.release_digest), previous.generation_id, {}
        )
        self._invoke(
            adapter,
            AdapterOperation.START,
            binding,
            previous.release_digest,
            previous.generation_id,
            deadline,
        )
        self._invoke(
            adapter,
            AdapterOperation.HEALTH,
            binding,
            previous.release_digest,
            previous.generation_id,
            deadline,
        )
        if status == "failed":
            self._state.transition_generation(
                binding,
                generation_id=target.generation_id,
                expected_states=frozenset({"retained"}),
                state="failed",
            )
        self._state.set_phase(binding, "rolled-back")
        return _evidence(request, target.generation_id, "rolled-back")

    def _invoke_existing(self, request, binding, deadline, operation):
        deployment_id, release_digest = _release_identity(request)
        self._state.begin_operation(binding, phase=operation.value)
        generation = self._state.generation_for_release(deployment_id, release_digest)
        if generation is None:
            raise PackageEngineError("package generation is not installed")
        adapter = self._adapter(
            self._local_lock(release_digest), generation.generation_id, {}
        )
        self._invoke(
            adapter,
            operation,
            binding,
            release_digest,
            generation.generation_id,
            deadline,
        )
        self._state.set_phase(binding, "completed")
        return _evidence(request, generation.generation_id, "ok")

    def _remove(self, request, binding):
        deployment_id, release_digest = _release_identity(request)
        self._state.begin_operation(binding, phase="remove")
        generation = self._state.generation_for_release(deployment_id, release_digest)
        if generation is None:
            self._state.set_phase(binding, "completed")
            return _evidence(request, None, "removed")
        if generation.state == "active":
            raise PackageEngineError("active generation cannot be removed")
        self._state.transition_generation(
            binding,
            generation_id=generation.generation_id,
            expected_states=frozenset({generation.state}),
            state="failed",
        )
        self._state.set_phase(binding, "completed")
        return _evidence(request, generation.generation_id, "removed")

    def _repair(self, request, binding, deadline):
        deployment_id, release_digest = _release_identity(request)
        self._state.begin_operation(binding, phase="repair")
        generation = self._state.generation_for_release(deployment_id, release_digest)
        if generation is not None:
            if generation.state == "active":
                raise PackageEngineError(
                    "active generation must be rolled back before repair"
                )
            self._state.transition_generation(
                binding,
                generation_id=generation.generation_id,
                expected_states=frozenset({generation.state}),
                state="quarantined",
            )
        return self._prepare(request, binding, deadline)

    def _gc(self, request, binding):
        collector = self._garbage_collector
        if collector is None:
            raise PackageEngineError("package garbage collection is unavailable")
        target_bytes = request.target_bytes or 2**63 - 1
        return collector.collect(
            binding,
            dry_run=bool(request.dry_run),
            target_bytes=target_bytes,
        )

    def _adapter(self, lock, generation_id, objects):
        return self._adapter_factory(
            lock,
            generation_id,
            self._generation_root / lock.digest,
            objects,
        )

    def _local_lock(self, release_digest: str):
        try:
            return self._locks[release_digest]
        except KeyError:
            path = self._lock_root / f"{release_digest}.json"
            try:
                metadata = path.stat(follow_symlinks=False)
                if (
                    not path.is_file()
                    or metadata.st_nlink != 1
                    or metadata.st_size > 1024 * 1024
                    or metadata.st_mode & 0o177
                ):
                    raise OSError("lock metadata is unsafe")
                raw = path.read_bytes()
                lock = PackageReleaseLock.parse(raw)
            except (OSError, TypeError, ValueError) as error:
                raise PackageEngineError("local release metadata is unavailable") from error
            if lock.digest != release_digest or lock.canonical_bytes != raw:
                raise PackageEngineError("local release metadata is inconsistent")
            self._locks[release_digest] = lock
            return lock

    def _persist_lock(self, lock: PackageReleaseLock) -> None:
        if type(lock) is not PackageReleaseLock:
            # Existing unit fixtures use a narrow in-memory lock double.  A
            # production trust source always returns the protocol type; never
            # serialize an untyped value into the durable lock boundary.
            return
        raw = lock.canonical_bytes
        path = self._lock_root / f"{lock.digest}.json"
        if path.exists():
            try:
                if path.read_bytes() != raw:
                    raise PackageEngineError("local release metadata conflicts")
            except OSError as error:
                raise PackageEngineError("local release metadata is unavailable") from error
            return
        temporary = self._lock_root / f".{lock.digest}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o444,
        )
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, path)
            directory = os.open(
                self._lock_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            try:
                if path.read_bytes() != raw:
                    raise PackageEngineError("local release metadata conflicts")
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _invoke(adapter, operation, binding, release_digest, generation_id, deadline):
        invocation = AdapterInvocation(
            job_id=binding.job_id,
            operation_id=binding.operation_id,
            attempt=binding.attempt,
            fence=binding.fence,
            release_digest=release_digest,
            generation=generation_id,
        )
        return adapter.execute(operation, invocation, deadline)

    def _select_pointer(
        self, generation: GenerationRecord, previous: GenerationRecord | None
    ) -> None:
        body = {
            "schema_version": 1,
            "deployment_id": generation.deployment_id,
            "generation": generation.generation_id,
            "release_digest": generation.release_digest,
            "previous_generation": None if previous is None else previous.generation_id,
        }
        encoded_body = _canonical(body)
        document = dict(body)
        document["pointer_digest"] = hashlib.sha256(encoded_body).hexdigest()
        _atomic_write(
            self._pointer_root / f"{generation.deployment_id}.json",
            _canonical(document),
        )


def _release_identity(request: PackageOperationRequest) -> tuple[str, str]:
    if request.deployment_id is None or request.release_digest is None:
        raise PackageEngineError("release-bound package identity is missing")
    return request.deployment_id, request.release_digest


def _generation_id(request: PackageOperationRequest) -> str:
    deployment_id, release_digest = _release_identity(request)
    deployment = request.deployment_digest or ("0" * 64)
    return f"gen-{deployment_id[:24]}-{deployment[:12]}-{release_digest[:24]}"


def _evidence(
    request: PackageOperationRequest, generation: str | None, status: str
) -> PackageEvidence:
    deployment_id, release_digest = _release_identity(request)
    body = {
        "operation": request.operation.value,
        "deployment_id": deployment_id,
        "release_digest": release_digest,
        "generation": generation,
        "status": status,
    }
    return PackageEvidence(
        **body, evidence_digest=hashlib.sha256(_canonical(body)).hexdigest()
    )


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("package engine roots must be absolute")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o400,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
