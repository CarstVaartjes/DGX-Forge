from __future__ import annotations

import subprocess

import pytest
from vonk_control.git_content import read_commit_file


def _git(repository, *arguments):
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_read_commit_file_ignores_worktree_changes(tmp_path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    inventory = repository / "inventory"
    inventory.mkdir()
    plan = inventory / "reconciliation.json"
    plan.write_text('{"accepted":true}\n')
    _git(repository, "add", "inventory/reconciliation.json")
    _git(repository, "commit", "-qm", "accepted plan")
    commit = _git(repository, "rev-parse", "HEAD")
    plan.write_text('{"accepted":false}\n')

    assert read_commit_file(
        repository,
        commit,
        "inventory/reconciliation.json",
    ) == b'{"accepted":true}\n'


def test_read_commit_file_rejects_traversal_and_oversized_blobs(tmp_path) -> None:
    with pytest.raises(ValueError, match="reference"):
        read_commit_file(tmp_path, "a" * 40, "../secret")

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    (repository / "large.txt").write_text("too large")
    _git(repository, "add", "large.txt")
    _git(repository, "commit", "-qm", "large")
    commit = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="oversized"):
        read_commit_file(repository, commit, "large.txt", maximum_bytes=2)
