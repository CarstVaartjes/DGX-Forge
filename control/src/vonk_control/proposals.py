"""Canonical proposal previews produced in disposable exact-base worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .repository import RepositoryPolicyError, RepositoryService
from .serializers import serialize_document


class StaleBaseCommit(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentChange:
    path: str
    document: Mapping[str, object]


@dataclass(frozen=True)
class ProposalPreview:
    actor: str
    base_commit: str
    patch: bytes
    affected_documents: tuple[str, ...]
    validation_results: tuple[str, ...]
    digest: str


class ProposalService:
    def __init__(self, repository: RepositoryService, *, head: Callable[[], str]) -> None:
        self._repository = repository
        self._head = head
        self._previews: dict[str, ProposalPreview] = {}

    def head(self) -> str:
        return self._head()

    def _git(self, *arguments: str, cwd: Path | None = None) -> bytes:
        completed = subprocess.run(
            (
                "git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never",
                "-C", str(cwd or self._repository.root), *arguments,
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
            shell=False,
            env=os.environ | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
        if completed.returncode != 0:
            raise RepositoryPolicyError("Git proposal operation failed")
        if len(completed.stdout) > 4_194_304:
            raise RepositoryPolicyError("proposal patch exceeds the safety limit")
        return completed.stdout

    def preview(
        self,
        actor: str,
        base_commit: str,
        changes: Sequence[DocumentChange],
    ) -> ProposalPreview:
        if not actor.strip() or not changes:
            raise ValueError("proposal actor and changes are required")
        # Object inspection validates the full commit before any worktree operation.
        self._repository.inspect(base_commit)
        normalized: dict[str, bytes] = {}
        for change in changes:
            path = self._repository.validate_path(change.path)
            if path in normalized:
                raise ValueError(f"duplicate proposal path: {path}")
            normalized[path] = serialize_document(path, change.document)
        ordered_paths = tuple(sorted(normalized))
        temporary_root = Path(tempfile.mkdtemp(prefix="vonk-proposal-"))
        worktree = temporary_root / "worktree"
        try:
            worktree.mkdir(mode=0o700)
            self._git("init", "-q", cwd=worktree)
            alternates = worktree / ".git/objects/info/alternates"
            alternates.write_text(str(self._repository.object_store) + "\n")
            alternates.chmod(0o600)
            self._git("checkout", "--detach", base_commit, cwd=worktree)
            for path in ordered_paths:
                target = worktree / path
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    raise RepositoryPolicyError("proposal target must not be a symlink")
                target.write_bytes(normalized[path])
            self._git("add", "-N", "--", *ordered_paths, cwd=worktree)
            patch = self._git(
                "diff", "--binary", "--no-ext-diff", "--no-color", "--", *ordered_paths,
                cwd=worktree,
            )
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)
        digest_input = json.dumps(
            {"base_commit": base_commit, "documents": ordered_paths},
            sort_keys=True,
            separators=(",", ":"),
        ).encode() + b"\n" + patch
        digest = hashlib.sha256(digest_input).hexdigest()
        preview = ProposalPreview(
            actor=actor,
            base_commit=base_commit,
            patch=patch,
            affected_documents=ordered_paths,
            validation_results=("typed-syntax:passed", "path-policy:passed"),
            digest=digest,
        )
        self._previews[digest] = preview
        return preview

    def apply(self, digest: str) -> ProposalPreview:
        try:
            preview = self._previews[digest]
        except KeyError:
            raise ValueError("unknown proposal digest") from None
        if self.head() != preview.base_commit:
            raise StaleBaseCommit("proposal base commit is no longer repository HEAD")
        return preview
