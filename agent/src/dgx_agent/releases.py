"""Typed, content-addressed Spark release installation boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from pathlib import PurePosixPath
import stat
import tempfile
import time
from typing import Any, Mapping
from typing import Protocol
import unicodedata
from urllib.parse import urlsplit

from .deadlines import DeadlineBindingError, MonotonicDeadline


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_RELEASE_FIELDS = frozenset(
    {
        "schema_version",
        "target_name",
        "oci_manifest_digest",
        "target_digest",
        "provenance_digest",
        "adapter_id",
    }
)
_DESCRIPTOR_FIELDS = frozenset(
    {
        "schema_version",
        "target_name",
        "target_digest",
        "target_length",
        "registry_origin",
        "repository",
        "oci_manifest_digest",
        "provenance_digest",
        "adapter_id",
        "adapter_version",
        "architecture",
        "agent_min_version",
        "agent_max_version",
        "protocol_min_version",
        "protocol_max_version",
        "members",
    }
)
_MEMBER_FIELDS = frozenset({"path", "sha256", "size", "mode", "uid", "gid"})
_REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


class ReleaseValidationError(ValueError):
    """A release request or signed descriptor is invalid."""


class ReleaseInstallError(RuntimeError):
    error_code = "release_install_failed"


class ReleaseDisposition(StrEnum):
    READY = "ready"
    SAFE_TO_RESUME = "safe-to-resume"
    COMPLETED = "completed"
    OPERATOR_INTERVENTION = "operator-intervention"


@dataclass(frozen=True)
class ReleaseEvidence:
    status: str
    release_digest: str
    manifest_digest: str
    adapter_id: str

    def __post_init__(self) -> None:
        if self.status not in {"installed", "already-installed"}:
            raise ReleaseValidationError("release evidence status is invalid")
        _digest(self.release_digest, "release digest")
        if not _OCI_DIGEST.fullmatch(self.manifest_digest):
            raise ReleaseValidationError("release manifest digest is invalid")
        _token(self.adapter_id, "adapter ID")

    def to_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "release_digest": self.release_digest,
            "manifest_digest": self.manifest_digest,
            "adapter_id": self.adapter_id,
        }


@dataclass(frozen=True)
class ReleaseInspection:
    disposition: ReleaseDisposition
    evidence: ReleaseEvidence | None = None


@dataclass(frozen=True)
class ReleaseMember:
    path: str
    sha256: str
    size: int
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ReleaseDescriptor:
    schema_version: int
    target_name: str
    target_digest: str
    target_length: int
    registry_origin: str
    repository: str
    oci_manifest_digest: str
    provenance_digest: str
    adapter_id: str
    adapter_version: str
    architecture: str
    agent_min_version: str
    agent_max_version: str
    protocol_min_version: int
    protocol_max_version: int
    members: tuple[ReleaseMember, ...]

    @classmethod
    def parse(cls, document: Mapping[str, Any]) -> "ReleaseDescriptor":
        if not isinstance(document, Mapping) or set(document) != _DESCRIPTOR_FIELDS:
            raise ReleaseValidationError("release descriptor fields are invalid")
        if document["schema_version"] != 1 or isinstance(document["schema_version"], bool):
            raise ReleaseValidationError("release descriptor version is invalid")
        target_length = _bounded_int(document["target_length"], 1, 1 << 30, "target length")
        protocol_min = _bounded_int(document["protocol_min_version"], 1, 1, "protocol range")
        protocol_max = _bounded_int(document["protocol_max_version"], 1, 1, "protocol range")
        origin = _https_origin(document["registry_origin"])
        repository = document["repository"]
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise ReleaseValidationError("OCI repository is invalid")
        members = _members(document["members"])
        if target_length != sum(member.size for member in members):
            raise ReleaseValidationError("target length does not match members")
        descriptor = cls(
            1,
            _token(document["target_name"], "target name"),
            _digest(document["target_digest"], "target digest"),
            target_length,
            origin,
            repository,
            _oci_digest(document["oci_manifest_digest"]),
            _digest(document["provenance_digest"], "provenance digest"),
            _token(document["adapter_id"], "adapter ID"),
            _version(document["adapter_version"], "adapter version"),
            _token(document["architecture"], "architecture"),
            _version(document["agent_min_version"], "minimum agent version"),
            _version(document["agent_max_version"], "maximum agent version"),
            protocol_min,
            protocol_max,
            members,
        )
        if len(_receipt_bytes(descriptor)) > 64 * 1024:
            raise ReleaseValidationError("release descriptor receipt is too large")
        return descriptor

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target_name": self.target_name,
            "target_digest": self.target_digest,
            "target_length": self.target_length,
            "registry_origin": self.registry_origin,
            "repository": self.repository,
            "oci_manifest_digest": self.oci_manifest_digest,
            "provenance_digest": self.provenance_digest,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "architecture": self.architecture,
            "agent_min_version": self.agent_min_version,
            "agent_max_version": self.agent_max_version,
            "protocol_min_version": self.protocol_min_version,
            "protocol_max_version": self.protocol_max_version,
            "members": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size": item.size,
                    "mode": item.mode,
                    "uid": item.uid,
                    "gid": item.gid,
                }
                for item in self.members
            ],
        }

    def agrees_with(self, request: ReleaseRequest) -> bool:
        return (
            self.target_name == request.target_name
            and self.oci_manifest_digest == request.oci_manifest_digest
            and self.target_digest == request.target_digest
            and self.provenance_digest == request.provenance_digest
            and self.adapter_id == request.adapter_id
        )


class ReleaseTrustBoundary(Protocol):
    def authorize(
        self, request: ReleaseRequest, deadline: MonotonicDeadline
    ) -> ReleaseDescriptor: ...


class ReleaseTransportBoundary(Protocol):
    def pull(
        self,
        descriptor: ReleaseDescriptor,
        destination: Path,
        deadline: MonotonicDeadline,
    ) -> None: ...


class ReleaseInstaller:
    def __init__(
        self,
        trust: ReleaseTrustBoundary,
        transport: ReleaseTransportBoundary,
        releases_root: Path,
        staging_root: Path,
    ) -> None:
        self._trust = trust
        self._transport = transport
        self._releases_root = Path(releases_root)
        self._staging_root = Path(staging_root)

    def install(
        self, request: ReleaseRequest, deadline: datetime | MonotonicDeadline
    ) -> ReleaseEvidence:
        fixed_deadline = _bind_deadline(deadline)
        _deadline(fixed_deadline)
        descriptor = self._trust.authorize(request, fixed_deadline)
        _deadline(fixed_deadline)
        _secure_root(self._releases_root, fixed_deadline)
        _secure_root(self._staging_root, fixed_deadline)
        _deadline(fixed_deadline)
        releases_metadata = self._releases_root.stat()
        _deadline(fixed_deadline)
        staging_root_metadata = self._staging_root.stat()
        _deadline(fixed_deadline)
        if releases_metadata.st_dev != staging_root_metadata.st_dev:
            raise ReleaseInstallError("release staging is not on the install filesystem")
        destination = self._releases_root / request.target_digest
        _deadline(fixed_deadline)
        lock_fd = os.open(
            self._releases_root / f".install-{request.target_digest}.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            _deadline(fixed_deadline)
            _acquire_lock(lock_fd, fixed_deadline)
            _deadline(fixed_deadline)
            try:
                _deadline(fixed_deadline)
                os.stat(destination, follow_symlinks=False)
                _deadline(fixed_deadline)
            except FileNotFoundError:
                pass
            else:
                _verify_installed(
                    self._releases_root, destination.name, descriptor,
                    fixed_deadline,
                )
                return ReleaseEvidence(
                    "already-installed",
                    request.target_digest,
                    request.oci_manifest_digest,
                    request.adapter_id,
                )
            _deadline(fixed_deadline)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".install-{request.target_digest}-",
                    dir=self._staging_root,
                )
            )
            _deadline(fixed_deadline)
            os.chmod(staging, 0o700)
            _deadline(fixed_deadline)
            staging_metadata = staging.stat()
            _deadline(fixed_deadline)
            staging_fd = os.open(
                staging,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            published = False
            try:
                _deadline(fixed_deadline)
                self._transport.pull(descriptor, staging, fixed_deadline)
                _deadline(fixed_deadline)
                _require_path_identity(staging, staging_metadata, fixed_deadline)
                _verify_release_tree_fd(
                    staging_fd, descriptor, deadline=fixed_deadline
                )
                _write_receipt_fd(staging_fd, descriptor, fixed_deadline)
                if verify_installed_release_fd(
                    staging_fd, fixed_deadline
                ) != descriptor:
                    raise ReleaseInstallError("staged release receipt does not match")
                _fsync_tree_fd(staging_fd, fixed_deadline)
                _require_path_identity(staging, staging_metadata, fixed_deadline)
                try:
                    _deadline(fixed_deadline)
                    _rename_noreplace(staging, destination)
                    published = True
                    _fsync_directory(
                        self._releases_root,
                        fixed_deadline,
                        commit_started=True,
                    )
                    destination_fd = os.open(
                        destination,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                    )
                    try:
                        _deadline(fixed_deadline)
                        installed_metadata = os.fstat(destination_fd)
                        _deadline(fixed_deadline)
                        if (
                            installed_metadata.st_dev,
                            installed_metadata.st_ino,
                        ) != (staging_metadata.st_dev, staging_metadata.st_ino):
                            raise ReleaseInstallError(
                                "published release identity changed"
                            )
                        try:
                            installed_descriptor = verify_installed_release_fd(
                                destination_fd, fixed_deadline
                            )
                        except Exception:
                            # Publication is already durable. An elapsed
                            # deadline leaves the verified staged inode in
                            # place for idempotent re-verification on retry.
                            _deadline(fixed_deadline)
                            _remove_bound_tree(
                                self._releases_root,
                                destination.name,
                                (
                                    installed_metadata.st_dev,
                                    installed_metadata.st_ino,
                                ),
                            )
                            raise
                        if installed_descriptor != descriptor:
                            raise ReleaseInstallError(
                                "published release does not match"
                            )
                    finally:
                        os.close(destination_fd)
                except FileExistsError:
                    _verify_installed(
                        self._releases_root, destination.name, descriptor,
                        fixed_deadline,
                    )
            except ReleaseInstallError:
                raise
            except Exception as error:
                raise ReleaseInstallError("release installation failed") from error
            finally:
                os.close(staging_fd)
                if not published:
                    _remove_bound_tree(
                        self._staging_root,
                        staging.name,
                        (staging_metadata.st_dev, staging_metadata.st_ino),
                    )
        finally:
            os.close(lock_fd)
        return ReleaseEvidence(
            "installed" if published else "already-installed",
            request.target_digest,
            request.oci_manifest_digest,
            request.adapter_id,
        )

    def inspect(
        self,
        request: ReleaseRequest,
        deadline: datetime | MonotonicDeadline,
    ) -> ReleaseInspection:
        try:
            fixed_deadline = _bind_deadline(deadline)
            descriptor = self._trust.authorize(
                request, fixed_deadline,
            )
            destination = self._releases_root / request.target_digest
            if destination.exists():
                _verify_installed(
                    self._releases_root, destination.name, descriptor,
                    fixed_deadline,
                )
                return ReleaseInspection(
                    ReleaseDisposition.COMPLETED,
                    ReleaseEvidence(
                        "already-installed",
                        request.target_digest,
                        request.oci_manifest_digest,
                        request.adapter_id,
                    ),
                )
            candidates = tuple(
                self._staging_root.glob(f".install-{request.target_digest}-*")
            )
            if len(candidates) == 1:
                verify_release_tree(
                    candidates[0], descriptor, deadline=fixed_deadline
                )
                return ReleaseInspection(ReleaseDisposition.SAFE_TO_RESUME)
            if not candidates:
                return ReleaseInspection(ReleaseDisposition.OPERATOR_INTERVENTION)
        except Exception:
            pass
        return ReleaseInspection(ReleaseDisposition.OPERATOR_INTERVENTION)


def verify_release_tree(
    root: Path,
    descriptor: ReleaseDescriptor,
    *,
    _allow_receipt: bool = False,
    deadline: MonotonicDeadline | None = None,
) -> None:
    _deadline_step(deadline)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _deadline_step(deadline)
        _verify_release_tree_fd(
            root_fd, descriptor, allow_receipt=_allow_receipt, deadline=deadline
        )
    finally:
        os.close(root_fd)


def _verify_release_tree_fd(
    root_fd: int,
    descriptor: ReleaseDescriptor,
    *,
    allow_receipt: bool = False,
    deadline: MonotonicDeadline | None = None,
) -> None:
    expected = {member.path: member for member in descriptor.members}
    expected_directories = {""}
    for member in descriptor.members:
        parent = PurePosixPath(member.path).parent
        while str(parent) != ".":
            expected_directories.add(str(parent))
            parent = parent.parent
    seen: set[str] = set()
    identities: set[str] = set()
    count = 0
    try:
        _deadline_step(deadline)
        root_metadata = os.fstat(root_fd)
        _deadline_step(deadline)
        _verify_directory_metadata(root_metadata)
        def walk(directory_fd: int, prefix: str) -> None:
            nonlocal count
            for name in _deadline_names(directory_fd, deadline):
                count += 1
                if count > 512:
                    raise ReleaseInstallError("release member count is excessive")
                relative = name if not prefix else f"{prefix}/{name}"
                identity = unicodedata.normalize("NFC", relative).casefold()
                if identity in identities:
                    raise ReleaseInstallError("release paths collide")
                identities.add(identity)
                _deadline_step(deadline)
                metadata = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                _deadline_step(deadline)
                if relative == ".install-receipt.json" and allow_receipt:
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or stat.S_IMODE(metadata.st_mode) != 0o400
                    ):
                        raise ReleaseInstallError("release receipt metadata is invalid")
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in expected_directories:
                        raise ReleaseInstallError("release contains an unexpected directory")
                    _verify_directory_metadata(metadata)
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    try:
                        _deadline_step(deadline)
                        walk(child, relative)
                    finally:
                        os.close(child)
                    continue
                member = expected.get(relative)
                if member is None:
                    raise ReleaseInstallError("release contains an unexpected member")
                _verify_member(directory_fd, name, member, deadline)
                seen.add(relative)

        walk(root_fd, "")
    except OSError as error:
        raise ReleaseInstallError("release tree is unsafe") from error
    if seen != set(expected):
        raise ReleaseInstallError("release member set is incomplete")


def _verify_member(
    directory_fd: int,
    name: str,
    member: ReleaseMember,
    deadline: MonotonicDeadline | None = None,
) -> None:
    _deadline_step(deadline)
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        _deadline_step(deadline)
        metadata = os.fstat(descriptor)
        _deadline_step(deadline)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != member.uid
            or metadata.st_gid != member.gid
            or stat.S_IMODE(metadata.st_mode) != member.mode
            or metadata.st_size != member.size
            or (metadata.st_size > 0 and metadata.st_blocks * 512 < metadata.st_size)
        ):
            raise ReleaseInstallError("release member metadata is invalid")
        digest = hashlib.sha256()
        total = 0
        while True:
            _deadline_step(deadline)
            chunk = os.read(
                descriptor, min(64 * 1024, member.size - total + 1)
            )
            _deadline_step(deadline)
            if not chunk:
                break
            total += len(chunk)
            if total > member.size:
                raise ReleaseInstallError("release member size changed")
            digest.update(chunk)
        if total != member.size or digest.hexdigest() != member.sha256:
            raise ReleaseInstallError("release member digest is invalid")
    finally:
        os.close(descriptor)


def _verify_directory_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_gid not in {0, os.getegid()}
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700
    ):
        raise ReleaseInstallError("release directory metadata is invalid")


def _receipt_bytes(descriptor: ReleaseDescriptor) -> bytes:
    document = {"schema_version": 1, "release": descriptor.to_mapping()}
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _unique_receipt_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ReleaseInstallError(
                "installed release receipt contains duplicate fields"
            )
        document[key] = value
    return document


def _write_receipt(root: Path, descriptor: ReleaseDescriptor) -> None:
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _write_receipt_fd(root_fd, descriptor)
    finally:
        os.close(root_fd)


def _write_receipt_fd(
    root_fd: int,
    descriptor: ReleaseDescriptor,
    deadline: MonotonicDeadline | None = None,
) -> None:
    data = _receipt_bytes(descriptor)
    _deadline_step(deadline)
    fd = os.open(
        ".install-receipt.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o400,
        dir_fd=root_fd,
    )
    try:
        _deadline_step(deadline)
        offset = 0
        while offset < len(data):
            _deadline_step(deadline)
            written = os.write(fd, data[offset:])
            _deadline_step(deadline)
            if written <= 0:
                raise ReleaseInstallError("release receipt write was incomplete")
            offset += written
        _deadline_step(deadline)
        os.fsync(fd)
        _deadline_step(deadline)
    finally:
        os.close(fd)


def _verify_installed(
    parent: Path,
    name: str,
    descriptor: ReleaseDescriptor,
    deadline: MonotonicDeadline | None = None,
) -> None:
    parent_fd = -1
    root_fd = -1
    try:
        _deadline_step(deadline)
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        _deadline_step(deadline)
        root_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        _deadline_step(deadline)
        metadata = os.fstat(root_fd)
        _deadline_step(deadline)
        installed_descriptor = verify_installed_release_fd(root_fd, deadline)
        if installed_descriptor != descriptor:
            raise ReleaseInstallError("installed release receipt does not match")
        _deadline_step(deadline)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _deadline_step(deadline)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise ReleaseInstallError("installed release identity changed")
    except ReleaseInstallError:
        raise
    except Exception as error:
        raise ReleaseInstallError("installed release is invalid") from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def verify_installed_release(root: Path) -> ReleaseDescriptor:
    """Return the signed descriptor only when its receipt and tree still agree."""
    root_fd = -1
    try:
        root_fd = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        return verify_installed_release_fd(root_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def verify_installed_release_fd(
    root_fd: int, deadline: MonotonicDeadline | None = None
) -> ReleaseDescriptor:
    """Verify receipt and members through one already-open release identity."""
    try:
        _deadline_step(deadline)
        receipt_fd = os.open(
            ".install-receipt.json",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            _deadline_step(deadline)
            metadata = os.fstat(receipt_fd)
            _deadline_step(deadline)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
                raise ReleaseInstallError("installed release receipt is unsafe")
            _deadline_step(deadline)
            raw = os.read(receipt_fd, 64 * 1024 + 1)
            _deadline_step(deadline)
        finally:
            os.close(receipt_fd)
        document = json.loads(raw, object_pairs_hook=_unique_receipt_object)
        if not isinstance(document, dict) or set(document) != {"schema_version", "release"}:
            raise ReleaseInstallError("installed release receipt is invalid")
        if document["schema_version"] != 1:
            raise ReleaseInstallError("installed release receipt is invalid")
        descriptor = ReleaseDescriptor.parse(document["release"])
        if raw != _receipt_bytes(descriptor):
            raise ReleaseInstallError("installed release receipt does not match")
        _verify_release_tree_fd(
            root_fd, descriptor, allow_receipt=True, deadline=deadline
        )
        return descriptor
    except ReleaseInstallError:
        raise
    except Exception as error:
        raise ReleaseInstallError("installed release is invalid") from error


def _secure_root(
    path: Path, deadline: MonotonicDeadline | None = None
) -> None:
    if not path.is_absolute():
        raise ReleaseInstallError("release root is invalid")
    _deadline_step(deadline)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _deadline_step(deadline)
    metadata = path.lstat()
    _deadline_step(deadline)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ReleaseInstallError("release root is unsafe")


def _bind_deadline(deadline: datetime | MonotonicDeadline) -> MonotonicDeadline:
    try:
        return MonotonicDeadline.bind(deadline)
    except DeadlineBindingError as error:
        raise ReleaseInstallError("release deadline has elapsed") from error


def _deadline(deadline: MonotonicDeadline) -> None:
    try:
        deadline.check()
    except DeadlineBindingError as error:
        raise ReleaseInstallError("release deadline has elapsed")


def _deadline_step(deadline: MonotonicDeadline | None) -> None:
    if deadline is not None:
        _deadline(deadline)


def _deadline_names(
    directory_fd: int, deadline: MonotonicDeadline | None
):
    _deadline_step(deadline)
    entries = os.scandir(directory_fd)
    try:
        while True:
            _deadline_step(deadline)
            try:
                entry = next(entries)
            except StopIteration:
                break
            _deadline_step(deadline)
            yield entry.name
    finally:
        entries.close()


def _acquire_lock(descriptor: int, deadline: MonotonicDeadline) -> None:
    while True:
        _deadline(deadline)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline.remaining()
            if remaining <= 0:
                raise ReleaseInstallError("release deadline has elapsed")
            time.sleep(min(0.01, remaining))


def _fsync_tree(root: Path) -> None:
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _fsync_tree_fd(root_fd)
    finally:
        os.close(root_fd)


def _fsync_tree_fd(
    directory_fd: int, deadline: MonotonicDeadline | None = None
) -> None:
    for name in _deadline_names(directory_fd, deadline):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _deadline_step(deadline)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | (os.O_DIRECTORY if stat.S_ISDIR(metadata.st_mode) else 0),
            dir_fd=directory_fd,
        )
        try:
            _deadline_step(deadline)
            if stat.S_ISDIR(metadata.st_mode):
                _fsync_tree_fd(descriptor, deadline)
            _deadline_step(deadline)
            os.fsync(descriptor)
            _deadline_step(deadline)
        finally:
            os.close(descriptor)
    _deadline_step(deadline)
    os.fsync(directory_fd)
    _deadline_step(deadline)


def _fsync_directory(
    path: Path,
    deadline: MonotonicDeadline | None = None,
    *,
    commit_started: bool = False,
) -> None:
    if not commit_started:
        _deadline_step(deadline)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not commit_started:
            _deadline_step(deadline)
        os.fsync(descriptor)
        _deadline_step(deadline)
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, destination: Path) -> None:
    renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        value = ctypes.get_errno()
        if value == errno.EEXIST:
            raise FileExistsError(value, os.strerror(value), destination)
        raise OSError(value, os.strerror(value), destination)


def _require_path_identity(
    path: Path,
    expected: os.stat_result,
    deadline: MonotonicDeadline | None = None,
) -> None:
    try:
        _deadline_step(deadline)
        current = os.stat(path, follow_symlinks=False)
        _deadline_step(deadline)
    except OSError as error:
        raise ReleaseInstallError("release staging identity changed") from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ReleaseInstallError("release staging identity changed")


def _remove_bound_tree(parent: Path, name: str, identity: tuple[int, int]) -> None:
    """Remove only the private staging inode created by this installer."""
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (metadata.st_dev, metadata.st_ino) != identity or not stat.S_ISDIR(metadata.st_mode):
            return
        _remove_directory_contents(parent_fd, name, identity)
    finally:
        os.close(parent_fd)


def _remove_directory_contents(
    parent_fd: int, name: str, identity: tuple[int, int]
) -> None:
    directory_fd = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        if (os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino) != identity:
            return
        for child_name in os.listdir(directory_fd):
            child = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
            child_identity = (child.st_dev, child.st_ino)
            if stat.S_ISDIR(child.st_mode):
                _remove_directory_contents(directory_fd, child_name, child_identity)
            else:
                os.unlink(child_name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        os.rmdir(name, dir_fd=parent_fd)


@dataclass(frozen=True)
class ReleaseRequest:
    schema_version: int
    target_name: str
    oci_manifest_digest: str
    target_digest: str
    provenance_digest: str
    adapter_id: str

    @classmethod
    def parse(cls, document: Mapping[str, Any]) -> "ReleaseRequest":
        if not isinstance(document, Mapping) or set(document) != _RELEASE_FIELDS:
            raise ReleaseValidationError("release request fields are invalid")
        if document["schema_version"] != 1 or isinstance(
            document["schema_version"], bool
        ):
            raise ReleaseValidationError("release request version is invalid")
        target_name = _token(document["target_name"], "target name")
        adapter_id = _token(document["adapter_id"], "adapter ID")
        manifest_digest = document["oci_manifest_digest"]
        target_digest = document["target_digest"]
        provenance_digest = document["provenance_digest"]
        if not isinstance(manifest_digest, str) or not _OCI_DIGEST.fullmatch(
            manifest_digest
        ):
            raise ReleaseValidationError("OCI manifest digest is invalid")
        for value, name in (
            (target_digest, "target digest"),
            (provenance_digest, "provenance digest"),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ReleaseValidationError(f"{name} is invalid")
        return cls(
            1,
            target_name,
            manifest_digest,
            target_digest,
            provenance_digest,
            adapter_id,
        )


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ReleaseValidationError(f"{name} is invalid")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ReleaseValidationError(f"{name} is invalid")
    return value


def _oci_digest(value: Any) -> str:
    if not isinstance(value, str) or not _OCI_DIGEST.fullmatch(value):
        raise ReleaseValidationError("OCI manifest digest is invalid")
    return value


def _bounded_int(value: Any, minimum: int, maximum: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ReleaseValidationError(f"{name} is invalid")
    return value


def _version(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ReleaseValidationError(f"{name} is invalid")
    return value


def semantic_version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ReleaseValidationError("version is invalid")
    return tuple(int(part) for part in match.groups())


def _https_origin(value: Any) -> str:
    if not isinstance(value, str):
        raise ReleaseValidationError("registry origin is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        raise ReleaseValidationError("registry origin is invalid")
    return value


def _members(value: Any) -> tuple[ReleaseMember, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        raise ReleaseValidationError("release members are invalid")
    members: list[ReleaseMember] = []
    identities: set[str] = set()
    aggregate = 0
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _MEMBER_FIELDS:
            raise ReleaseValidationError("release member fields are invalid")
        path = item["path"]
        if not isinstance(path, str) or not path or "\\" in path:
            raise ReleaseValidationError("release member path is invalid")
        pure = PurePosixPath(path)
        if pure.is_absolute() or str(pure) != path or any(part in {"", ".", ".."} for part in pure.parts):
            raise ReleaseValidationError("release member path is invalid")
        identity = unicodedata.normalize("NFC", path).casefold()
        if identity in identities or unicodedata.normalize("NFC", path) != path:
            raise ReleaseValidationError("release member paths collide")
        identities.add(identity)
        size = _bounded_int(item["size"], 0, 256 * 1024 * 1024, "member size")
        aggregate += size
        if aggregate > 1 << 30:
            raise ReleaseValidationError("release aggregate size is invalid")
        mode = item["mode"]
        if mode not in {0o400, 0o500} or isinstance(mode, bool):
            raise ReleaseValidationError("release member mode is invalid")
        uid = _bounded_int(item["uid"], 0, 65535, "member owner")
        gid = _bounded_int(item["gid"], 0, 65535, "member group")
        members.append(ReleaseMember(path, _digest(item["sha256"], "member digest"), size, mode, uid, gid))
    if tuple(member.path for member in members) != tuple(sorted(member.path for member in members)):
        raise ReleaseValidationError("release members are not canonical")
    return tuple(members)
