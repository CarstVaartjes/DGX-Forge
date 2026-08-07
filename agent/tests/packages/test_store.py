from __future__ import annotations

import hashlib
import os
import stat
import threading
from pathlib import Path

import pytest
from vonk_agent.packages.fetch import AcquisitionEngine
from vonk_agent.packages.providers import (
    ComponentDescriptor as FetchComponentDescriptor,
)
from vonk_agent.packages.providers import (
    FetchResponse,
    NetworkHop,
    ProviderRegistry,
    SourceLocation,
    Validators,
)
from vonk_agent.packages.state import OperationBinding, PackageStateConflict
from vonk_agent.packages.store import (
    ComponentDescriptor,
    ContentStore,
    PackageCapacityError,
    PackageStoreError,
    StoreObject,
)


def _binding(index: int, *, attempt: int = 1) -> OperationBinding:
    return OperationBinding(
        job_id=f"20000000-0000-4000-8000-{index:012d}",
        operation_id=f"00000000-0000-4000-8000-{index:012d}",
        attempt=attempt,
        fence=f"10000000-0000-4000-8000-{index:012d}",
        node_id="spk_" + f"{index:032x}",
    )


def _descriptor(content: bytes, *, kind: str = "blob") -> ComponentDescriptor:
    return ComponentDescriptor(
        digest=hashlib.sha256(content).hexdigest(),
        size=len(content),
        kind=kind,
    )


def test_concurrent_reservations_cannot_overcommit_capacity(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reserve(index: int) -> None:
        store = ContentStore(root, capacity_bytes=10)
        barrier.wait()
        try:
            store.reserve(_binding(index), bytes_required=8)
        except PackageCapacityError:
            outcomes.append("rejected")
        else:
            outcomes.append("reserved")

    threads = [threading.Thread(target=reserve, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["rejected", "reserved"]


def test_verified_component_is_atomically_promoted_and_reused_after_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "packages"
    content = b"future-model-weights"
    descriptor = _descriptor(content, kind="model")
    store = ContentStore(root, capacity_bytes=1024)
    reservation = store.reserve(_binding(1), bytes_required=len(content))
    record = store.begin_component(reservation, descriptor)
    store.write_partial(record, content)

    promoted = store.promote_component(record, descriptor.digest)

    assert promoted == StoreObject(
        digest=descriptor.digest,
        size=len(content),
        kind="model",
        relative_name=f"objects/sha256/{descriptor.digest}",
    )
    object_path = store.object_path(promoted)
    assert object_path.read_bytes() == content
    assert stat.S_IMODE(object_path.stat().st_mode) == 0o444
    reopened = ContentStore(root, capacity_bytes=1024)
    assert reopened.lookup(descriptor.digest) == promoted
    second = reopened.reserve(_binding(2), bytes_required=len(content))
    cached = reopened.begin_component(second, descriptor)
    assert cached.state == "complete"
    assert reopened.promote_component(cached, descriptor.digest) == promoted


def test_digest_mismatch_never_enters_verified_namespace(tmp_path: Path) -> None:
    content = b"expected"
    descriptor = _descriptor(content)
    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    record = store.begin_component(
        store.reserve(_binding(1), bytes_required=len(content)),
        descriptor,
    )
    store.write_partial(record, b"tampered")

    with pytest.raises(PackageStoreError, match="size|digest"):
        store.promote_component(record, descriptor.digest)

    assert store.lookup(descriptor.digest) is None
    assert not (store.root / "objects" / "sha256" / descriptor.digest).exists()


@pytest.mark.parametrize("attack", ("replacement", "symlink", "hardlink", "fifo"))
def test_partial_inode_substitution_and_special_files_fail_closed(
    tmp_path: Path,
    attack: str,
) -> None:
    content = b"immutable-component"
    descriptor = _descriptor(content)
    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    record = store.begin_component(
        store.reserve(_binding(1), bytes_required=len(content)),
        descriptor,
    )
    partial = store.root / "partials" / record.partial_name
    partial.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(content)
    outside.chmod(0o600)
    if attack == "replacement":
        partial.write_bytes(content)
        partial.chmod(0o600)
    elif attack == "symlink":
        partial.symlink_to(outside)
    elif attack == "hardlink":
        partial.hardlink_to(outside)
    else:
        os.mkfifo(partial, mode=0o600)

    with pytest.raises(PackageStoreError, match="partial"):
        store.promote_component(record, descriptor.digest)

    assert outside.read_bytes() == content
    assert store.lookup(descriptor.digest) is None


@pytest.mark.parametrize(
    "crash_phase",
    (
        "after-file-fsync",
        "after-rename",
        "after-directory-fsync",
        "after-db-commit",
    ),
)
def test_restart_recovers_every_promotion_crash_window(
    tmp_path: Path,
    crash_phase: str,
) -> None:
    content = b"restart-safe-component"
    descriptor = _descriptor(content)
    crashed = False

    def crash(phase: str) -> None:
        nonlocal crashed
        if phase == crash_phase and not crashed:
            crashed = True
            raise RuntimeError("simulated crash")

    root = tmp_path / "packages"
    store = ContentStore(root, capacity_bytes=1024, crash_hook=crash)
    record = store.begin_component(
        store.reserve(_binding(1), bytes_required=len(content)),
        descriptor,
    )
    store.write_partial(record, content)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.promote_component(record, descriptor.digest)

    reopened = ContentStore(root, capacity_bytes=1024)
    recovered = reopened.lookup(descriptor.digest)
    if crash_phase == "after-file-fsync":
        assert recovered is None
        recovered = reopened.promote_component(record, descriptor.digest)
    assert recovered is not None
    assert reopened.object_path(recovered).read_bytes() == content


def test_reservation_and_partial_mutations_require_exact_fence(tmp_path: Path) -> None:
    content = b"owned"
    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    reservation = store.reserve(_binding(1), bytes_required=len(content))
    record = store.begin_component(reservation, _descriptor(content))

    with pytest.raises(PackageStateConflict, match="fence"):
        store.release_reservation(
            reservation.with_binding(
                OperationBinding(
                    job_id=reservation.job_id,
                    operation_id=reservation.operation_id,
                    attempt=reservation.attempt,
                    fence="20000000-0000-4000-8000-000000000001",
                    node_id=reservation.node_id,
                )
            )
        )
    with pytest.raises(PackageStateConflict, match="fence"):
        store.write_partial(record.with_fence("3" * 64), content)


def test_store_rejects_symlink_root_and_hardlinked_database(tmp_path: Path) -> None:
    real = tmp_path / "real"
    ContentStore(real, capacity_bytes=1024)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real, target_is_directory=True)
    with pytest.raises(PackageStoreError, match="root"):
        ContentStore(linked_root, capacity_bytes=1024)

    database = real / "package-state.sqlite3"
    (tmp_path / "database-link").hardlink_to(database)
    with pytest.raises(PackageStoreError, match="database"):
        ContentStore(real, capacity_bytes=1024)


def test_partial_checkpoint_binds_validators_and_prefix_across_restart(
    tmp_path: Path,
) -> None:
    content = b"resumable-component"
    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    record = store.begin_component(
        store.reserve(_binding(1), bytes_required=len(content)),
        _descriptor(content),
    )
    store.append_partial(record, content[:8])
    validators = Validators(etag='"immutable-v1"')
    checkpoint = store.checkpoint(record, 8, validators)

    reopened = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    resumed = reopened.resume_component(checkpoint.binding, checkpoint.digest)

    assert resumed.bytes_completed == 8
    assert resumed.validators == validators
    assert b"".join(reopened.iter_partial(resumed)) == content[:8]


def test_derived_lookup_and_fenced_corrupt_quarantine(tmp_path: Path) -> None:
    content = b"derived-environment"
    binding = _binding(1)
    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    record = store.begin_component(
        store.reserve(binding, bytes_required=len(content)),
        _descriptor(content, kind="python-environment"),
    )
    store.write_partial(record, content)
    value = store.promote_component(record, record.digest)
    store.record_derived(binding, "d" * 64, value.digest)
    assert store.lookup_derived("d" * 64) == value

    path = store.object_path(value)
    path.chmod(0o644)
    path.write_bytes(b"corrupt-environment")
    path.chmod(0o444)
    assert store.is_immutable(value) is False
    store.quarantine(value, binding)

    assert store.lookup_derived("d" * 64) is None
    assert store.lookup(value.digest) is None
    assert len(tuple((store.root / "quarantine").iterdir())) == 1


def test_acquisition_engine_uses_the_durable_content_store_contract(
    tmp_path: Path,
) -> None:
    content = b"integrated-resumable-component"
    digest = hashlib.sha256(content).hexdigest()
    source = SourceLocation(
        provider="https",
        url="https://models.example.test/component.bin",
        immutable_id=f"sha256:{digest}",
        allowed_domains=("models.example.test",),
    )
    descriptor = FetchComponentDescriptor(
        name="future-component",
        kind="model",
        digest=f"sha256:{digest}",
        size=len(content),
        sources=(source,),
    )

    class Provider:
        name = "https"

        def open(self, _source, offset, _validators, _deadline):
            return FetchResponse(
                status_code=206 if offset else 200,
                start_offset=offset,
                total_size=len(content),
                validators=Validators(etag='"immutable-v1"'),
                chunks=(content[offset:],),
                hops=(
                    NetworkHop(
                        "https://models.example.test/component.bin",
                        ("8.8.8.8",),
                    ),
                ),
            )

    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    progress: list[dict[str, object]] = []

    result = AcquisitionEngine(store, ProviderRegistry((Provider(),))).fetch(
        descriptor,
        _binding(1),
        progress.append,
        lambda: False,
    )

    assert result == store.lookup(digest)
    assert progress[-1]["bytes_completed"] == len(content)


def test_new_attempt_atomically_adopts_its_own_restart_partial(
    tmp_path: Path,
) -> None:
    content = b"retry-owned-partial"
    store = ContentStore(tmp_path / "packages", capacity_bytes=1024)
    first = _binding(1)
    descriptor = _descriptor(content)
    initial = store.begin_component(
        store.reserve(first, bytes_required=len(content)), descriptor
    )
    checkpoint = store.append_partial(initial, content[:5])
    second = OperationBinding(
        job_id=first.job_id,
        operation_id=first.operation_id,
        attempt=2,
        fence="40000000-0000-4000-8000-000000000001",
        node_id=first.node_id,
    )

    resumed = store.begin_component(
        store.reserve(second, bytes_required=len(content)), descriptor
    )

    assert resumed.attempt == 2
    assert resumed.fence == second.fence
    assert resumed.bytes_completed == 5
    assert b"".join(store.iter_partial(resumed)) == content[:5]
    with pytest.raises(PackageStateConflict, match="stale"):
        store.append_partial(checkpoint, b"x")
