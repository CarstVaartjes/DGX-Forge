"""Deterministic local platform publication adapter for tests and mirrors."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_TARGET = re.compile(
    r"platform/releases/"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))/"
    r"(?P<sha256>[0-9a-f]{64})\.json\Z"
)
_CHANNEL = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TARGET_BYTES = 1024 * 1024
_MAX_CHANNEL_BYTES = 64 * 1024


class PlatformPublicationError(RuntimeError):
    """Publication input would violate immutable target or channel state."""


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _parse_canonical(raw: bytes, *, label: str, limit: int) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise PlatformPublicationError(f"{label} bytes are invalid")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlatformPublicationError(f"{label} bytes are invalid") from error
    if not isinstance(value, dict) or raw != _canonical(value):
        raise PlatformPublicationError(f"{label} bytes must be canonical")
    return value


def _directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        details = path.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise PlatformPublicationError("publication root is unsafe") from None
    path.chmod(0o700)


def _write_new(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise PlatformPublicationError("published target is unavailable") from error
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or not 0 < details.st_size <= limit
        ):
            raise PlatformPublicationError("published target is unsafe")
        raw = b""
        while len(raw) < details.st_size:
            chunk = os.read(descriptor, details.st_size - len(raw))
            if not chunk:
                raise PlatformPublicationError("published target is incomplete")
            raw += chunk
        return raw
    finally:
        os.close(descriptor)


class LocalPlatformPublicationStore:
    """Append-only target store with monotonic discovery-channel CAS."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = self.root.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise PlatformPublicationError("publication root is unsafe")
        self.root.chmod(0o700)
        self.targets = self.root / "targets"
        self.receipts = self.root / "receipts"
        self.channels = self.root / "channels"
        for directory in (self.targets, self.receipts, self.channels):
            _directory(directory)
        self._lock_path = self.root / "publication.lock"

    def _locked(self) -> int:
        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor

    def _target_path(self, target_name: str) -> Path:
        match = _TARGET.fullmatch(target_name)
        if match is None:
            raise PlatformPublicationError("immutable target name is invalid")
        return self.targets / match.group("version") / f"{match.group('sha256')}.json"

    def publish_target(
        self,
        target_name: str,
        manifest: bytes,
        deployment_bundle: Mapping[str, object],
        tuf_metadata: Mapping[str, object],
    ) -> dict[str, object]:
        target_path = self._target_path(target_name)
        document = _parse_canonical(
            manifest, label="platform manifest", limit=_MAX_TARGET_BYTES
        )
        match = _TARGET.fullmatch(target_name)
        assert match is not None
        digest = _sha256(manifest)
        if (
            match.group("sha256") != digest
            or document.get("platform_version") != match.group("version")
        ):
            raise PlatformPublicationError("immutable target identity is invalid")
        targets_version = tuf_metadata.get("targets_version")
        retained = tuf_metadata.get("retained_targets")
        if (
            type(targets_version) is not int
            or targets_version < 1
            or not isinstance(retained, list)
            or any(not isinstance(item, str) for item in retained)
            or len(set(retained)) != len(retained)
        ):
            raise PlatformPublicationError("TUF publication metadata is invalid")
        receipt: dict[str, object] = {
            "deployment_bundle": dict(deployment_bundle),
            "retained_targets": retained,
            "schema_version": 1,
            "target_name": target_name,
            "target_sha256": digest,
            "targets_version": targets_version,
        }
        receipt_raw = _canonical(receipt)
        receipt_path = self.receipts / f"{digest}.json"
        lock = self._locked()
        try:
            for predecessor in retained:
                path = self._target_path(predecessor)
                if not path.is_file():
                    raise PlatformPublicationError("retained target is not published")
            if target_path.exists() or receipt_path.exists():
                if (
                    _read_regular(target_path, _MAX_TARGET_BYTES) == manifest
                    and _read_regular(receipt_path, _MAX_CHANNEL_BYTES) == receipt_raw
                ):
                    return receipt
                raise PlatformPublicationError("immutable target cannot be overwritten")
            _directory(target_path.parent)
            _write_new(target_path, manifest)
            try:
                _write_new(receipt_path, receipt_raw)
            except BaseException:
                target_path.unlink(missing_ok=True)
                raise
            return receipt
        finally:
            os.close(lock)

    def read_target(self, target_name: str) -> bytes:
        return _read_regular(self._target_path(target_name), _MAX_TARGET_BYTES)

    def publish_channel(self, channel: str, raw: bytes) -> dict[str, object]:
        if _CHANNEL.fullmatch(channel) is None or channel == "latest":
            raise PlatformPublicationError("channel name is invalid")
        document = _parse_canonical(
            raw, label="channel document", limit=_MAX_CHANNEL_BYTES
        )
        if (
            set(document)
            != {
                "channel",
                "discovery_only",
                "schema_version",
                "target_name",
                "target_sha256",
                "tuf_targets_version",
            }
            or document.get("schema_version") != 1
            or document.get("discovery_only") is not True
            or document.get("channel") != channel
            or _SHA256.fullmatch(str(document.get("target_sha256"))) is None
            or type(document.get("tuf_targets_version")) is not int
            or document["tuf_targets_version"] < 1
        ):
            raise PlatformPublicationError("channel document is invalid")
        target_name = document.get("target_name")
        if not isinstance(target_name, str):
            raise PlatformPublicationError("channel document is invalid")
        try:
            target = self.read_target(target_name)
        except PlatformPublicationError as error:
            raise PlatformPublicationError("channel requires a published target") from error
        if _sha256(target) != document["target_sha256"]:
            raise PlatformPublicationError("channel target binding is invalid")
        receipt: dict[str, object] = {
            "channel": channel,
            "document_sha256": _sha256(raw),
            "schema_version": 1,
            "target_name": target_name,
            "target_sha256": document["target_sha256"],
            "targets_version": document["tuf_targets_version"],
        }
        channel_path = self.channels / f"{channel}.json"
        lock = self._locked()
        try:
            if channel_path.exists():
                current_raw = _read_regular(channel_path, _MAX_CHANNEL_BYTES)
                if current_raw == raw:
                    return receipt
                current = _parse_canonical(
                    current_raw, label="channel document", limit=_MAX_CHANNEL_BYTES
                )
                if document["tuf_targets_version"] <= current["tuf_targets_version"]:
                    raise PlatformPublicationError(
                        "channel targets version must advance monotonically"
                    )
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.channels,
                    prefix=f".{channel}.",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(raw)
                    temporary.flush()
                    os.fchmod(temporary.fileno(), 0o600)
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, channel_path)
                parent = os.open(
                    self.channels, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                )
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return receipt
        finally:
            os.close(lock)
