"""Safe, deterministic materialization of verified workload objects."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from dgx_agent_protocol.workload_packages import OciBundleMetadata, PackageReleaseLock

_DIGEST = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_MAX_ENTRIES = 100_000
_MAX_UNPACKED_BYTES = 16 * 1024**4
_RECEIPT = ".dgx-generation.json"


class MaterializationError(RuntimeError):
    """Verified objects cannot be materialized without weakening isolation."""


class MaterializationCancelled(MaterializationError):
    """Materialization was cancelled before publication."""


class StoredObject(Protocol):
    digest: str
    size: int
    kind: str


class ObjectStore(Protocol):
    def object_path(self, value: StoredObject) -> Path: ...


@dataclass(frozen=True)
class MaterializedGeneration:
    release_digest: str
    root_object_digest: str
    object_digests: tuple[str, ...]
    environment_digest: str | None


class Materializer:
    """Build one read-only generation and publish it with one rename."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if not hasattr(store, "object_path"):
            raise TypeError("materialization object store is invalid")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("materialization cancellation callback is invalid")
        self._store = store
        self._cancelled = cancelled or (lambda: False)

    def materialize(
        self,
        lock: object,
        objects: Mapping[str, StoredObject],
        staging: Path,
    ) -> MaterializedGeneration:
        # The lock is a security boundary: accepting a duck-typed object here
        # would let an untrusted caller choose component descriptors while
        # merely supplying a plausible digest.  Reparse the typed value's
        # canonical bytes so the materializer consumes the same validated,
        # immutable representation that the protocol verifier accepted.
        if not isinstance(lock, PackageReleaseLock):
            raise MaterializationError("trusted release lock is invalid")
        try:
            lock = PackageReleaseLock.parse(lock.canonical_bytes)
        except (TypeError, ValueError, RuntimeError) as error:
            raise MaterializationError("trusted release lock is invalid") from error
        release_digest = _raw_digest(getattr(lock, "digest", None), "release digest")
        if release_digest == "0" * 64:
            raise MaterializationError("trusted release lock is invalid")
        components = _components(lock)
        available = _objects(objects)
        staging = _staging_root(Path(staging))
        final = staging / release_digest
        partial = staging / f"{release_digest}.partial-{secrets.token_hex(8)}"
        generation_lock = _generation_lock(staging, release_digest)
        try:
            existing = _existing_generation(final, release_digest)
            if existing is not None:
                return existing
            self._check_cancelled()
            _remove_abandoned(staging, release_digest)
            partial.mkdir(mode=0o700)
            (partial / "components").mkdir(mode=0o700)
            used: set[str] = set()
            environment_digest: str | None = None
            oci_bundles: list[tuple[OciBundleMetadata, Path]] = []
            entries: list[dict[str, object]] = []
            names: set[str] = set()
            for descriptor in components:
                self._check_cancelled()
                name = getattr(descriptor, "name", None)
                if not isinstance(name, str) or _NAME.fullmatch(name) is None:
                    raise MaterializationError("component name is invalid")
                if name in names:
                    raise MaterializationError("component names are duplicated")
                names.add(name)
                digest = _raw_digest(
                    getattr(descriptor, "digest", None), "component digest"
                )
                stored = available.get(digest)
                if stored is None:
                    raise MaterializationError(f"component {name} is missing")
                source = self._verified_path(descriptor, stored, digest)
                method = _method(descriptor)
                destination = partial / "components" / name
                destination.mkdir(mode=0o700)
                if method in {
                    "archive",
                    "snapshot",
                    "native-archive",
                    "pylock-environment",
                }:
                    _extract_archive(
                        source,
                        destination,
                        maximum_bytes=_unpacked_limit(descriptor),
                        cancelled=self._cancelled,
                    )
                    if method == "pylock-environment":
                        if environment_digest is not None:
                            raise MaterializationError(
                                "multiple Python environments are not supported"
                            )
                        environment_digest = digest
                elif method == "oci-content":
                    document = {
                        "digest": f"sha256:{digest}",
                        "media_type": str(getattr(descriptor, "media_type", "")),
                        "schema_version": 1,
                    }
                    _write_file(
                        destination / "oci-reference.json",
                        _canonical(document),
                        executable=False,
                    )
                elif method == "oci-bundle":
                    metadata = _oci_metadata(descriptor)
                    if metadata.component != name:
                        raise MaterializationError(
                            "OCI bundle component identity is inconsistent"
                        )
                    _extract_archive(
                        source,
                        destination,
                        maximum_bytes=_unpacked_limit(descriptor),
                        cancelled=self._cancelled,
                    )
                    oci_bundles.append((metadata, destination))
                elif method in {"configuration", "file", "wheel", "executable"}:
                    filename = {
                        "configuration": "configuration",
                        "file": "payload",
                        "wheel": f"{name}.whl",
                        "executable": name,
                    }[method]
                    _copy_file(
                        source,
                        destination / filename,
                        executable=method == "executable",
                        cancelled=self._cancelled,
                    )
                else:
                    raise MaterializationError(
                        f"component {name} materialization method is unsupported"
                    )
                used.add(digest)
            for metadata, destination in oci_bundles:
                _publish_oci_metadata(destination, metadata)
            _seal_tree(partial)
            _verify_python_environment_tree(lock, partial)
            for metadata, destination in oci_bundles:
                _verify_oci_bundle_archive(destination, metadata)
            entries = _tree_manifest(partial)
            root_digest = hashlib.sha256(_canonical(entries)).hexdigest()
            result = MaterializedGeneration(
                release_digest=release_digest,
                root_object_digest=root_digest,
                object_digests=tuple(sorted(used)),
                environment_digest=environment_digest,
            )
            receipt = {
                "environment_digest": result.environment_digest,
                "files": entries,
                "object_digests": list(result.object_digests),
                "release_digest": result.release_digest,
                "root_object_digest": result.root_object_digest,
                "schema_version": 1,
            }
            # The receipt is written last and is not part of the root digest it records.
            os.chmod(partial, 0o700)
            _write_file(partial / _RECEIPT, _canonical(receipt), executable=False)
            os.chmod(partial / _RECEIPT, 0o444)
            os.chmod(partial, 0o555)
            _fsync_tree(partial)
            self._check_cancelled()
            try:
                os.rename(partial, final)
            except FileExistsError:
                existing = _existing_generation(final, release_digest)
                if existing is None:
                    raise MaterializationError(
                        "published generation is inconsistent"
                    ) from None
                return existing
            _fsync_directory(staging)
            return result
        except MaterializationCancelled:
            raise
        except MaterializationError:
            raise
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
            raise MaterializationError(
                "generation materialization failed safely"
            ) from error
        finally:
            _remove_private_tree(partial)
            os.close(generation_lock)

    def _verified_path(
        self,
        descriptor: object,
        stored: StoredObject,
        digest: str,
    ) -> Path:
        if getattr(stored, "digest", None) != digest:
            raise MaterializationError("component object receipt is inconsistent")
        expected_size = getattr(descriptor, "size", None)
        if type(expected_size) is not int or expected_size < 0:
            raise MaterializationError("component size is invalid")
        if getattr(stored, "size", None) != expected_size:
            raise MaterializationError("component object size is inconsistent")
        try:
            path = Path(self._store.object_path(stored))
            metadata = path.stat(follow_symlinks=False)
        except (OSError, TypeError, ValueError) as error:
            raise MaterializationError("component object is unavailable") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != expected_size
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or _hash_file(path, self._cancelled) != digest
        ):
            raise MaterializationError("component object is unsafe or corrupt")
        return path

    def _check_cancelled(self) -> None:
        if self._cancelled():
            raise MaterializationCancelled("generation materialization was cancelled")


def _components(lock: object) -> tuple[object, ...]:
    raw = getattr(lock, "components", None)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise MaterializationError("release lock components are invalid")
    result = list(raw)
    adapter = getattr(lock, "adapter", None)
    if adapter is not None:
        result.append(adapter)
    if not result or len(result) > 256:
        raise MaterializationError("release lock component count is invalid")
    return tuple(result)


def _objects(values: Mapping[str, StoredObject]) -> dict[str, StoredObject]:
    if not isinstance(values, Mapping) or len(values) > 256:
        raise MaterializationError("materialization object mapping is invalid")
    result: dict[str, StoredObject] = {}
    for key, value in values.items():
        digest = _raw_digest(key, "object mapping digest")
        if digest in result:
            raise MaterializationError("materialization object mapping is duplicated")
        result[digest] = value
    return result


def _method(descriptor: object) -> str:
    value = getattr(descriptor, "materialization", None)
    if not isinstance(value, Mapping) or "method" not in value:
        raise MaterializationError("component materialization method is invalid")
    method = value.get("method")
    if not isinstance(method, str):
        raise MaterializationError("component materialization method is invalid")
    return method


def _oci_metadata(descriptor: object) -> OciBundleMetadata:
    value = getattr(descriptor, "materialization", None)
    if not isinstance(value, Mapping) or value.get("method") != "oci-bundle":
        raise MaterializationError("OCI bundle materialization metadata is missing")
    try:
        return OciBundleMetadata.parse(
            {key: item for key, item in value.items() if key != "method"}
        )
    except (TypeError, ValueError, RuntimeError) as error:
        raise MaterializationError("OCI bundle materialization metadata is invalid") from error


def _verify_python_environment_tree(lock: PackageReleaseLock, generation: Path) -> None:
    compatibility = getattr(lock, "compatibility", {})
    runtime = compatibility.get("python_runtime") if isinstance(compatibility, Mapping) else None
    if runtime is None:
        return
    if not isinstance(runtime, Mapping):
        raise MaterializationError("Python runtime metadata is invalid")
    component = runtime.get("environment_component")
    expected = runtime.get("environment_tree_digest")
    if not isinstance(component, str) or not isinstance(expected, str):
        raise MaterializationError("Python environment tree metadata is invalid")
    expected = expected.removeprefix("sha256:")
    environment = generation / "components" / component
    _safe_child(generation / "components", component, "Python environment component")
    try:
        entries = _tree_manifest(environment)
    except (OSError, MaterializationError) as error:
        raise MaterializationError("Python environment tree is unavailable") from error
    digest = hashlib.sha256(_canonical(entries)).hexdigest()
    if digest != expected:
        raise MaterializationError("Python environment tree digest is not authorized")


def _verify_oci_bundle_archive(
    destination: Path,
    metadata: OciBundleMetadata,
    *,
    publish_manifest: bool = False,
) -> None:
    """Verify the signed OCI-rootfs archive after immutable tree sealing.

    This intentionally treats the OCI config as descriptive metadata only. The
    helper creates the execution config from the signed deployment policy, so
    image-provided command, environment, identity, mounts, and capabilities
    cannot widen the DGX-Forge sandbox.
    """

    expected_names = {"oci-manifest.json", "config.json", metadata.rootfs}
    if not publish_manifest:
        expected_names.add("oci-bundle.json")
    try:
        entries = {item.name for item in destination.iterdir()}
    except OSError as error:
        raise MaterializationError("OCI bundle archive is unavailable") from error
    if entries != expected_names:
        raise MaterializationError("OCI bundle archive layout is invalid")
    oci_manifest_path = destination / "oci-manifest.json"
    config_path = destination / "config.json"
    manifest_path = destination / "oci-bundle.json"
    if not publish_manifest:
        manifest_raw = _read_canonical_json(manifest_path, "OCI bundle manifest")
        try:
            manifest = OciBundleMetadata.parse(manifest_raw)
        except (TypeError, ValueError, RuntimeError) as error:
            raise MaterializationError("OCI bundle manifest is invalid") from error
        if manifest != metadata:
            raise MaterializationError("OCI bundle manifest does not match signed metadata")
    oci_manifest = _read_canonical_json(oci_manifest_path, "OCI image manifest")
    if (
        not isinstance(oci_manifest, dict)
        or oci_manifest.get("schemaVersion") != 2
        or not isinstance(oci_manifest.get("config"), dict)
        or oci_manifest["config"].get("digest") != metadata.config_digest
        or not isinstance(oci_manifest.get("layers"), list)
    ):
        raise MaterializationError("OCI image manifest is not an object")
    if hashlib.sha256(_canonical(oci_manifest)).hexdigest() != metadata.manifest_digest.removeprefix("sha256:"):
        raise MaterializationError("OCI bundle manifest digest is invalid")
    config = _read_canonical_json(config_path, "OCI bundle config")
    if not isinstance(config, dict):
        raise MaterializationError("OCI bundle config is not an object")
    if hashlib.sha256(_canonical(config)).hexdigest() != metadata.config_digest.removeprefix("sha256:"):
        raise MaterializationError("OCI bundle config digest is invalid")
    rootfs = _safe_child(destination, metadata.rootfs, "OCI bundle rootfs")
    rootfs_stat = rootfs.stat(follow_symlinks=False)
    if not stat.S_ISDIR(rootfs_stat.st_mode):
        raise MaterializationError("OCI bundle rootfs is not a directory")
    rootfs_entries = _tree_manifest(rootfs)
    rootfs_digest = hashlib.sha256(_canonical(rootfs_entries)).hexdigest()
    if rootfs_digest != metadata.rootfs_digest.removeprefix("sha256:"):
        raise MaterializationError("OCI bundle rootfs digest is invalid")
    entrypoint = _safe_child(rootfs, metadata.entrypoint, "OCI bundle entrypoint")
    entry_stat = entrypoint.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(entry_stat.st_mode)
        or not entry_stat.st_mode & 0o111
        or entry_stat.st_nlink != 1
    ):
        raise MaterializationError("OCI bundle entrypoint is not executable")
    if publish_manifest:
        _write_file(manifest_path, _canonical(metadata.to_mapping()), executable=False)


def _publish_oci_metadata(destination: Path, metadata: OciBundleMetadata) -> None:
    """Publish lock-signed OCI metadata into an archive-derived component.

    The archive is not allowed to provide this file.  Keeping this write
    separate from verification lets the generation be sealed only after the
    metadata has been bound to the lock, while the subsequent full verifier
    checks the rootfs digest after sealing has normalized modes.
    """
    try:
        entries = {item.name for item in destination.iterdir()}
    except OSError as error:
        raise MaterializationError("OCI bundle archive is unavailable") from error
    if entries != {"oci-manifest.json", "config.json", metadata.rootfs}:
        raise MaterializationError("OCI bundle archive layout is invalid")
    _write_file(destination / "oci-bundle.json", _canonical(metadata.to_mapping()), executable=False)


def _read_canonical_json(path: Path, name: str) -> object:
    try:
        raw = path.read_bytes()
        if not 2 <= len(raw) <= 256 * 1024:
            raise MaterializationError(f"{name} is too large")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=_reject_json_constant,
        )
        if raw != _canonical(value):
            raise MaterializationError(f"{name} is not canonical")
        return value
    except MaterializationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise MaterializationError(f"{name} is invalid") from error


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MaterializationError("OCI bundle JSON contains duplicate fields")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise MaterializationError("OCI bundle JSON contains a nonfinite value")


def _safe_child(root: Path, relative: str, name: str) -> Path:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in relative
    ):
        raise MaterializationError(f"{name} is invalid")
    current = root
    for part in path.parts:
        current = current / part
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as error:
            raise MaterializationError(f"{name} is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise MaterializationError(f"{name} contains a symlink")
    return current


def _unpacked_limit(descriptor: object) -> int:
    value = getattr(descriptor, "unpacked_size", None)
    if value is None:
        return _MAX_UNPACKED_BYTES
    if type(value) is not int or not 0 <= value <= _MAX_UNPACKED_BYTES:
        raise MaterializationError("component unpacked size is invalid")
    return value


def _extract_archive(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int,
    cancelled: Callable[[], bool],
) -> None:
    try:
        with tarfile.open(source, mode="r:*") as archive:
            _extract_tar(archive, destination, maximum_bytes, cancelled)
            return
    except tarfile.ReadError:
        pass
    try:
        with zipfile.ZipFile(source) as archive:
            _extract_zip(archive, destination, maximum_bytes, cancelled)
    except zipfile.BadZipFile as error:
        raise MaterializationError("component archive format is invalid") from error


def _extract_tar(
    archive: tarfile.TarFile,
    destination: Path,
    maximum_bytes: int,
    cancelled: Callable[[], bool],
) -> None:
    names: set[str] = set()
    total = 0
    members = archive.getmembers()
    if len(members) > _MAX_ENTRIES:
        raise MaterializationError("component archive has too many entries")
    for member in members:
        if cancelled():
            raise MaterializationCancelled("generation materialization was cancelled")
        relative = _archive_name(member.name, names)
        if member.mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise MaterializationError("component archive carries privileged mode bits")
        target = destination.joinpath(*relative.parts)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        if not member.isfile():
            raise MaterializationError("component archive contains a special entry")
        total += member.size
        if member.size < 0 or total > maximum_bytes:
            raise MaterializationError("component archive exceeds unpacked size")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise MaterializationError("component archive file is unavailable")
        _stream_file(
            extracted, target, member.size, bool(member.mode & 0o111), cancelled
        )


def _extract_zip(
    archive: zipfile.ZipFile,
    destination: Path,
    maximum_bytes: int,
    cancelled: Callable[[], bool],
) -> None:
    names: set[str] = set()
    total = 0
    entries = archive.infolist()
    if len(entries) > _MAX_ENTRIES:
        raise MaterializationError("component archive has too many entries")
    for entry in entries:
        if cancelled():
            raise MaterializationCancelled("generation materialization was cancelled")
        relative = _archive_name(entry.filename.rstrip("/"), names)
        mode = entry.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise MaterializationError("component archive carries privileged mode bits")
        target = destination.joinpath(*relative.parts)
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        if kind not in {0, stat.S_IFREG}:
            raise MaterializationError("component archive contains a special entry")
        total += entry.file_size
        if total > maximum_bytes:
            raise MaterializationError("component archive exceeds unpacked size")
        with archive.open(entry, "r") as source:
            _stream_file(source, target, entry.file_size, bool(mode & 0o111), cancelled)


def _archive_name(value: str, names: set[str]) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise MaterializationError("component archive path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MaterializationError("component archive path escapes generation")
    normalized = path.as_posix()
    if normalized in names:
        raise MaterializationError("component archive contains duplicate paths")
    names.add(normalized)
    return path


def _stream_file(
    source: object,
    target: Path,
    expected_size: int,
    executable: bool,
    cancelled: Callable[[], bool],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = _exclusive_file(target)
    completed = 0
    try:
        while True:
            if cancelled():
                raise MaterializationCancelled(
                    "generation materialization was cancelled"
                )
            chunk = source.read(1024 * 1024)  # type: ignore[attr-defined]
            if not chunk:
                break
            completed += len(chunk)
            if completed > expected_size:
                raise MaterializationError(
                    "component archive file exceeds declared size"
                )
            os.write(descriptor, chunk)
        if completed != expected_size:
            raise MaterializationError("component archive file is truncated")
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o555 if executable else 0o444)
    finally:
        os.close(descriptor)


def _copy_file(
    source: Path,
    target: Path,
    *,
    executable: bool,
    cancelled: Callable[[], bool],
) -> None:
    with source.open("rb") as stream:
        _stream_file(stream, target, source.stat().st_size, executable, cancelled)


def _write_file(path: Path, content: bytes, *, executable: bool) -> None:
    descriptor = _exclusive_file(path)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o555 if executable else 0o444)
    finally:
        os.close(descriptor)


def _exclusive_file(path: Path) -> int:
    try:
        return os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as error:
        raise MaterializationError(
            "materialized file cannot be created safely"
        ) from error


def _seal_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o555)
        elif stat.S_ISREG(metadata.st_mode):
            mode = stat.S_IMODE(metadata.st_mode)
            path.chmod(0o555 if mode & 0o111 else 0o444)
        else:
            raise MaterializationError(
                "materialized generation contains a special file"
            )


def _tree_manifest(root: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.stat(follow_symlinks=False)
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            result.append({"kind": "directory", "mode": mode, "path": relative})
        elif stat.S_ISREG(metadata.st_mode):
            result.append(
                {
                    "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "kind": "file",
                    "mode": mode,
                    "path": relative,
                    "size": metadata.st_size,
                }
            )
        else:
            raise MaterializationError(
                "materialized generation contains a special file"
            )
    return result


def _existing_generation(
    path: Path,
    release_digest: str,
) -> MaterializedGeneration | None:
    if not path.exists():
        return None
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            raise MaterializationError("published generation is mutable or unsafe")
        raw = (path / _RECEIPT).read_bytes()
        receipt = json.loads(raw)
        if not isinstance(receipt, dict) or set(receipt) != {
            "environment_digest",
            "files",
            "object_digests",
            "release_digest",
            "root_object_digest",
            "schema_version",
        }:
            raise MaterializationError("published generation receipt is invalid")
        if raw != _canonical(receipt) or receipt["release_digest"] != release_digest:
            raise MaterializationError("published generation receipt is inconsistent")
        files = receipt["files"]
        if not isinstance(files, list) or _tree_manifest_without_receipt(path) != files:
            raise MaterializationError("published generation bytes are inconsistent")
        root_digest = hashlib.sha256(_canonical(files)).hexdigest()
        if receipt["root_object_digest"] != root_digest:
            raise MaterializationError("published generation digest is inconsistent")
        object_digests = receipt["object_digests"]
        if not isinstance(object_digests, list) or any(
            not isinstance(item, str) or _DIGEST.fullmatch(item) is None
            for item in object_digests
        ):
            raise MaterializationError("published generation objects are invalid")
        environment = receipt["environment_digest"]
        if environment is not None:
            environment = _raw_digest(environment, "environment digest")
        return MaterializedGeneration(
            release_digest,
            root_digest,
            tuple(object_digests),
            environment,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, MaterializationError):
            raise
        raise MaterializationError("published generation is invalid") from error


def _tree_manifest_without_receipt(root: Path) -> list[dict[str, object]]:
    return [item for item in _tree_manifest(root) if item["path"] != _RECEIPT]


def _staging_root(path: Path) -> Path:
    if not path.is_absolute():
        raise MaterializationError("generation staging root must be absolute")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
            raise MaterializationError("generation staging root is unsafe")
        path.chmod(0o700)
        return path
    except OSError as error:
        raise MaterializationError("generation staging root is unavailable") from error


def _generation_lock(root: Path, release_digest: str) -> int:
    try:
        descriptor = os.open(
            root / f".{release_digest}.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid not in {0, os.geteuid()}
        ):
            raise MaterializationError("generation materialization lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except MaterializationError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        raise MaterializationError(
            "generation materialization lock is unavailable"
        ) from error


def _remove_abandoned(root: Path, release_digest: str) -> None:
    for path in root.glob(f"{release_digest}.partial-*"):
        _remove_private_tree(path)


def _remove_private_tree(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise MaterializationError("generation partial staging is unsafe")
    for child in path.rglob("*"):
        if child.is_symlink():
            raise MaterializationError("generation partial staging is unsafe")
    for directory in [path, *[item for item in path.rglob("*") if item.is_dir()]]:
        directory.chmod(0o700)
    shutil.rmtree(path)


def _hash_file(path: Path, cancelled: Callable[[], bool]) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            if cancelled():
                raise MaterializationCancelled(
                    "generation materialization was cancelled"
                )
            chunk = source.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _raw_digest(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise MaterializationError(f"{label} is invalid")
    match = _DIGEST.fullmatch(value)
    if match is None:
        raise MaterializationError(f"{label} is invalid")
    return match.group(1)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fsync_tree(root: Path) -> None:
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()), reverse=True
    ):
        _fsync_directory(path)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "MaterializationCancelled",
    "MaterializationError",
    "MaterializedGeneration",
    "Materializer",
]
