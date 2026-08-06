from __future__ import annotations

from pathlib import Path

import pytest
from dgx_agent.package_operations import OperationBinding
from dgx_agent.packages.gc import GarbageCollectionInterrupted, PackageGarbageCollector
from dgx_agent.packages.state import PackageState
from dgx_agent.packages.store import StoreObject


def _binding(index: int = 1) -> OperationBinding:
    return OperationBinding(
        job_id=f"40000000-0000-4000-8000-{index:012d}",
        operation_id=f"50000000-0000-4000-8000-{index:012d}",
        attempt=1,
        fence=f"60000000-0000-4000-8000-{index:012d}",
        node_id="spk_" + f"{index:032x}",
    )


class Objects:
    def __init__(self, values: list[StoreObject]) -> None:
        self.values = {value.digest: value for value in values}
        self.deleted: list[str] = []

    def list_objects(self):
        return tuple(self.values.values())

    def delete_unreachable(self, binding, digest: str, *, now_ns: int) -> int:
        del binding, now_ns
        value = self.values.pop(digest, None)
        if value is None:
            return 0
        self.deleted.append(digest)
        return value.size


def _record(
    state: PackageState,
    binding: OperationBinding,
    generation: str,
    digest: str,
    status: str,
) -> None:
    state.record_generation(
        binding,
        deployment_id="future-stack",
        generation_id=generation,
        release_digest=digest,
        object_digests=(digest,),
        state=status,
    )


def test_gc_preview_and_apply_respect_every_reachability_root(tmp_path: Path) -> None:
    state = PackageState(tmp_path / "state")
    owner = _binding(1)
    state.begin_operation(owner)
    _record(state, owner, "active-a", "1" * 64, "active")
    _record(state, owner, "retained-a", "2" * 64, "retained")
    _record(state, owner, "staging-a", "3" * 64, "staging")
    _record(state, owner, "pinned-a", "4" * 64, "pinned")
    _record(state, owner, "leased-a", "5" * 64, "failed")
    state.acquire_lease(
        owner,
        lease_id="70000000-0000-4000-8000-000000000001",
        generation_id="leased-a",
        expires_at_ns=2_000,
    )
    objects = Objects(
        [
            StoreObject(
                str(index) * 64,
                index,
                "derived" if index == 6 else "model",
                f"objects/sha256/{str(index) * 64}",
            )
            for index in range(1, 8)
        ]
    )
    collector = PackageGarbageCollector(state, objects, clock_ns=lambda: 1_000)

    preview = collector.collect(_binding(2), dry_run=True, target_bytes=100)
    applied = collector.collect(_binding(3), dry_run=False, target_bytes=100)

    assert preview.candidate_digests == ("6" * 64, "7" * 64)
    assert objects.deleted == ["6" * 64, "7" * 64]
    assert applied.reclaimed_bytes == 13


def test_gc_restart_resumes_exact_durable_plan(tmp_path: Path) -> None:
    state = PackageState(tmp_path / "state")
    objects = Objects(
        [
            StoreObject("a" * 64, 5, "derived", f"objects/sha256/{'a' * 64}"),
            StoreObject("b" * 64, 7, "model", f"objects/sha256/{'b' * 64}"),
        ]
    )
    binding = _binding(1)
    crashes = {"count": 0}

    def interrupt(_digest: str) -> None:
        crashes["count"] += 1
        if crashes["count"] == 1:
            raise GarbageCollectionInterrupted("restart")

    collector = PackageGarbageCollector(
        state,
        objects,
        clock_ns=lambda: 1_000,
        after_delete=interrupt,
    )
    with pytest.raises(GarbageCollectionInterrupted):
        collector.collect(binding, dry_run=False, target_bytes=100)

    result = PackageGarbageCollector(
        PackageState(tmp_path / "state"), objects, clock_ns=lambda: 1_000
    ).collect(binding, dry_run=False, target_bytes=100)

    assert result.reclaimed_bytes == 12
    assert objects.values == {}
