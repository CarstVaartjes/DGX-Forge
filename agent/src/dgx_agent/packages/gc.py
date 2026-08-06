"""Restart-safe garbage collection for the workload content store."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from ..package_operations import OperationBinding
from .state import PackageState


class GarbageCollectionError(RuntimeError):
    """Garbage collection could not preserve its safety contract."""


class GarbageCollectionInterrupted(GarbageCollectionError):
    """A testable interruption occurred after a destructive boundary."""


class _Objects(Protocol):
    def list_objects(self): ...

    def delete_unreachable(
        self, binding: OperationBinding, digest: str, *, now_ns: int
    ) -> int: ...


@dataclass(frozen=True)
class GarbageCollectionResult(Mapping[str, object]):
    candidate_digests: tuple[str, ...]
    reclaimed_bytes: int
    dry_run: bool
    status: str

    def _value(self) -> dict[str, object]:
        return {
            "candidate_digests": self.candidate_digests,
            "reclaimed_bytes": self.reclaimed_bytes,
            "dry_run": self.dry_run,
            "status": self.status,
        }

    def __getitem__(self, key: str) -> object:
        return self._value()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._value())

    def __len__(self) -> int:
        return 4


class PackageGarbageCollector:
    """Delete only objects outside every durable generation and lease root."""

    def __init__(
        self,
        state: PackageState,
        objects: _Objects,
        *,
        clock_ns: Callable[[], int],
        cancelled: Callable[[OperationBinding], bool] | None = None,
        after_delete: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(state, PackageState):
            raise TypeError("package state is invalid")
        if not callable(getattr(objects, "list_objects", None)) or not callable(
            getattr(objects, "delete_unreachable", None)
        ):
            raise TypeError("package object store is invalid")
        if not callable(clock_ns):
            raise TypeError("GC clock is invalid")
        self._state = state
        self._objects = objects
        self._clock_ns = clock_ns
        self._cancelled = cancelled or (lambda _binding: False)
        self._after_delete = after_delete or (lambda _digest: None)

    def collect(
        self,
        binding: OperationBinding,
        *,
        dry_run: bool,
        target_bytes: int,
    ) -> GarbageCollectionResult:
        if not isinstance(binding, OperationBinding):
            raise TypeError("GC operation binding is invalid")
        if (
            type(dry_run) is not bool
            or type(target_bytes) is not int
            or target_bytes < 1
        ):
            raise ValueError("GC request is invalid")
        self._state.begin_operation(binding, phase="gc-plan")
        intent_id = binding.operation_id
        self._state.record_gc_intent(
            binding,
            intent_id=intent_id,
            target_bytes=target_bytes,
            dry_run=dry_run,
        )
        existing = self._state.list_gc_candidates(binding, intent_id)
        if not existing:
            now_ns = self._now()
            reachable = self._state.reachable_objects(now_ns=now_ns)
            available = tuple(self._objects.list_objects())
            candidates = tuple(
                sorted(
                    (
                        (item.digest, item.size)
                        for item in available
                        if item.digest not in reachable
                    ),
                    key=lambda item: (
                        0
                        if next(
                            value.kind for value in available if value.digest == item[0]
                        )
                        == "derived"
                        else 1,
                        item[0],
                    ),
                )
            )
            if candidates:
                existing = self._state.plan_gc_candidates(
                    binding, intent_id, candidates
                )
        candidate_digests = tuple(candidate.digest for candidate in existing)
        if dry_run or not existing:
            self._state.transition_gc_intent(
                binding,
                intent_id=intent_id,
                expected_states=frozenset({"planned", "running"}),
                state="completed",
            )
            self._state.set_phase(binding, "completed")
            return GarbageCollectionResult(candidate_digests, 0, dry_run, "completed")

        intent = self._state.gc_intent(binding, intent_id)
        if intent is not None and intent.state == "planned":
            self._state.transition_gc_intent(
                binding,
                intent_id=intent_id,
                expected_states=frozenset({"planned"}),
                state="running",
            )
        self._state.set_phase(binding, "gc-delete")
        reclaimed = sum(
            candidate.size for candidate in existing if candidate.state == "deleted"
        )
        for candidate in self._state.list_gc_candidates(binding, intent_id):
            if candidate.state != "pending" or reclaimed >= target_bytes:
                continue
            if self._cancelled(binding):
                self._state.transition_gc_intent(
                    binding,
                    intent_id=intent_id,
                    expected_states=frozenset({"running"}),
                    state="cancelled",
                )
                self._state.set_phase(binding, "cancelled")
                return GarbageCollectionResult(
                    candidate_digests, reclaimed, False, "cancelled"
                )
            now_ns = self._now()
            if candidate.digest in self._state.reachable_objects(now_ns=now_ns):
                self._state.mark_gc_candidate(
                    binding, intent_id, candidate.digest, state="skipped"
                )
                continue
            deleted = self._objects.delete_unreachable(
                binding, candidate.digest, now_ns=now_ns
            )
            self._after_delete(candidate.digest)
            self._state.mark_gc_candidate(
                binding, intent_id, candidate.digest, state="deleted"
            )
            # A zero result on a pending replay means the prior process deleted
            # the exact planned object but crashed before journaling progress.
            reclaimed += deleted if deleted > 0 else candidate.size

        self._state.transition_gc_intent(
            binding,
            intent_id=intent_id,
            expected_states=frozenset({"running"}),
            state="completed",
        )
        self._state.set_phase(binding, "completed")
        return GarbageCollectionResult(candidate_digests, reclaimed, False, "completed")

    def _now(self) -> int:
        value = self._clock_ns()
        if type(value) is not int or value < 0:
            raise GarbageCollectionError("GC clock returned an invalid value")
        return value
