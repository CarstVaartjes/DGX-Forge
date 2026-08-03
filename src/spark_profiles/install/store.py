"""Atomic, restrictive persistence for installation journals."""

from __future__ import annotations

import json
import os
import tempfile
import fcntl
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from spark_profiles.fleet import ManagementEndpoint, NodeId
from spark_profiles.fleet.install_contracts import (
    InstallationJournal,
    InstallationRequest,
    InstallationState,
)


class InstallStoreError(RuntimeError):
    """Installation state cannot be read or written safely."""


class InstallConflict(InstallStoreError):
    """Optimistic installation journal revision did not match."""


class InstallStore:
    def __init__(self, root: Path, *, clock: Callable[[], datetime]) -> None:
        self._root = root
        self._clock = clock
        if root.is_symlink():
            raise InstallStoreError("installation state root must not be a symlink")
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not root.is_dir():
                raise InstallStoreError("installation state root must be a directory")
            root.chmod(0o700)
        except OSError as error:
            raise InstallStoreError(
                f"could not prepare installation state root: {type(error).__name__}"
            ) from None
        self._lock_path = root / ".install-journal.lock"
        if self._lock_path.is_symlink():
            raise InstallStoreError("installation state lock must not be a symlink")
        try:
            descriptor = os.open(
                self._lock_path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
        except OSError as error:
            raise InstallStoreError(
                f"could not prepare installation state lock: {type(error).__name__}"
            ) from None

    @contextmanager
    def _exclusive(self):
        try:
            descriptor = os.open(
                self._lock_path,
                os.O_RDWR | os.O_NOFOLLOW,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise InstallStoreError(
                f"could not lock installation state: {type(error).__name__}"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _path(self, node_id: NodeId) -> Path:
        return self._root / f"{node_id.value}.json"

    @staticmethod
    def _request_payload(request: InstallationRequest) -> dict[str, object]:
        return request.as_public_dict()

    @staticmethod
    def _journal_payload(journal: InstallationJournal) -> dict[str, object]:
        return {
            "request": InstallStore._request_payload(journal.request),
            "state": journal.state,
            "steps": [
                {
                    "state": step.state,
                    "evidence_digest": step.evidence_digest,
                    "completed_at": step.completed_at.isoformat(),
                }
                for step in journal.steps
            ],
            "created_at": journal.created_at.isoformat(),
            "updated_at": journal.updated_at.isoformat(),
            "failure_reason": journal.failure_reason,
        }

    @staticmethod
    def _encoded(journal: InstallationJournal, revision: int) -> bytes:
        payload = {
            "schema_version": 1,
            "revision": revision,
            "journal": InstallStore._journal_payload(journal),
        }
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    def _write_temporary(self, content: bytes) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".install-journal.tmp-",
            dir=self._root,
        )
        temporary = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            return temporary
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise

    def _fsync_root(self) -> None:
        descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def create(self, request: InstallationRequest) -> InstallationJournal:
        journal = InstallationJournal.start(request, at=self._clock())
        target = self._path(request.node_id)
        with self._exclusive():
            if target.is_symlink():
                raise InstallStoreError("installation journal must not be a symlink")
            temporary = self._write_temporary(self._encoded(journal, 0))
            try:
                os.link(temporary, target)
                temporary.unlink()
                self._fsync_root()
            except FileExistsError:
                temporary.unlink(missing_ok=True)
                raise InstallConflict(
                    f"installation journal already exists for {request.node_id.value}"
                ) from None
            except OSError as error:
                temporary.unlink(missing_ok=True)
                raise InstallStoreError(
                    f"could not create installation journal: {type(error).__name__}"
                ) from None
        return journal

    def _load_envelope(self, node_id: NodeId) -> tuple[InstallationJournal, int]:
        target = self._path(node_id)
        if target.is_symlink():
            raise InstallStoreError("installation journal must not be a symlink")
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            raise InstallStoreError(
                f"installation journal does not exist for {node_id.value}"
            ) from None
        except OSError as error:
            raise InstallStoreError(
                f"could not read installation journal: {type(error).__name__}"
            ) from None
        try:
            payload = json.loads(raw)
            journal, revision = self._decode(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise InstallStoreError(
                f"installation journal is invalid: {type(error).__name__}"
            ) from None
        if journal.request.node_id != node_id:
            raise InstallStoreError("installation journal node identity does not match path")
        return journal, revision

    @staticmethod
    def _object(value: object) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("expected object")
        return cast(Mapping[str, Any], value)

    @staticmethod
    def _decode(payload: object) -> tuple[InstallationJournal, int]:
        envelope = InstallStore._object(payload)
        if set(envelope) != {"schema_version", "revision", "journal"}:
            raise ValueError("unknown envelope fields")
        if envelope["schema_version"] != 1:
            raise ValueError("unsupported journal schema")
        revision = envelope["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("invalid journal revision")
        raw_journal = InstallStore._object(envelope["journal"])
        if set(raw_journal) != {
            "request",
            "state",
            "steps",
            "created_at",
            "updated_at",
            "failure_reason",
        }:
            raise ValueError("unknown journal fields")
        raw_request = InstallStore._object(raw_journal["request"])
        if set(raw_request) != {
            "node_id",
            "display_name",
            "host",
            "user",
            "port",
            "credential_ref",
            "labels",
        }:
            raise ValueError("unknown installation request fields")
        labels = InstallStore._object(raw_request["labels"])
        request = InstallationRequest(
            node_id=NodeId.parse(raw_request["node_id"]),
            display_name=raw_request["display_name"],
            endpoint=ManagementEndpoint(
                host=raw_request["host"],
                user=raw_request["user"],
                port=raw_request["port"],
                credential_ref=raw_request["credential_ref"],
            ),
            labels=labels,
        )
        journal = InstallationJournal.start(
            request,
            at=datetime.fromisoformat(raw_journal["created_at"]),
        )
        raw_steps = raw_journal["steps"]
        if not isinstance(raw_steps, list):
            raise TypeError("journal steps must be a list")
        for raw_step_value in raw_steps:
            raw_step = InstallStore._object(raw_step_value)
            if set(raw_step) != {"state", "evidence_digest", "completed_at"}:
                raise ValueError("unknown installation step fields")
            journal = journal.advance(
                cast(InstallationState, raw_step["state"]),
                evidence_digest=raw_step["evidence_digest"],
                at=datetime.fromisoformat(raw_step["completed_at"]),
            )
        serialized_state = raw_journal["state"]
        if serialized_state == "failed":
            reason = raw_journal["failure_reason"]
            if not isinstance(reason, str):
                raise TypeError("failed journal requires failure reason")
            journal = journal.fail(
                reason=reason,
                at=datetime.fromisoformat(raw_journal["updated_at"]),
            )
        elif journal.state != serialized_state or raw_journal["failure_reason"] is not None:
            raise ValueError("journal state does not match its steps")
        if journal.updated_at != datetime.fromisoformat(raw_journal["updated_at"]):
            raise ValueError("journal update timestamp does not match its steps")
        return journal, revision

    def load(self, node_id: NodeId) -> InstallationJournal:
        journal, _ = self._load_envelope(node_id)
        return journal

    def save(
        self,
        journal: InstallationJournal,
        *,
        expected_revision: int,
    ) -> int:
        with self._exclusive():
            _, current_revision = self._load_envelope(journal.request.node_id)
            if current_revision != expected_revision:
                raise InstallConflict(
                    f"expected revision {expected_revision}, current is {current_revision}"
                )
            next_revision = current_revision + 1
            target = self._path(journal.request.node_id)
            temporary = self._write_temporary(self._encoded(journal, next_revision))
            try:
                os.replace(temporary, target)
                target.chmod(0o600)
                self._fsync_root()
            except OSError as error:
                temporary.unlink(missing_ok=True)
                raise InstallStoreError(
                    f"could not save installation journal: {type(error).__name__}"
                ) from None
        return next_revision
