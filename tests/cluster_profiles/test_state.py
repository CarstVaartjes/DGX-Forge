from __future__ import annotations

import json
import os
import socket
from dataclasses import replace
from pathlib import Path
from unittest.mock import call, patch

import pytest

from cluster_profiles.state import (
    ControllerState,
    LockBusy,
    LockNotStale,
    StateFormatError,
    StateStore,
)

PROFILE_SHA = "1" * 64
DEFINITION_A_SHA = "a" * 64
DEFINITION_B_SHA = "b" * 64


def test_state_round_trip_and_atomic_replacement(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = ControllerState(
        status="active",
        active_profile="agent-full-dual",
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=PROFILE_SHA,
        active_definition_sha256={
            "visual-evaluator": DEFINITION_B_SHA,
            "deepseek-agent-dual": DEFINITION_A_SHA,
        },
        boot_ids={"node1": "boot-a", "node2": "boot-b"},
    )

    store.save(state)

    assert store.load() == state
    assert list(store.load().active_definition_sha256) == [
        "deepseek-agent-dual",
        "visual-evaluator",
    ]
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_state_write_is_atomic_when_replace_is_interrupted(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    original = ControllerState(
        status="active",
        active_profile="default",
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=PROFILE_SHA,
        active_definition_sha256={"deepseek-agent-dual": DEFINITION_A_SHA},
        boot_ids={},
    )
    store.save(original)

    with patch(
        "cluster_profiles.state.os.replace", side_effect=OSError("interrupted")
    ), pytest.raises(OSError, match="interrupted"):
        store.save(replace(original, active_profile="broken"))

    assert store.load().active_profile == "default"
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_missing_state_loads_safe_stopped_default(tmp_path: Path) -> None:
    state = StateStore(tmp_path).load()

    assert state.status == "stopped"
    assert state.active_profile is None
    assert state.active_profile_sha256 is None
    assert state.active_definition_sha256 == {}
    assert state.schema_version == 1


def test_crashed_transition_fence_quarantines_persisted_active_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    active = ControllerState(
        status="active",
        active_profile="agent-full-dual",
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=PROFILE_SHA,
        active_definition_sha256={"deepseek-agent-dual": DEFINITION_A_SHA},
    )
    store.save(active)
    store.begin_transition()

    quarantined = store.load()

    assert quarantined.status == "degraded"
    assert quarantined.active_profile is None
    assert quarantined.active_profile_sha256 is None
    assert quarantined.active_definition_sha256 == {}
    assert "manual recovery" in (quarantined.last_error or "")
    assert "model and output data are preserved" in (quarantined.last_error or "")
    assert len(quarantined.last_error or "") <= 512
    assert (tmp_path / "transition.fence").exists()


def test_first_transition_durably_creates_each_state_directory_entry(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / ".state" / "vonkctl"
    store = StateStore(state_directory)

    with patch.object(store, "_fsync_directory") as fsync_directory:
        store.begin_transition()

    assert fsync_directory.call_args_list == [
        call(tmp_path),
        call(tmp_path / ".state"),
        call(state_directory),
    ]
    assert (state_directory / "transition.fence").exists()


def test_load_rechecks_fence_after_reading_active_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    active = ControllerState(
        status="active",
        active_profile="agent-full-dual",
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=PROFILE_SHA,
        active_definition_sha256={"deepseek-agent-dual": DEFINITION_A_SHA},
    )
    store.save(active)
    json_load = json.load

    def read_then_fence(state_file):
        data = json_load(state_file)
        store.begin_transition()
        return data

    with patch("cluster_profiles.state.json.load", side_effect=read_then_fence):
        quarantined = store.load()

    assert quarantined.status == "degraded"
    assert quarantined.active_profile is None
    assert "manual recovery" in (quarantined.last_error or "")


def test_stale_transition_fence_is_not_removed_by_load_or_lock_acquisition(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    store.save(ControllerState.stopped())
    store.begin_transition()
    (tmp_path / "transition.fence").write_text(
        '{"created_at":"2000-01-01T00:00:00Z","host":"retired","pid":1}\n',
        encoding="utf-8",
    )

    assert store.load().status == "degraded"
    with store.acquire() as state:
        assert state.status == "degraded"

    assert (tmp_path / "transition.fence").exists()


def test_finish_transition_clears_fence_only_after_safe_state_save(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    store.save(ControllerState.stopped())
    store.begin_transition()
    safe = ControllerState(
        status="degraded",
        active_profile=None,
        target_profile="generator-only",
        restore_profile=None,
        last_error="remote cleanup could not be verified",
    )

    with patch(
        "cluster_profiles.state.os.replace", side_effect=OSError("interrupted")
    ), pytest.raises(OSError, match="interrupted"):
        store.finish_transition(safe)

    assert (tmp_path / "transition.fence").exists()

    store.finish_transition(safe)

    assert not (tmp_path / "transition.fence").exists()
    assert store.load() == safe


def test_changed_active_definition_hash_is_persistently_distinguishable(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    before = ControllerState(
        status="active",
        active_profile="agent-full-dual",
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=PROFILE_SHA,
        active_definition_sha256={"deepseek-agent-dual": DEFINITION_A_SHA},
    )
    after = replace(
        before,
        active_definition_sha256={"deepseek-agent-dual": DEFINITION_B_SHA},
    )

    store.save(before)
    first_fingerprints = store.load().active_definition_sha256
    store.save(after)
    second_fingerprints = store.load().active_definition_sha256

    assert first_fingerprints != second_fingerprints
    assert second_fingerprints["deepseek-agent-dual"] == DEFINITION_B_SHA


@pytest.mark.parametrize(
    ("profile_sha", "definition_sha"),
    [
        ("A" * 64, DEFINITION_A_SHA),
        ("a" * 63, DEFINITION_A_SHA),
        (PROFILE_SHA, "B" * 64),
        (PROFILE_SHA, "b" * 63),
    ],
)
def test_active_fingerprints_require_lowercase_sha256(
    profile_sha: str, definition_sha: str
) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        ControllerState(
            status="active",
            active_profile="agent-full-dual",
            target_profile=None,
            restore_profile=None,
            last_error=None,
            active_profile_sha256=profile_sha,
            active_definition_sha256={"deepseek-agent-dual": definition_sha},
        )


def test_persisted_state_requires_fingerprint_schema_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "active",
                "active_profile": "agent-full-dual",
                "target_profile": None,
                "restore_profile": None,
                "last_error": None,
                "boot_ids": {},
                "updated_at": "2026-08-02T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateFormatError, match="invalid controller state fields"):
        StateStore(tmp_path).load()


def test_malformed_state_json_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(StateFormatError, match="state.json"):
        StateStore(tmp_path).load()


def test_acquire_serializes_and_yields_current_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save(ControllerState.stopped())

    with store.acquire() as state:
        assert state.status == "stopped"
        with pytest.raises(LockBusy), StateStore(tmp_path).acquire():
            pass


def test_lock_metadata_records_pid_host_and_timestamp(tmp_path: Path) -> None:
    store = StateStore(tmp_path)

    with store.acquire():
        metadata = json.loads((tmp_path / "switch.lock").read_text(encoding="utf-8"))

    assert metadata["pid"] == os.getpid()
    assert metadata["host"] == socket.gethostname()
    assert metadata["created_at"].endswith("Z")


def test_break_stale_lock_refuses_live_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "switch.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "created_at": "2000-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LockNotStale, match="live PID"):
        StateStore(tmp_path, stale_lock_seconds=60).break_stale_lock()

    assert lock_path.exists()


def test_break_stale_lock_refuses_young_dead_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "switch.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "host": socket.gethostname(),
                "created_at": "2026-08-02T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with patch("cluster_profiles.state._utc_now") as now:
        now.return_value = "2026-08-02T10:00:30Z"
        with pytest.raises(LockNotStale, match="younger"):
            StateStore(tmp_path, stale_lock_seconds=60).break_stale_lock()

    assert lock_path.exists()


def test_break_stale_lock_refuses_foreign_host_even_when_old(tmp_path: Path) -> None:
    lock_path = tmp_path / "switch.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "host": "other-controller",
                "created_at": "2000-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LockNotStale, match="different host"):
        StateStore(tmp_path, stale_lock_seconds=60).break_stale_lock()

    assert lock_path.exists()


def test_break_stale_lock_removes_old_dead_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "switch.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "host": socket.gethostname(),
                "created_at": "2026-08-02T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with patch("cluster_profiles.state._utc_now") as now:
        now.return_value = "2026-08-02T10:02:00Z"
        broken = StateStore(tmp_path, stale_lock_seconds=60).break_stale_lock()

    assert broken is True
    assert not lock_path.exists()


def test_break_stale_lock_rejects_malformed_metadata(tmp_path: Path) -> None:
    (tmp_path / "switch.lock").write_text("{}", encoding="utf-8")

    with pytest.raises(StateFormatError, match="switch.lock"):
        StateStore(tmp_path).break_stale_lock()
