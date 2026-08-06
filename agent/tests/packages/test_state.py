from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from dgx_agent.packages.state import (
    OperationBinding,
    PackageState,
    PackageStateConflict,
    PackageStateError,
)


def _binding(*, attempt: int = 1, fence: str | None = None) -> OperationBinding:
    return OperationBinding(
        job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        operation_id="11111111-1111-4111-8111-111111111111",
        attempt=attempt,
        fence=fence or "22222222-2222-4222-8222-222222222222",
        node_id="spk_" + "a" * 32,
    )


def test_operation_journal_survives_restart_and_cancel_is_fence_owned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "packages"
    binding = _binding()
    state = PackageState(root)

    opened = state.begin_operation(binding, phase="fetch")
    state.request_cancel(binding)

    assert opened.phase == "fetch"
    reopened = PackageState(root)
    assert reopened.operation(binding).cancel_requested is True
    wrong_fence = _binding(fence="33333333-3333-4333-8333-333333333333")
    with pytest.raises(PackageStateConflict, match="fence"):
        reopened.request_cancel(wrong_fence)


def test_operation_attempts_are_monotonic_and_old_fences_never_reenter(
    tmp_path: Path,
) -> None:
    state = PackageState(tmp_path / "packages")
    first = _binding()
    state.begin_operation(first)
    second = _binding(
        attempt=2,
        fence="44444444-4444-4444-8444-444444444444",
    )

    state.begin_operation(second)

    assert state.operation(second).attempt == 2
    with pytest.raises(PackageStateConflict, match="stale"):
        state.begin_operation(first)
    with pytest.raises(PackageStateConflict, match="fence"):
        state.begin_operation(
            _binding(
                attempt=2,
                fence="55555555-5555-4555-8555-555555555555",
            )
        )


def test_schema_contains_all_durable_package_engine_records(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    PackageState(root)

    with sqlite3.connect(root / "package-state.sqlite3") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert {
        "operations",
        "reservations",
        "components",
        "partials",
        "derived_objects",
        "generations",
        "generation_objects",
        "leases",
        "gc_intents",
    } <= tables


@pytest.mark.parametrize("damage", ("bytes", "version", "missing-table"))
def test_corrupt_or_unknown_database_schema_fails_closed(
    tmp_path: Path,
    damage: str,
) -> None:
    root = tmp_path / "packages"
    PackageState(root)
    database = root / "package-state.sqlite3"
    if damage == "bytes":
        database.write_bytes(b"not sqlite")
    else:
        with sqlite3.connect(database) as connection:
            if damage == "version":
                connection.execute("PRAGMA user_version = 99")
            else:
                connection.execute("DROP TABLE leases")

    with pytest.raises(PackageStateError, match="database|schema"):
        PackageState(root)


def test_reachability_includes_active_rollback_staged_and_live_leases(
    tmp_path: Path,
) -> None:
    state = PackageState(tmp_path / "packages")
    binding = _binding()
    state.begin_operation(binding)
    state.record_generation(
        binding,
        deployment_id="future-stack",
        generation_id="active-a",
        release_digest="a" * 64,
        object_digests=("1" * 64,),
        state="active",
    )
    state.record_generation(
        binding,
        deployment_id="future-stack",
        generation_id="rollback-a",
        release_digest="b" * 64,
        object_digests=("2" * 64,),
        state="rollback",
    )
    state.record_generation(
        binding,
        deployment_id="future-stack",
        generation_id="staged-a",
        release_digest="c" * 64,
        object_digests=("3" * 64,),
        state="staged",
    )
    state.record_generation(
        binding,
        deployment_id="future-stack",
        generation_id="inactive-a",
        release_digest="d" * 64,
        object_digests=("4" * 64,),
        state="inactive",
    )
    state.acquire_lease(
        binding,
        lease_id=str(uuid.uuid4()),
        generation_id="inactive-a",
        expires_at_ns=2_000,
    )

    assert state.reachable_objects(now_ns=1_000) == {
        "1" * 64,
        "2" * 64,
        "3" * 64,
        "4" * 64,
    }
    assert state.reachable_objects(now_ns=2_001) == {
        "1" * 64,
        "2" * 64,
        "3" * 64,
    }


def test_generation_and_gc_mutations_require_the_current_operation_fence(
    tmp_path: Path,
) -> None:
    state = PackageState(tmp_path / "packages")
    binding = _binding()
    state.begin_operation(binding)
    wrong = _binding(fence="66666666-6666-4666-8666-666666666666")

    with pytest.raises(PackageStateConflict, match="fence"):
        state.record_generation(
            wrong,
            deployment_id="future-stack",
            generation_id="generation-a",
            release_digest="a" * 64,
            object_digests=("1" * 64,),
            state="staged",
        )
    with pytest.raises(PackageStateConflict, match="fence"):
        state.record_gc_intent(
            wrong,
            intent_id=str(uuid.uuid4()),
            target_bytes=1024,
            dry_run=True,
        )


def test_derived_object_mapping_is_durable_and_fence_owned(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    binding = _binding()
    state = PackageState(root)
    state.begin_operation(binding)

    state.record_derived(binding, "7" * 64, "8" * 64)

    assert PackageState(root).lookup_derived("7" * 64) == "8" * 64
    with pytest.raises(PackageStateConflict, match="fence"):
        state.record_derived(
            _binding(fence="99999999-9999-4999-8999-999999999999"),
            "9" * 64,
            "a" * 64,
        )


def test_generation_transition_is_cas_fenced_across_operations(tmp_path: Path) -> None:
    state = PackageState(tmp_path / "packages")
    prepare = _binding()
    activate = OperationBinding(
        job_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        operation_id="77777777-7777-4777-8777-777777777777",
        attempt=1,
        fence="88888888-8888-4888-8888-888888888888",
        node_id=prepare.node_id,
    )
    state.begin_operation(prepare)
    state.record_generation(
        prepare,
        deployment_id="future-stack",
        generation_id="generation-a",
        release_digest="a" * 64,
        object_digests=("1" * 64, "2" * 64),
        state="validated",
    )
    state.begin_operation(activate)

    transitioned = state.transition_generation(
        activate,
        generation_id="generation-a",
        expected_states=frozenset({"validated"}),
        state="active",
    )

    assert transitioned.state == "active"
    assert transitioned.object_digests == ("1" * 64, "2" * 64)
    assert state.generations("future-stack") == (transitioned,)
    with pytest.raises(PackageStateConflict, match="state"):
        state.transition_generation(
            activate,
            generation_id="generation-a",
            expected_states=frozenset({"staging"}),
            state="failed",
        )


def test_gc_intent_and_candidate_progress_are_idempotent_and_restart_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "packages"
    state = PackageState(root)
    binding = _binding()
    state.begin_operation(binding)
    intent_id = binding.operation_id
    state.record_gc_intent(
        binding,
        intent_id=intent_id,
        target_bytes=1024,
        dry_run=False,
    )
    state.record_gc_intent(
        binding,
        intent_id=intent_id,
        target_bytes=1024,
        dry_run=False,
    )
    state.plan_gc_candidates(
        binding,
        intent_id,
        (("1" * 64, 100), ("2" * 64, 200)),
    )
    state.mark_gc_candidate(binding, intent_id, "1" * 64, state="deleted")
    state.transition_gc_intent(
        binding,
        intent_id=intent_id,
        expected_states=frozenset({"planned"}),
        state="running",
    )

    reopened = PackageState(root)
    assert reopened.gc_intent(binding, intent_id).state == "running"
    candidates = reopened.list_gc_candidates(binding, intent_id)
    assert tuple(candidate.state for candidate in candidates) == ("deleted", "pending")
