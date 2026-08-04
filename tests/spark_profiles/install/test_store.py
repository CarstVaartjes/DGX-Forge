from __future__ import annotations

import fcntl
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spark_profiles.fleet import ManagementEndpoint, NodeId
from spark_profiles.fleet.install_contracts import (
    InstallationRequest,
)
from spark_profiles.install.store import (
    InstallConflict,
    InstallStore,
    InstallStoreError,
)

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _request() -> InstallationRequest:
    return InstallationRequest(
        node_id=NodeId.parse("spk_00000000000000000000000000000001"),
        display_name="alpha",
        endpoint=ManagementEndpoint(
            host="alpha.local",
            user="operator",
            credential_ref="secret://ssh/admin",
        ),
        labels={"rack": "lab"},
    )


def test_store_round_trips_canonical_journal_with_restrictive_modes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install-state"
    store = InstallStore(root, clock=lambda: NOW)

    journal = store.create(_request())
    loaded = store.load(journal.request.node_id)
    state_file = root / f"{journal.request.node_id.value}.json"
    raw = state_file.read_text()

    assert loaded == journal
    assert json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")) + "\n" == raw
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert "PRIVATE KEY" not in raw
    assert "password" not in raw.lower()


def test_store_rejects_stale_revision(tmp_path: Path) -> None:
    store = InstallStore(tmp_path / "state", clock=lambda: NOW)
    journal = store.create(_request())
    changed = journal.advance(
        "identity-gated",
        evidence_digest="a" * 64,
        at=NOW + timedelta(seconds=1),
    )

    assert store.load_versioned(journal.request.node_id) == (journal, 0)
    assert store.save(changed, expected_revision=0) == 1
    assert store.load_versioned(journal.request.node_id) == (changed, 1)
    with pytest.raises(InstallConflict, match="expected revision 0.*current is 1"):
        store.save(journal, expected_revision=0)


def test_store_round_trips_waiting_operator_state(tmp_path: Path) -> None:
    store = InstallStore(tmp_path / "state", clock=lambda: NOW)
    journal = store.create(_request()).wait(
        reason="verify physical console",
        at=NOW + timedelta(seconds=1),
    )

    store.save(journal, expected_revision=0)

    loaded = store.load(journal.request.node_id)
    assert loaded.waiting_reason == "verify physical console"
    assert loaded.state == "discovered"
    assert loaded.updated_at == NOW + timedelta(seconds=1)


def test_store_round_trips_explicit_retry_state(tmp_path: Path) -> None:
    store = InstallStore(tmp_path / "state", clock=lambda: NOW)
    journal = store.create(_request()).fail(
        reason="temporary failure",
        at=NOW + timedelta(seconds=1),
    )
    store.save(journal, expected_revision=0)
    retried = journal.retry(at=NOW + timedelta(seconds=2))
    store.save(retried, expected_revision=1)

    loaded = store.load(retried.request.node_id)
    assert loaded == retried
    assert loaded.retry_count == 1


def test_store_rejects_duplicate_create_and_missing_load(tmp_path: Path) -> None:
    store = InstallStore(tmp_path / "state", clock=lambda: NOW)
    store.create(_request())

    with pytest.raises(InstallConflict, match="already exists"):
        store.create(_request())
    with pytest.raises(InstallStoreError, match="installation journal does not exist"):
        store.load(NodeId.parse("spk_ffffffffffffffffffffffffffffffff"))


def test_store_rejects_symlink_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(InstallStoreError, match="must not be a symlink"):
        InstallStore(linked, clock=lambda: NOW)


def test_failed_atomic_replace_preserves_previous_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InstallStore(tmp_path / "state", clock=lambda: NOW)
    journal = store.create(_request())
    state_file = tmp_path / "state" / f"{journal.request.node_id.value}.json"
    before = state_file.read_bytes()
    changed = journal.advance(
        "identity-gated",
        evidence_digest="a" * 64,
        at=NOW + timedelta(seconds=1),
    )

    def fail_replace(source: Path | str, destination: Path | str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(InstallStoreError, match="could not save"):
        store.save(changed, expected_revision=0)

    assert state_file.read_bytes() == before
    assert not list((tmp_path / "state").glob(".*.tmp-*"))


def test_store_rejects_tampered_node_identity(tmp_path: Path) -> None:
    store = InstallStore(tmp_path / "state", clock=lambda: NOW)
    journal = store.create(_request())
    state_file = tmp_path / "state" / f"{journal.request.node_id.value}.json"
    payload = json.loads(state_file.read_text())
    payload["journal"]["request"]["node_id"] = "spk_ffffffffffffffffffffffffffffffff"
    state_file.write_text(json.dumps(payload))

    with pytest.raises(InstallStoreError, match="node identity does not match"):
        store.load(journal.request.node_id)


def test_save_holds_process_lock_across_revision_check_and_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InstallStore(tmp_path / "state", clock=lambda: NOW)
    journal = store.create(_request()).advance(
        "identity-gated",
        evidence_digest="a" * 64,
        at=NOW + timedelta(seconds=1),
    )
    entered_write = threading.Event()
    allow_write = threading.Event()
    original_write = store._write_temporary

    def paused_write(content: bytes) -> Path:
        entered_write.set()
        assert allow_write.wait(timeout=2)
        return original_write(content)

    monkeypatch.setattr(store, "_write_temporary", paused_write)
    result: list[object] = []

    def save() -> None:
        try:
            result.append(store.save(journal, expected_revision=0))
        except (AssertionError, OSError, RuntimeError, TypeError, ValueError) as error:
            result.append(error)

    thread = threading.Thread(target=save)
    thread.start()
    assert entered_write.wait(timeout=2)

    lock_path = tmp_path / "state" / ".install-journal.lock"
    with lock_path.open("rb") as lock_file, pytest.raises(BlockingIOError):
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    allow_write.set()
    thread.join(timeout=2)
    assert result == [1]
    assert lock_path.stat().st_mode & 0o777 == 0o600
