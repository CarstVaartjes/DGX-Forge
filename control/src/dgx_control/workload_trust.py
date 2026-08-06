"""Workload-only TUF publication and bounded delivery primitives.

This module deliberately has no dependency on the platform update trust code.
Its keys and delegated target vocabulary can authorize workload definition and
release-lock bytes only.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from securesystemslib.signer import Signer
from tuf.api.metadata import (
    DelegatedRole,
    Delegations,
    Metadata,
    MetaFile,
    Root,
    Snapshot,
    TargetFile,
    Targets,
    Timestamp,
)
from tuf.api.serialization.json import CanonicalJSONSerializer

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_FAMILY_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
_RELEASE_TARGET = re.compile(r"releases/(?P<digest>[0-9a-f]{64})\.json\Z")
_METADATA_ROLE = re.compile(
    r"(?:(?P<version>[1-9][0-9]*)\.)?"
    r"(?P<role>root|targets|snapshot|timestamp|families|releases)\Z"
)
_MAX_LOCK_BYTES = 1024 * 1024
_MAX_EVIDENCE_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_EXPIRY = timedelta(days=7)
_FLOOR_NAME = "version-floor.json"
_LOCK_NAME = ".publish.lock"
_ROLES = ("root", "targets", "snapshot", "timestamp", "families", "releases")


class WorkloadTrustError(RuntimeError):
    """Workload publication or delivery crossed a trust or safety boundary."""


class _DuplicateField(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField(key)
        result[key] = value
    return result


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise WorkloadTrustError("workload document is not canonical JSON") from error


def _canonical_lock(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise WorkloadTrustError(
            "workload release lock canonical encoding is invalid"
        ) from error


def _document(raw: bytes, label: str, maximum: int) -> dict[str, object]:
    if not 0 < len(raw) <= maximum:
        raise WorkloadTrustError(f"{label} size is unsafe")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (
        _DuplicateField,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise WorkloadTrustError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise WorkloadTrustError(f"{label} is invalid")
    return value


def _lock_document(raw: bytes) -> tuple[dict[str, object], str]:
    value = _document(raw, "workload release lock canonical", _MAX_LOCK_BYTES)
    if raw != _canonical_lock(value):
        raise WorkloadTrustError("workload release lock canonical encoding is invalid")
    family_id = value.get("family_id")
    if (
        value.get("schema_version") != 1
        or not isinstance(family_id, str)
        or _FAMILY_ID.fullmatch(family_id) is None
    ):
        raise WorkloadTrustError("workload release lock canonical identity is invalid")
    return value, family_id


def _evidence_document(value: Mapping[str, object]) -> dict[str, object]:
    document = dict(value)
    if len(_canonical(document)) > _MAX_EVIDENCE_BYTES:
        raise WorkloadTrustError("workload evidence size is unsafe")
    if (
        set(document)
        != {
            "lock_digest",
            "provenance_digest",
            "sbom_digest",
            "schema_version",
        }
        or document.get("schema_version") != 1
    ):
        raise WorkloadTrustError("workload evidence is invalid")
    if any(
        not isinstance(document.get(name), str)
        or _SHA256.fullmatch(document[name]) is None  # type: ignore[arg-type]
        for name in ("lock_digest", "provenance_digest", "sbom_digest")
    ):
        raise WorkloadTrustError("workload evidence is invalid")
    return document


def _secure_directory(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise WorkloadTrustError(f"{label} must be absolute")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise WorkloadTrustError(f"{label} is unsafe") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise WorkloadTrustError(f"{label} is unsafe")


def _safe_read(root: Path, name: str, maximum: int, label: str) -> bytes:
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise WorkloadTrustError(f"{label} is unsafe")
    directory = -1
    descriptor = -1
    try:
        directory = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        root_stat = os.fstat(directory)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(root_stat.st_mode) & 0o022
        ):
            raise WorkloadTrustError(f"{label} directory is unsafe")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
        before = os.fstat(descriptor)
        if before.st_size > maximum:
            raise WorkloadTrustError(f"{label} size is unsafe")
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size < 1
        ):
            raise WorkloadTrustError(f"{label} is unsafe")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if (
            len(content) > maximum
            or len(content) != before.st_size
            or identity(before) != identity(after)
        ):
            raise WorkloadTrustError(f"{label} changed while being read")
        return bytes(content)
    except FileNotFoundError as error:
        raise WorkloadTrustError(f"{label} is unavailable") from error
    except WorkloadTrustError:
        raise
    except OSError as error:
        raise WorkloadTrustError(f"{label} is unsafe") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory >= 0:
            os.close(directory)


def _atomic_write(root: Path, name: str, content: bytes, *, replace: bool) -> None:
    directory = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.new"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise WorkloadTrustError("workload trust write was incomplete")
            offset += written
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if not replace:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing = _safe_read(
                    root, name, max(len(content), 1), "workload target"
                )
                if existing != content:
                    raise WorkloadTrustError(
                        "workload target digest collision"
                    ) from None
            os.unlink(temporary, dir_fd=directory)
        else:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except WorkloadTrustError:
        raise
    except OSError as error:
        raise WorkloadTrustError("workload trust publication failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _signed_bytes(metadata: Metadata[Any]) -> bytes:
    return metadata.to_bytes(CanonicalJSONSerializer())


def _expiry(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise WorkloadTrustError("workload trust clock must be UTC")
    return now + _EXPIRY


@dataclass(frozen=True)
class WorkloadTrustSigners:
    root: Signer
    targets: Signer
    snapshot: Signer
    timestamp: Signer
    families: Signer
    releases: Signer

    def __post_init__(self) -> None:
        keyids = {getattr(self, role).public_key.keyid for role in _ROLES}
        if len(keyids) != len(_ROLES):
            raise WorkloadTrustError("workload trust roles require distinct keys")


@dataclass(frozen=True)
class WorkloadOnlineSigners:
    releases: Signer
    snapshot: Signer
    timestamp: Signer

    def __post_init__(self) -> None:
        if (
            len(
                {
                    self.releases.public_key.keyid,
                    self.snapshot.public_key.keyid,
                    self.timestamp.public_key.keyid,
                }
            )
            != 3
        ):
            raise WorkloadTrustError("workload online roles require distinct keys")


@dataclass(frozen=True)
class TrustedWorkloadTarget:
    digest: str
    length: int
    git_commit: str
    tuf_snapshot_version: int


def _delegated_metadata(
    role: str,
    signer: Signer,
    now: datetime,
) -> Metadata[Targets]:
    metadata = Metadata(Targets(version=1, expires=_expiry(now), targets={}))
    metadata.sign(signer)
    return metadata


def initialize_workload_trust(
    *,
    metadata_root: Path,
    target_root: Path,
    signers: WorkloadTrustSigners,
    now: datetime,
) -> bytes:
    """Provision a new workload-only TUF repository with disjoint role keys."""

    metadata_root = Path(metadata_root)
    target_root = Path(target_root)
    if (
        metadata_root == target_root
        or metadata_root in target_root.parents
        or target_root in metadata_root.parents
    ):
        raise WorkloadTrustError("workload metadata and target roots must not overlap")
    _secure_directory(metadata_root, "workload metadata root")
    _secure_directory(target_root, "workload target root")
    if any(metadata_root.iterdir()) or any(target_root.iterdir()):
        raise WorkloadTrustError("workload trust repository already exists")

    root = Root(version=1, expires=_expiry(now), consistent_snapshot=True)
    for role in ("root", "targets", "snapshot", "timestamp"):
        root.add_key(getattr(signers, role).public_key, role)
    root_metadata = Metadata(root)
    root_metadata.sign(signers.root)

    delegations = Delegations(
        keys={
            signers.families.public_key.keyid: signers.families.public_key,
            signers.releases.public_key.keyid: signers.releases.public_key,
        },
        roles={
            "families": DelegatedRole(
                "families",
                [signers.families.public_key.keyid],
                1,
                True,
                paths=["families/*"],
            ),
            "releases": DelegatedRole(
                "releases",
                [signers.releases.public_key.keyid],
                1,
                True,
                paths=["releases/*"],
            ),
        },
    )
    targets_metadata = Metadata(
        Targets(version=1, expires=_expiry(now), targets={}, delegations=delegations)
    )
    targets_metadata.sign(signers.targets)
    families_metadata = _delegated_metadata("families", signers.families, now)
    releases_metadata = _delegated_metadata("releases", signers.releases, now)

    role_bytes = {
        "targets": _signed_bytes(targets_metadata),
        "families": _signed_bytes(families_metadata),
        "releases": _signed_bytes(releases_metadata),
    }
    snapshot_metadata = Metadata(
        Snapshot(
            version=1,
            expires=_expiry(now),
            meta={
                f"{role}.json": MetaFile.from_data(1, raw, ["sha256"])
                for role, raw in role_bytes.items()
            },
        )
    )
    snapshot_metadata.sign(signers.snapshot)
    snapshot_bytes = _signed_bytes(snapshot_metadata)
    timestamp_metadata = Metadata(
        Timestamp(
            version=1,
            expires=_expiry(now),
            snapshot_meta=MetaFile.from_data(1, snapshot_bytes, ["sha256"]),
        )
    )
    timestamp_metadata.sign(signers.timestamp)

    initial = {
        "root.json": _signed_bytes(root_metadata),
        "targets.json": role_bytes["targets"],
        "families.json": role_bytes["families"],
        "releases.json": role_bytes["releases"],
        "1.targets.json": role_bytes["targets"],
        "1.families.json": role_bytes["families"],
        "1.releases.json": role_bytes["releases"],
        "snapshot.json": snapshot_bytes,
        "1.snapshot.json": snapshot_bytes,
        "timestamp.json": _signed_bytes(timestamp_metadata),
    }
    for name, raw in initial.items():
        _atomic_write(metadata_root, name, raw, replace=False)
    _write_floor(metadata_root, {role: 1 for role in _ROLES})
    return initial["root.json"]


def _metadata(raw: bytes, role: str) -> Metadata[Any]:
    try:
        metadata = Metadata.from_bytes(raw)
    except Exception as error:
        raise WorkloadTrustError(f"workload {role} metadata is invalid") from error
    if metadata.signed.type != role:
        raise WorkloadTrustError(f"workload {role} metadata type is invalid")
    return metadata


def _verify_descriptor(descriptor: MetaFile, raw: bytes, label: str) -> None:
    try:
        descriptor.verify_length_and_hashes(raw)
    except Exception as error:
        raise WorkloadTrustError(f"workload metadata mix-and-match: {label}") from error


def _verify_signatures(
    root: Metadata[Root],
    targets: Metadata[Targets],
    families: Metadata[Targets],
    releases: Metadata[Targets],
    snapshot: Metadata[Snapshot],
    timestamp: Metadata[Timestamp],
) -> None:
    try:
        root.verify_delegate("root", root)
        root.verify_delegate("targets", targets)
        root.verify_delegate("snapshot", snapshot)
        root.verify_delegate("timestamp", timestamp)
        targets.verify_delegate("families", families)
        targets.verify_delegate("releases", releases)
    except Exception as error:
        raise WorkloadTrustError("workload metadata signature is invalid") from error


def _check_expiry(metadata: Mapping[str, Metadata[Any]], now: datetime) -> None:
    for role, document in metadata.items():
        if document.signed.is_expired(now):
            raise WorkloadTrustError(f"workload {role} metadata expired")


def _read_floor(root: Path) -> dict[str, int]:
    raw = _safe_read(root, _FLOOR_NAME, 4096, "workload version floor")
    value = _document(raw, "workload version floor", 4096)
    if set(value) != set(_ROLES) or any(
        type(value[role]) is not int or value[role] < 1 for role in _ROLES
    ):
        raise WorkloadTrustError("workload version floor is invalid")
    return {role: value[role] for role in _ROLES}  # type: ignore[return-value]


def _write_floor(root: Path, versions: Mapping[str, int]) -> None:
    _atomic_write(root, _FLOOR_NAME, _canonical(dict(versions)), replace=True)


@dataclass(frozen=True)
class _Repository:
    root: Metadata[Root]
    targets: Metadata[Targets]
    families: Metadata[Targets]
    releases: Metadata[Targets]
    snapshot: Metadata[Snapshot]
    timestamp: Metadata[Timestamp]
    raw_targets: bytes
    raw_families: bytes
    raw_releases: bytes
    raw_snapshot: bytes


def _load_repository(metadata_root: Path, now: datetime) -> _Repository:
    raw_root = _safe_read(
        metadata_root, "root.json", _MAX_METADATA_BYTES, "workload root metadata"
    )
    raw_targets = _safe_read(
        metadata_root, "targets.json", _MAX_METADATA_BYTES, "workload targets metadata"
    )
    raw_families = _safe_read(
        metadata_root,
        "families.json",
        _MAX_METADATA_BYTES,
        "workload families metadata",
    )
    raw_timestamp = _safe_read(
        metadata_root,
        "timestamp.json",
        _MAX_METADATA_BYTES,
        "workload timestamp metadata",
    )
    root = _metadata(raw_root, "root")
    targets = _metadata(raw_targets, "targets")
    families = _metadata(raw_families, "targets")
    timestamp = _metadata(raw_timestamp, "timestamp")
    snapshot_version = timestamp.signed.snapshot_meta.version
    raw_snapshot = _safe_read(
        metadata_root,
        f"{snapshot_version}.snapshot.json",
        _MAX_METADATA_BYTES,
        "workload snapshot metadata",
    )
    snapshot = _metadata(raw_snapshot, "snapshot")
    releases_descriptor = snapshot.signed.meta.get("releases.json")
    if releases_descriptor is None:
        raise WorkloadTrustError("workload metadata mix-and-match: releases missing")
    raw_releases = _safe_read(
        metadata_root,
        f"{releases_descriptor.version}.releases.json",
        _MAX_METADATA_BYTES,
        "workload releases metadata",
    )
    releases = _metadata(raw_releases, "targets")
    _verify_signatures(root, targets, families, releases, snapshot, timestamp)
    _check_expiry(
        {
            "root": root,
            "targets": targets,
            "families": families,
            "releases": releases,
            "snapshot": snapshot,
            "timestamp": timestamp,
        },
        now,
    )
    _verify_descriptor(timestamp.signed.snapshot_meta, raw_snapshot, "snapshot")
    for role, raw in {
        "targets": raw_targets,
        "families": raw_families,
        "releases": raw_releases,
    }.items():
        descriptor = snapshot.signed.meta.get(f"{role}.json")
        if descriptor is None:
            raise WorkloadTrustError(f"workload metadata mix-and-match: {role} missing")
        expected_version = _metadata(raw, "targets").signed.version
        if descriptor.version != expected_version:
            raise WorkloadTrustError(f"workload metadata mix-and-match: {role} version")
        _verify_descriptor(descriptor, raw, role)
    versions = {
        "root": root.signed.version,
        "targets": targets.signed.version,
        "snapshot": snapshot.signed.version,
        "timestamp": timestamp.signed.version,
        "families": families.signed.version,
        "releases": releases.signed.version,
    }
    floor = _read_floor(metadata_root)
    if any(versions[role] < floor[role] for role in _ROLES):
        raise WorkloadTrustError("workload metadata rollback was rejected")
    return _Repository(
        root,
        targets,
        families,
        releases,
        snapshot,
        timestamp,
        raw_targets,
        raw_families,
        raw_releases,
        raw_snapshot,
    )


class _PublicationLock:
    def __init__(self, metadata_root: Path) -> None:
        self._root = metadata_root
        self._descriptor = -1

    def __enter__(self) -> None:
        self._descriptor = os.open(
            self._root / _LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        metadata = os.fstat(self._descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(self._descriptor)
            self._descriptor = -1
            raise WorkloadTrustError("workload publication lock is unsafe")
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._descriptor)
            self._descriptor = -1
            raise WorkloadTrustError("another workload publication is active") from None

    def __exit__(self, *_args: object) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1


class WorkloadTrustPublisher:
    """Authorize canonical release locks under the workload releases role."""

    def __init__(
        self,
        *,
        metadata_root: Path,
        target_root: Path,
        signers: WorkloadOnlineSigners,
        commit_eligible: Callable[[str], bool],
        policy_authorized: Callable[[str, Mapping[str, object]], bool],
        evidence_verified: Callable[[str, Mapping[str, object]], bool],
        clock: Callable[[], datetime],
    ) -> None:
        self._metadata_root = Path(metadata_root)
        self._target_root = Path(target_root)
        self._signers = signers
        self._commit_eligible = commit_eligible
        self._policy_authorized = policy_authorized
        self._evidence_verified = evidence_verified
        self._clock = clock

    def publish(
        self,
        lock_bytes: bytes,
        git_commit: str,
        evidence: Mapping[str, object],
    ) -> TrustedWorkloadTarget:
        digest = hashlib.sha256(lock_bytes).hexdigest()
        return self.publish_as(
            f"releases/{digest}.json",
            lock_bytes,
            git_commit,
            evidence,
        )

    def publish_as(
        self,
        target_name: str,
        lock_bytes: bytes,
        git_commit: str,
        evidence: Mapping[str, object],
    ) -> TrustedWorkloadTarget:
        digest = hashlib.sha256(lock_bytes).hexdigest()
        match = (
            _RELEASE_TARGET.fullmatch(target_name)
            if isinstance(target_name, str)
            else None
        )
        if match is None or match.group("digest") != digest:
            raise WorkloadTrustError("target is outside workload delegation")
        _lock, family_id = _lock_document(lock_bytes)
        if not isinstance(git_commit, str) or _GIT_COMMIT.fullmatch(git_commit) is None:
            raise WorkloadTrustError("workload Git commit is not eligible")
        evidence_document = _evidence_document(evidence)

        with _PublicationLock(self._metadata_root):
            if not self._commit_eligible(git_commit):
                raise WorkloadTrustError("workload Git commit is not eligible")
            if not self._policy_authorized(family_id, evidence_document):
                raise WorkloadTrustError("workload family policy denied publication")
            if not self._evidence_verified(digest, evidence_document):
                raise WorkloadTrustError("workload evidence verification failed")
            repository = _load_repository(self._metadata_root, self._clock())
            _atomic_write(self._target_root, digest, lock_bytes, replace=False)
            existing = repository.releases.signed.targets.get(target_name)
            if existing is not None:
                existing.verify_length_and_hashes(lock_bytes)
                return TrustedWorkloadTarget(
                    digest,
                    len(lock_bytes),
                    git_commit,
                    repository.snapshot.signed.version,
                )
            return self._publish_metadata(
                repository,
                target_name,
                lock_bytes,
                git_commit,
            )

    def _publish_metadata(
        self,
        repository: _Repository,
        target_name: str,
        lock_bytes: bytes,
        git_commit: str,
    ) -> TrustedWorkloadTarget:
        now = self._clock()
        releases = copy.deepcopy(repository.releases)
        releases.signatures.clear()
        releases.signed.version += 1
        releases.signed.expires = _expiry(now)
        target = TargetFile.from_data(
            target_name,
            lock_bytes,
            ["sha256"],
        )
        target.unrecognized_fields["custom"] = {"git_commit": git_commit}
        releases.signed.targets[target_name] = target
        releases.sign(self._signers.releases)
        try:
            repository.targets.verify_delegate("releases", releases)
        except Exception as error:
            raise WorkloadTrustError(
                "workload releases signer is unauthorized"
            ) from error
        releases_bytes = _signed_bytes(releases)

        snapshot = copy.deepcopy(repository.snapshot)
        snapshot.signatures.clear()
        snapshot.signed.version += 1
        snapshot.signed.expires = _expiry(now)
        snapshot.signed.meta["releases.json"] = MetaFile.from_data(
            releases.signed.version,
            releases_bytes,
            ["sha256"],
        )
        snapshot.sign(self._signers.snapshot)
        try:
            repository.root.verify_delegate("snapshot", snapshot)
        except Exception as error:
            raise WorkloadTrustError(
                "workload snapshot signer is unauthorized"
            ) from error
        snapshot_bytes = _signed_bytes(snapshot)

        timestamp = copy.deepcopy(repository.timestamp)
        timestamp.signatures.clear()
        timestamp.signed.version += 1
        timestamp.signed.expires = _expiry(now)
        timestamp.signed.snapshot_meta = MetaFile.from_data(
            snapshot.signed.version,
            snapshot_bytes,
            ["sha256"],
        )
        timestamp.sign(self._signers.timestamp)
        try:
            repository.root.verify_delegate("timestamp", timestamp)
        except Exception as error:
            raise WorkloadTrustError(
                "workload timestamp signer is unauthorized"
            ) from error
        timestamp_bytes = _signed_bytes(timestamp)

        _atomic_write(
            self._metadata_root,
            f"{releases.signed.version}.releases.json",
            releases_bytes,
            replace=False,
        )
        _atomic_write(
            self._metadata_root,
            f"{snapshot.signed.version}.snapshot.json",
            snapshot_bytes,
            replace=False,
        )
        _atomic_write(
            self._metadata_root, "timestamp.json", timestamp_bytes, replace=True
        )
        _atomic_write(
            self._metadata_root, "releases.json", releases_bytes, replace=True
        )
        _atomic_write(
            self._metadata_root, "snapshot.json", snapshot_bytes, replace=True
        )
        versions = _read_floor(self._metadata_root)
        versions.update(
            {
                "releases": releases.signed.version,
                "snapshot": snapshot.signed.version,
                "timestamp": timestamp.signed.version,
            }
        )
        _write_floor(self._metadata_root, versions)
        return TrustedWorkloadTarget(
            hashlib.sha256(lock_bytes).hexdigest(),
            len(lock_bytes),
            git_commit,
            snapshot.signed.version,
        )


def rotate_workload_root(
    *,
    metadata_root: Path,
    current_signer: Signer,
    replacement_signer: Signer,
    now: datetime,
) -> bytes:
    """Rotate only the workload root with old-and-new TUF authorization."""

    metadata_root = Path(metadata_root)
    with _PublicationLock(metadata_root):
        return _rotate_workload_root_locked(
            metadata_root,
            current_signer,
            replacement_signer,
            now,
        )


def _rotate_workload_root_locked(
    metadata_root: Path,
    current_signer: Signer,
    replacement_signer: Signer,
    now: datetime,
) -> bytes:
    current_raw = _safe_read(
        metadata_root, "root.json", _MAX_METADATA_BYTES, "workload root metadata"
    )
    current = _metadata(current_raw, "root")
    try:
        current.verify_delegate("root", current)
    except Exception as error:
        raise WorkloadTrustError(
            "workload root metadata signature is invalid"
        ) from error
    if current_signer.public_key.keyid not in current.signed.roles["root"].keyids:
        raise WorkloadTrustError("current workload root signer is unauthorized")
    if replacement_signer.public_key.keyid == current_signer.public_key.keyid:
        raise WorkloadTrustError("replacement workload root signer must be new")
    floor = _read_floor(metadata_root)
    if current.signed.version < floor["root"]:
        raise WorkloadTrustError("workload root metadata rollback was rejected")
    rotated = copy.deepcopy(current)
    rotated.signatures.clear()
    rotated.signed.version += 1
    rotated.signed.expires = _expiry(now)
    rotated.signed.add_key(replacement_signer.public_key, "root")
    rotated.signed.revoke_key(current_signer.public_key.keyid, "root")
    rotated.sign(current_signer)
    rotated.sign(replacement_signer, append=True)
    raw = _signed_bytes(rotated)
    current.verify_delegate("root", rotated)
    rotated.verify_delegate("root", rotated)
    _atomic_write(
        metadata_root,
        f"{rotated.signed.version}.root.json",
        raw,
        replace=False,
    )
    _atomic_write(metadata_root, "root.json", raw, replace=True)
    if rotated.signed.version <= floor["root"]:
        raise WorkloadTrustError("workload root rotation version is stale")
    floor["root"] = rotated.signed.version
    _write_floor(metadata_root, floor)
    return raw


class WorkloadTrustDelivery:
    """Read bounded workload metadata and digest-addressed locks safely."""

    def __init__(
        self,
        *,
        metadata_root: Path,
        target_root: Path,
        max_metadata_bytes: int = _MAX_METADATA_BYTES,
        max_target_bytes: int = _MAX_LOCK_BYTES,
    ) -> None:
        if (
            type(max_metadata_bytes) is not int
            or not 1 <= max_metadata_bytes <= _MAX_METADATA_BYTES
        ):
            raise WorkloadTrustError("workload metadata size limit is invalid")
        if (
            type(max_target_bytes) is not int
            or not 1 <= max_target_bytes <= _MAX_LOCK_BYTES
        ):
            raise WorkloadTrustError("workload target size limit is invalid")
        self._metadata_root = Path(metadata_root)
        self._target_root = Path(target_root)
        self._max_metadata_bytes = max_metadata_bytes
        self._max_target_bytes = max_target_bytes

    def metadata(self, role: str) -> bytes:
        match = _METADATA_ROLE.fullmatch(role) if isinstance(role, str) else None
        if match is None:
            raise WorkloadTrustError("workload metadata role is invalid")
        version = match.group("version")
        name = match.group("role")
        if version is not None and name not in {
            "root",
            "targets",
            "snapshot",
            "families",
            "releases",
        }:
            raise WorkloadTrustError("workload metadata role is invalid")
        if version is None and name == "snapshot":
            timestamp = _metadata(
                _safe_read(
                    self._metadata_root,
                    "timestamp.json",
                    self._max_metadata_bytes,
                    "workload timestamp metadata",
                ),
                "timestamp",
            )
            version = str(timestamp.signed.snapshot_meta.version)
        if version is None and name == "releases":
            snapshot = _metadata(self.metadata("snapshot"), "snapshot")
            version = str(snapshot.signed.meta["releases.json"].version)
        filename = f"{version}.{name}.json" if version is not None else f"{name}.json"
        return _safe_read(
            self._metadata_root,
            filename,
            self._max_metadata_bytes,
            f"workload {name} metadata",
        )

    def target(self, digest: str) -> bytes:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise WorkloadTrustError("workload target digest is invalid")
        try:
            raw = _safe_read(
                self._target_root,
                digest,
                self._max_target_bytes,
                "workload target",
            )
        except WorkloadTrustError as error:
            if "unavailable" in str(error):
                raise WorkloadTrustError(
                    "workload target digest is unavailable"
                ) from error
            raise
        if hashlib.sha256(raw).hexdigest() != digest:
            raise WorkloadTrustError("workload target digest mismatch")
        return raw
