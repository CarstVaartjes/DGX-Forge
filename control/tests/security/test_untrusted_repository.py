from pathlib import Path

import pytest
from dgx_control.repository import RepositoryPolicyError, RepositoryService


def test_repository_root_and_object_store_must_not_be_symlinks(tmp_path: Path) -> None:
    actual = tmp_path / "actual"; actual.mkdir()
    link = tmp_path / "repo"; link.symlink_to(actual)
    with pytest.raises(RepositoryPolicyError):
        RepositoryService(link)


def test_repository_commit_input_is_not_a_revision_expression(tmp_path: Path) -> None:
    root = tmp_path / "repo"; (root / ".git/objects").mkdir(parents=True)
    service = RepositoryService(root)
    with pytest.raises(RepositoryPolicyError, match="40-hex"):
        service.inspect("HEAD^{tree}")


def test_linked_worktree_pointer_requires_a_valid_back_reference(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    admin = tmp_path / "admin"
    (admin / "objects").mkdir(parents=True)
    (root / ".git").write_text(f"gitdir: {admin}\n")

    with pytest.raises(RepositoryPolicyError, match="object store"):
        RepositoryService(root)
