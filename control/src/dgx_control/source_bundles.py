from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class SourceBundleError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class BundleLimits:
    max_archive_bytes: int = 64 * 1024 * 1024
    max_files: int = 4096
    max_file_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BundleFile:
    path: str
    mode: int
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BundleManifest:
    files: tuple[BundleFile, ...]
    total_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredBundle:
    path: Path
    manifest: BundleManifest
    archive_bytes: int


def inspect_source_bundle(
    payload: BinaryIO, limits: BundleLimits | None = None
) -> BundleManifest:
    active = limits or BundleLimits()
    return _inspect_archive(_read_archive(payload, active), active)


class SourceBundleStore:
    def __init__(self, root: Path, *, limits: BundleLimits | None = None) -> None:
        self._root = root.resolve()
        self._limits = limits or BundleLimits()

    def put(self, expected_sha256: str, payload: BinaryIO) -> StoredBundle:
        if (
            len(expected_sha256) != 64
            or expected_sha256.lower() != expected_sha256
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise SourceBundleError("bundle.digest_invalid", "expected digest is invalid")
        archive = _read_archive(payload, self._limits)
        manifest = _inspect_archive(archive, self._limits)
        if manifest.sha256 != expected_sha256:
            raise SourceBundleError(
                "bundle.digest_mismatch", "source bundle digest does not match"
            )
        directory = self._root / expected_sha256[:2]
        destination = directory / f"{expected_sha256}.tar"
        directory.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing = destination.read_bytes()
            if _inspect_archive(existing, self._limits).sha256 != expected_sha256:
                raise SourceBundleError(
                    "bundle.storage_collision", "stored source bundle is inconsistent"
                )
            return StoredBundle(destination, manifest, len(existing))

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{expected_sha256}.", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(archive)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()
        return StoredBundle(destination, manifest, len(archive))


def _read_archive(payload: BinaryIO, limits: BundleLimits) -> bytes:
    archive = payload.read(limits.max_archive_bytes + 1)
    if not isinstance(archive, bytes):
        raise SourceBundleError("bundle.read_failed", "source bundle is not binary")
    if not archive:
        raise SourceBundleError("bundle.empty", "source bundle is empty")
    if len(archive) > limits.max_archive_bytes:
        raise SourceBundleError("bundle.archive_too_large", "source bundle is too large")
    return archive


def _inspect_archive(archive: bytes, limits: BundleLimits) -> BundleManifest:
    files: list[BundleFile] = []
    seen: set[str] = set()
    total = 0
    try:
        bundle = tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")
    except (tarfile.TarError, OSError) as error:
        raise SourceBundleError("bundle.invalid_archive", "source bundle is invalid") from error
    with bundle:
        for member in bundle:
            path = _safe_path(member.name)
            if path in seen:
                raise SourceBundleError(
                    "bundle.duplicate_path", "source bundle contains a duplicate path"
                )
            seen.add(path)
            if member.isdir():
                continue
            if not member.isfile():
                raise SourceBundleError(
                    "bundle.entry_forbidden",
                    "source bundle may contain only directories and regular files",
                )
            if len(files) >= limits.max_files:
                raise SourceBundleError(
                    "bundle.too_many_files", "source bundle contains too many files"
                )
            if member.size < 0 or member.size > limits.max_file_bytes:
                raise SourceBundleError(
                    "bundle.file_too_large", "source bundle file is too large"
                )
            total += member.size
            if total > limits.max_total_bytes:
                raise SourceBundleError(
                    "bundle.expanded_too_large", "expanded source bundle is too large"
                )
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise SourceBundleError(
                    "bundle.read_failed", "source bundle file cannot be read"
                )
            content = extracted.read(limits.max_file_bytes + 1)
            if len(content) != member.size:
                raise SourceBundleError(
                    "bundle.size_mismatch", "source bundle file size is inconsistent"
                )
            files.append(
                BundleFile(
                    path=path,
                    mode=0o755 if member.mode & 0o111 else 0o644,
                    size=member.size,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
    files.sort(key=lambda item: item.path.encode("utf-8"))
    canonical = json.dumps(
        {
            "schema_version": 1,
            "files": [asdict(item) for item in files],
            "total_bytes": total,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return BundleManifest(
        files=tuple(files),
        total_bytes=total,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _safe_path(value: str) -> str:
    if not value or "\x00" in value or value.startswith("/"):
        raise SourceBundleError("bundle.path_forbidden", "source bundle path is forbidden")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SourceBundleError("bundle.path_forbidden", "source bundle path is forbidden")
    normalized = path.as_posix()
    if len(normalized.encode("utf-8")) > 512:
        raise SourceBundleError("bundle.path_too_long", "source bundle path is too long")
    return normalized

