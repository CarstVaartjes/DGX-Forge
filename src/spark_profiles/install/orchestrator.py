"""Resumable, one-target-at-a-time Spark onboarding orchestration."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from spark_profiles.fleet import NodeId
from spark_profiles.fleet._redact import redact_message
from spark_profiles.fleet.install_contracts import (
    InstallationJournal,
    InstallationRequest,
    InstallationState,
)

from .store import InstallStore


@dataclass(frozen=True)
class StepResult:
    stdout: bytes
    stderr: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.stdout, bytes) or not isinstance(self.stderr, bytes):
            raise TypeError("installation step output must be bytes")


class WaitForOperator(RuntimeError):
    """A safe gate requires a trusted administrator action before resuming."""


class EvidenceStoreError(RuntimeError):
    """Installation evidence cannot be persisted safely."""


def _sanitized(value: bytes) -> bytes:
    text = value.decode("utf-8", errors="replace")
    return redact_message(text).encode("utf-8")


class FileEvidenceStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        if root.is_symlink():
            raise EvidenceStoreError("evidence root must not be a symlink")
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root.chmod(0o700)
        except OSError as error:
            raise EvidenceStoreError(
                f"could not prepare evidence root: {type(error).__name__}"
            ) from None

    @staticmethod
    def _content(
        node_id: NodeId,
        step: str,
        attempt: int,
        result: StepResult,
    ) -> bytes:
        payload = {
            "schema_version": 1,
            "node_id": node_id.value,
            "step": step,
            "attempt": attempt,
            "stdout_base64": base64.b64encode(_sanitized(result.stdout)).decode(),
            "stderr_base64": base64.b64encode(_sanitized(result.stderr)).decode(),
        }
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

    def save(
        self,
        node_id: NodeId,
        step: str,
        attempt: int,
        result: StepResult,
    ) -> str:
        if step not in _STEP_NAMES or attempt <= 0:
            raise EvidenceStoreError("invalid evidence identity")
        node_root = self._root / node_id.value
        if node_root.is_symlink():
            raise EvidenceStoreError("node evidence path must not be a symlink")
        node_root.mkdir(mode=0o700, exist_ok=True)
        node_root.chmod(0o700)
        content = self._content(node_id, step, attempt, result)
        digest = hashlib.sha256(content).hexdigest()
        target = node_root / f"{attempt:04d}-{step}-{digest}.json"
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=".evidence.tmp-",
            dir=node_root,
        )
        temporary = Path(raw_temporary)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != content:
                    raise EvidenceStoreError("existing evidence conflicts") from None
            temporary.unlink(missing_ok=True)
            target.chmod(0o600)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return digest


_TRANSITIONS: dict[InstallationState, tuple[str, InstallationState]] = {
    "discovered": ("identity", "identity-gated"),
    "identity-gated": ("pre-inventory", "inventoried"),
    "inventoried": ("public-key", "key-installed"),
    "key-installed": ("ssh-hardening", "hardened"),
    "hardened": ("node-policy", "policy-applied"),
    "policy-applied": ("post-inventory", "post-inventoried"),
    "post-inventoried": ("acceptance", "accepted"),
}
_STEP_NAMES = frozenset(step for step, _ in _TRANSITIONS.values())
StepHandler = Callable[[InstallationRequest], StepResult]


class NodeInstaller:
    def __init__(
        self,
        *,
        store: InstallStore,
        evidence_store: FileEvidenceStore,
        handlers: Mapping[str, StepHandler],
        clock: Callable[[], datetime],
    ) -> None:
        if set(handlers) != _STEP_NAMES:
            raise ValueError("installation handler registry must match declared gates")
        self._store = store
        self._evidence_store = evidence_store
        self._handlers = MappingProxyType(dict(handlers))
        self._clock = clock

    def start(self, request: InstallationRequest) -> InstallationJournal:
        return self._store.create(request)

    def retry(self, node_id: NodeId) -> InstallationJournal:
        journal, revision = self._store.load_versioned(node_id)
        retried = journal.retry(at=self._clock())
        self._store.save(retried, expected_revision=revision)
        return retried

    def resume(self, node_id: NodeId) -> InstallationJournal:
        journal, revision = self._store.load_versioned(node_id)
        resumed = journal.resume(at=self._clock())
        self._store.save(resumed, expected_revision=revision)
        return resumed

    def run(
        self,
        node_id: NodeId,
        *,
        until: str | None = None,
    ) -> InstallationJournal:
        if until is not None and until not in _STEP_NAMES:
            raise ValueError(f"unknown installation stop gate: {until}")
        journal, revision = self._store.load_versioned(node_id)
        if journal.waiting_reason is not None or journal.state in {"accepted", "failed"}:
            return journal

        while journal.state not in {"accepted", "failed"}:
            step, next_state = _TRANSITIONS[journal.state]
            handler = self._handlers[step]
            try:
                result = handler(journal.request)
                digest = self._evidence_store.save(
                    node_id,
                    step,
                    journal.retry_count + 1,
                    result,
                )
                journal = journal.advance(
                    next_state,
                    evidence_digest=digest,
                    at=self._clock(),
                )
            except WaitForOperator as error:
                journal = journal.wait(reason=str(error), at=self._clock())
                self._store.save(journal, expected_revision=revision)
                return journal
            except Exception as error:
                journal = journal.fail(
                    reason=f"{type(error).__name__}: {error}",
                    at=self._clock(),
                )
                self._store.save(journal, expected_revision=revision)
                return journal
            revision = self._store.save(journal, expected_revision=revision)
            if step == until:
                return journal
        return journal
