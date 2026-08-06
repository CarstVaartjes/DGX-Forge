"""Reproducible, networkless Python environment derivation."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Protocol

_DIGEST = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_PLATFORM = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_MAX_INPUTS = 512
_MAX_WHEEL_ENTRIES = 100_000
_MAX_ENVIRONMENT_BYTES = 16 * 1024**3


@dataclass(frozen=True)
class PythonRuntimeIdentity:
    """Digest-bound interpreter and platform identity used for derivation."""

    interpreter_digest: str
    platform: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.interpreter_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.interpreter_digest) is None
            or not isinstance(self.platform, str)
            or _PLATFORM.fullmatch(self.platform) is None
        ):
            raise ValueError("Python runtime identity is invalid")

    @classmethod
    def local(cls) -> PythonRuntimeIdentity:
        executable = Path(sys.executable)
        try:
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        except OSError as error:
            raise PythonEnvironmentError("Python interpreter is unavailable") from error
        platform = f"{sys.implementation.name}-{sys.version_info.major}{sys.version_info.minor}-{sys.platform}"
        if _PLATFORM.fullmatch(platform) is None:
            raise PythonEnvironmentError("Python runtime platform identity is invalid")
        return cls(digest, platform)


class PythonEnvironmentError(RuntimeError):
    """A Python environment cannot be reproduced from its complete lock."""


class PythonEnvironmentCancelled(PythonEnvironmentError):
    """Python environment construction was cancelled before publication."""


class StoredObject(Protocol):
    digest: str
    size: int
    kind: str
    relative_name: str


class EnvironmentStore(Protocol):
    root: Path

    def object_path(self, value: StoredObject) -> Path: ...


class WheelBuildSandbox(Protocol):
    def build_wheel(
        self,
        source: Path,
        *,
        network: bool,
        devices: tuple[str, ...],
        host_mounts: tuple[str, ...],
        build_identity: str,
        cancelled: Callable[[], bool],
        deadline: object | None,
    ) -> bytes: ...

    def validate_imports(
        self,
        environment: Path,
        imports: tuple[str, ...],
        *,
        network: bool,
        devices: tuple[str, ...],
        host_mounts: tuple[str, ...],
        build_identity: str,
        cancelled: Callable[[], bool],
        deadline: object | None,
    ) -> None: ...


@dataclass(frozen=True)
class SourceBuild:
    source_digest: str
    wheel_digest: str


@dataclass(frozen=True)
class PythonEnvironmentSpec:
    schema_version: int
    interpreter_digest: str
    platform: str
    lock_digest: str
    wheel_digests: tuple[str, ...]
    source_builds: tuple[SourceBuild, ...]
    build_identity: str
    imports: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> PythonEnvironmentSpec:
        if not isinstance(value, Mapping):
            raise PythonEnvironmentError("Python environment spec must be an object")
        expected = {
            "build_recipe",
            "imports",
            "interpreter_digest",
            "lock_digest",
            "platform",
            "schema_version",
            "source_builds",
            "wheel_digests",
        }
        unknown = set(value) - expected
        if unknown:
            if "index_url" in unknown:
                raise PythonEnvironmentError(
                    "live Python index resolution is forbidden"
                )
            raise PythonEnvironmentError("Python environment spec has unknown fields")
        if set(value) != expected or value.get("schema_version") != 1:
            raise PythonEnvironmentError("Python environment spec fields are invalid")
        platform = value.get("platform")
        if not isinstance(platform, str) or _PLATFORM.fullmatch(platform) is None:
            raise PythonEnvironmentError("Python environment platform is invalid")
        wheels = _digest_list(value.get("wheel_digests"), "wheel digests")
        raw_builds = value.get("source_builds")
        if (
            not isinstance(raw_builds, Sequence)
            or isinstance(raw_builds, (str, bytes))
            or len(raw_builds) > _MAX_INPUTS
        ):
            raise PythonEnvironmentError("Python source builds are invalid")
        builds: list[SourceBuild] = []
        for raw in raw_builds:
            if not isinstance(raw, Mapping) or set(raw) != {
                "source_digest",
                "wheel_digest",
            }:
                raise PythonEnvironmentError("Python source build fields are invalid")
            builds.append(
                SourceBuild(
                    _raw_digest(raw["source_digest"], "source digest"),
                    _raw_digest(raw["wheel_digest"], "built wheel digest"),
                )
            )
        if len({item.source_digest for item in builds}) != len(builds) or len(
            {item.wheel_digest for item in builds}
        ) != len(builds):
            raise PythonEnvironmentError("Python source builds are duplicated")
        recipe = value.get("build_recipe")
        if not isinstance(recipe, Mapping) or set(recipe) != {
            "build_identity",
            "network",
            "schema_version",
        }:
            raise PythonEnvironmentError("Python build recipe is invalid")
        build_identity = recipe.get("build_identity")
        if (
            recipe.get("schema_version") != 1
            or recipe.get("network") is not False
            or not isinstance(build_identity, str)
            or _PLATFORM.fullmatch(build_identity) is None
            or build_identity != "dgx-workload-build"
        ):
            raise PythonEnvironmentError(
                "Python build recipe must use a networkless build identity"
            )
        raw_imports = value.get("imports")
        if (
            not isinstance(raw_imports, Sequence)
            or isinstance(raw_imports, (str, bytes))
            or len(raw_imports) > 256
            or any(
                not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None
                for item in raw_imports
            )
            or len(set(raw_imports)) != len(raw_imports)
        ):
            raise PythonEnvironmentError("Python import validation list is invalid")
        return cls(
            schema_version=1,
            interpreter_digest=_raw_digest(
                value["interpreter_digest"], "interpreter digest"
            ),
            platform=platform,
            lock_digest=_raw_digest(value["lock_digest"], "Python lock digest"),
            wheel_digests=wheels,
            source_builds=tuple(builds),
            build_identity=build_identity,
            imports=tuple(raw_imports),
        )

    def identity_document(self) -> dict[str, object]:
        return {
            "build_recipe": {
                "build_identity": self.build_identity,
                "network": False,
                "schema_version": 1,
            },
            "imports": list(self.imports),
            "interpreter_digest": f"sha256:{self.interpreter_digest}",
            "lock_digest": f"sha256:{self.lock_digest}",
            "platform": self.platform,
            "schema_version": self.schema_version,
            "source_builds": [
                {
                    "source_digest": f"sha256:{item.source_digest}",
                    "wheel_digest": f"sha256:{item.wheel_digest}",
                }
                for item in self.source_builds
            ],
            "wheel_digests": [f"sha256:{item}" for item in self.wheel_digests],
        }

    def derivation_digest(self, lock_bytes: bytes) -> str:
        if not isinstance(lock_bytes, bytes):
            raise TypeError("Python lock bytes are invalid")
        lock_digest = hashlib.sha256(lock_bytes).hexdigest()
        if lock_digest != self.lock_digest:
            raise PythonEnvironmentError("Python lock bytes do not match the spec")
        return hashlib.sha256(
            _canonical(
                {
                    "inputs": self.identity_document(),
                    "lock_bytes_sha256": lock_digest,
                }
            )
        ).hexdigest()


class PythonEnvironmentBuilder:
    """Build a sealed venv artifact exclusively from pre-fetched objects."""

    def __init__(
        self,
        store: EnvironmentStore,
        *,
        sandbox: WheelBuildSandbox | None = None,
        cancelled: Callable[[object], bool] | None = None,
        deadline: object | None = None,
    ) -> None:
        if not hasattr(store, "object_path") or not hasattr(store, "root"):
            raise TypeError("Python environment store is invalid")
        if sandbox is not None and (
            not hasattr(sandbox, "build_wheel")
            or not hasattr(sandbox, "validate_imports")
        ):
            raise TypeError("Python wheel build sandbox is invalid")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("Python environment cancellation callback is invalid")
        self._store = store
        self._sandbox = sandbox
        self._cancelled = cancelled or (lambda _binding: False)
        self._deadline = deadline

    def build(
        self,
        spec: PythonEnvironmentSpec,
        objects: Mapping[str, StoredObject],
        binding: object,
    ) -> StoredObject:
        if not isinstance(spec, PythonEnvironmentSpec):
            raise TypeError("Python environment spec is invalid")
        _begin_operation(self._store, binding)
        available = _objects(objects)
        self._check_cancelled(binding)
        lock_object = _require_object(
            self._store,
            available,
            spec.lock_digest,
            "Python lock",
            self._cancelled,
            binding,
        )
        lock_bytes = self._store.object_path(lock_object).read_bytes()
        locked_wheels = _locked_wheels(lock_bytes)
        expected_wheels = set(spec.wheel_digests) | {
            item.wheel_digest for item in spec.source_builds
        }
        if set(locked_wheels) != expected_wheels:
            raise PythonEnvironmentError(
                "complete Python lock wheel hashes do not match environment inputs"
            )
        derivation_digest = spec.derivation_digest(lock_bytes)
        staging_root = Path(self._store.root) / "staging"
        _safe_staging_root(staging_root)
        environment_lock = _environment_lock(staging_root, derivation_digest)
        temporary = staging_root / (
            f"python-environment-{derivation_digest}-uncreated.partial"
        )
        try:
            cached = _lookup_derived(self._store, derivation_digest, binding)
            if cached is not None:
                if _is_immutable(self._store, cached):
                    return cached
                _quarantine(self._store, cached, binding)
            _remove_abandoned(staging_root, derivation_digest)
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f"python-environment-{derivation_digest}-",
                    suffix=".partial",
                    dir=staging_root,
                )
            )
            wheels: list[tuple[str, bytes]] = []
            for digest in spec.wheel_digests:
                self._check_cancelled(binding)
                stored = _require_object(
                    self._store,
                    available,
                    digest,
                    "Python wheel",
                    self._cancelled,
                    binding,
                )
                wheels.append((digest, self._store.object_path(stored).read_bytes()))
            for build in spec.source_builds:
                self._check_cancelled(binding)
                source = _require_object(
                    self._store,
                    available,
                    build.source_digest,
                    "Python source",
                    self._cancelled,
                    binding,
                )
                if self._sandbox is None:
                    raise PythonEnvironmentError(
                        "Python source build requires the networkless sandbox"
                    )
                wheel = self._sandbox.build_wheel(
                    self._store.object_path(source),
                    network=False,
                    devices=(),
                    host_mounts=(),
                    build_identity=spec.build_identity,
                    cancelled=lambda: self._cancelled(binding),
                    deadline=self._deadline,
                )
                if (
                    not isinstance(wheel, bytes)
                    or hashlib.sha256(wheel).hexdigest() != build.wheel_digest
                ):
                    raise PythonEnvironmentError(
                        "networkless Python source build digest is invalid"
                    )
                wheels.append((build.wheel_digest, wheel))
            entries = _environment_entries(spec, lock_bytes, wheels, locked_wheels)
            _validate_imports(entries, spec.imports)
            content = _environment_archive(entries)
            if spec.imports:
                if self._sandbox is None:
                    raise PythonEnvironmentError(
                        "Python import validation requires the networkless sandbox"
                    )
                environment_path = temporary / "environment.tar"
                environment_path.write_bytes(content)
                environment_path.chmod(0o400)
                try:
                    self._sandbox.validate_imports(
                        environment_path,
                        spec.imports,
                        network=False,
                        devices=(),
                        host_mounts=(),
                        build_identity=spec.build_identity,
                        cancelled=lambda: self._cancelled(binding),
                        deadline=self._deadline,
                    )
                except PythonEnvironmentCancelled:
                    raise
                except (OSError, RuntimeError, TypeError, ValueError) as error:
                    raise PythonEnvironmentError(
                        "Python import validation failed in the networkless sandbox"
                    ) from error
            self._check_cancelled(binding)
            existing = _lookup(self._store, hashlib.sha256(content).hexdigest())
            if existing is not None:
                if not _is_immutable(self._store, existing):
                    _quarantine(self._store, existing, binding)
                else:
                    _record_derived(self._store, binding, derivation_digest, existing)
                    return existing
            published = _publish(
                self._store,
                binding,
                derivation_digest=derivation_digest,
                content=content,
            )
            if not _is_immutable(self._store, published):
                raise PythonEnvironmentError(
                    "published Python environment is not immutable"
                )
            return published
        finally:
            _remove_tree(temporary)
            os.close(environment_lock)

    def _check_cancelled(self, binding: object) -> None:
        if self._cancelled(binding):
            raise PythonEnvironmentCancelled("Python environment build was cancelled")


def _environment_entries(
    spec: PythonEnvironmentSpec,
    lock_bytes: bytes,
    wheels: list[tuple[str, bytes]],
    locked_wheels: Mapping[str, tuple[str, str]],
) -> dict[str, tuple[bytes, int]]:
    entries: dict[str, tuple[bytes, int]] = {
        "pyvenv.cfg": (
            (
                "dgx-forge-immutable = true\n"
                f"interpreter-digest = sha256:{spec.interpreter_digest}\n"
                f"platform = {spec.platform}\n"
            ).encode(),
            0o444,
        ),
        "share/dgx-forge/pylock.toml": (lock_bytes, 0o444),
        "share/dgx-forge/derivation.json": (
            _canonical(spec.identity_document()),
            0o444,
        ),
    }
    total = sum(len(item[0]) for item in entries.values())
    for digest, wheel in wheels:
        if hashlib.sha256(wheel).hexdigest() != digest:
            raise PythonEnvironmentError("Python wheel digest is invalid")
        try:
            with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_WHEEL_ENTRIES:
                    raise PythonEnvironmentError("Python wheel has too many entries")
                if _wheel_metadata(archive) != locked_wheels[digest]:
                    raise PythonEnvironmentError(
                        "Python wheel metadata does not match the complete lock"
                    )
                for info in infos:
                    path = _wheel_path(info)
                    if info.is_dir():
                        continue
                    # Check the declared expanded size before asking zipfile to
                    # allocate/decompress the member.  This keeps a malicious
                    # wheel bomb bounded even when the archive is highly
                    # compressed.
                    if (
                        not isinstance(info.file_size, int)
                        or info.file_size < 0
                        or total > _MAX_ENVIRONMENT_BYTES - info.file_size
                    ):
                        raise PythonEnvironmentError(
                            "Python environment exceeds materialization limit"
                        )
                    destination = f"lib/python/site-packages/{path.as_posix()}"
                    if destination in entries:
                        raise PythonEnvironmentError(
                            "Python wheels contain duplicate environment paths"
                        )
                    content = archive.read(info)
                    if len(content) != info.file_size:
                        raise PythonEnvironmentError("Python wheel entry is truncated")
                    total += len(content)
                    if total > _MAX_ENVIRONMENT_BYTES:
                        raise PythonEnvironmentError(
                            "Python environment exceeds materialization limit"
                        )
                    entries[destination] = (content, 0o444)
        except zipfile.BadZipFile as error:
            raise PythonEnvironmentError("Python wheel archive is invalid") from error
    return entries


def _wheel_path(info: zipfile.ZipInfo) -> PurePosixPath:
    value = info.filename.rstrip("/")
    if not value or "\\" in value or "\x00" in value:
        raise PythonEnvironmentError("Python wheel path is invalid")
    path = PurePosixPath(value)
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
    ):
        raise PythonEnvironmentError("Python wheel entry is unsafe")
    return path


def _validate_imports(
    entries: Mapping[str, tuple[bytes, int]],
    imports: tuple[str, ...],
) -> None:
    paths = set(entries)
    for name in imports:
        module = name.split(".", 1)[0]
        prefix = "lib/python/site-packages/"
        if (
            f"{prefix}{module}.py" not in paths
            and f"{prefix}{module}/__init__.py" not in paths
        ):
            raise PythonEnvironmentError(f"Python import validation failed for {name}")


def _environment_archive(entries: Mapping[str, tuple[bytes, int]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        directories: set[str] = set()
        for name in entries:
            path = PurePosixPath(name)
            for parent in path.parents:
                if parent.as_posix() != ".":
                    directories.add(parent.as_posix())
        for name in sorted(directories):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o555
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info)
        for name, (content, mode) in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _locked_wheels(content: bytes) -> dict[str, tuple[str, str]]:
    try:
        document = tomllib.loads(content.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PythonEnvironmentError("Python lock is invalid") from error
    if not isinstance(document, dict) or document.get("lock-version") != "1.0":
        raise PythonEnvironmentError("Python lock version is invalid")
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages or len(packages) > _MAX_INPUTS:
        raise PythonEnvironmentError("Python lock packages are invalid")
    result: dict[str, tuple[str, str]] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise PythonEnvironmentError("Python lock package is invalid")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str) or not version:
            raise PythonEnvironmentError("Python lock package metadata is invalid")
        normalized_name = _normalized_distribution(name)
        wheels = package.get("wheels")
        if not isinstance(wheels, list) or not wheels:
            raise PythonEnvironmentError(
                "Python lock requires wheels and forbids live index resolution"
            )
        for wheel in wheels:
            hashes = wheel.get("hashes") if isinstance(wheel, dict) else None
            digest = hashes.get("sha256") if isinstance(hashes, dict) else None
            if not isinstance(digest, str):
                raise PythonEnvironmentError(
                    "Python lock wheel hash is missing or invalid"
                )
            digest = _raw_digest(digest, "Python lock wheel hash")
            if digest in result:
                raise PythonEnvironmentError("Python lock wheel hashes are duplicated")
            result[digest] = (normalized_name, version)
    return result


def _wheel_metadata(archive: zipfile.ZipFile) -> tuple[str, str]:
    candidates = [
        name
        for name in archive.namelist()
        if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
    ]
    if len(candidates) != 1:
        raise PythonEnvironmentError("Python wheel metadata is missing or ambiguous")
    message = BytesParser().parsebytes(archive.read(candidates[0]))
    name = message.get("Name")
    version = message.get("Version")
    if not isinstance(name, str) or not isinstance(version, str) or not version:
        raise PythonEnvironmentError("Python wheel metadata is invalid")
    return _normalized_distribution(name), version


def _normalized_distribution(value: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", value).lower()
    if (
        not normalized
        or len(normalized) > 128
        or _PLATFORM.fullmatch(normalized) is None
    ):
        raise PythonEnvironmentError("Python distribution name is invalid")
    return normalized


def _require_object(
    store: EnvironmentStore,
    objects: Mapping[str, StoredObject],
    digest: str,
    label: str,
    cancelled: Callable[[object], bool],
    binding: object,
) -> StoredObject:
    value = objects.get(digest)
    if value is None:
        raise PythonEnvironmentError(f"{label} object is missing")
    if getattr(value, "digest", None) != digest:
        raise PythonEnvironmentError(f"{label} object receipt is invalid")
    path = Path(store.object_path(value))
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or metadata.st_size != getattr(value, "size", None)
            or _hash(path, cancelled, binding) != digest
        ):
            raise PythonEnvironmentError(f"{label} object is unsafe or corrupt")
    except OSError as error:
        raise PythonEnvironmentError(f"{label} object is unavailable") from error
    return value


def _objects(values: Mapping[str, StoredObject]) -> dict[str, StoredObject]:
    if not isinstance(values, Mapping) or len(values) > _MAX_INPUTS:
        raise PythonEnvironmentError("Python environment objects are invalid")
    result: dict[str, StoredObject] = {}
    for key, value in values.items():
        digest = _raw_digest(key, "Python object digest")
        if digest in result:
            raise PythonEnvironmentError("Python environment objects are duplicated")
        result[digest] = value
    return result


def _lookup_derived(
    store: object,
    derivation_digest: str,
    binding: object,
) -> StoredObject | None:
    method = getattr(store, "lookup_derived", None)
    if method is not None:
        try:
            return method(derivation_digest)
        except (OSError, RuntimeError) as error:
            state = getattr(store, "state", None)
            state_lookup = getattr(state, "lookup_derived", None)
            quarantine = getattr(store, "quarantine_corrupt", None)
            digest = None if state_lookup is None else state_lookup(derivation_digest)
            if digest is None or quarantine is None:
                raise PythonEnvironmentError(
                    "cached Python environment is corrupt"
                ) from error
            quarantine(binding, digest)
            return None
    state = getattr(store, "state", None)
    state_lookup = getattr(state, "lookup_derived", None)
    if state_lookup is None:
        return None
    digest = state_lookup(derivation_digest)
    return None if digest is None else _lookup(store, digest)


def _begin_operation(store: object, binding: object) -> None:
    state = getattr(store, "state", None)
    begin = getattr(state, "begin_operation", None)
    if begin is not None:
        try:
            begin(binding, phase="materialize")
        except (TypeError, ValueError, RuntimeError) as error:
            raise PythonEnvironmentError(
                "Python environment operation fence is invalid"
            ) from error


def _lookup(store: object, digest: str) -> StoredObject | None:
    method = getattr(store, "lookup", None)
    if method is None:
        return None
    try:
        return method(digest)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise PythonEnvironmentError("cached Python environment is corrupt") from error


def _record_derived(
    store: object,
    binding: object,
    derivation_digest: str,
    value: StoredObject,
) -> None:
    method = getattr(store, "record_derived", None)
    if method is not None:
        method(binding, derivation_digest, value.digest)
        return
    state = getattr(store, "state", None)
    state_record = getattr(state, "record_derived", None)
    if state_record is not None:
        state_record(binding, derivation_digest, value.digest)


def _publish(
    store: object,
    binding: object,
    *,
    derivation_digest: str,
    content: bytes,
) -> StoredObject:
    method = getattr(store, "publish_derived", None)
    if method is not None:
        return method(
            binding,
            derivation_digest=derivation_digest,
            content=content,
            kind="python-environment",
        )
    try:
        from .store import ComponentDescriptor

        digest = hashlib.sha256(content).hexdigest()
        reservation = store.reserve(binding, bytes_required=len(content))
        try:
            record = store.begin_component(
                reservation,
                ComponentDescriptor(digest, len(content), "python-environment"),
            )
            if record.state == "partial":
                record = store.write_partial(record, content)
            result = store.promote_component(record, digest)
        finally:
            # A failed build must not strand a durable capacity reservation;
            # otherwise every later retry can be rejected as over capacity.
            store.release_reservation(reservation)
        _record_derived(store, binding, derivation_digest, result)
        return result
    except PythonEnvironmentError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise PythonEnvironmentError(
            "Python environment publication failed safely"
        ) from error


def _is_immutable(store: object, value: StoredObject) -> bool:
    method = getattr(store, "is_immutable", None)
    if method is not None:
        return bool(method(value))
    try:
        path = Path(store.object_path(value))
        metadata = path.stat(follow_symlinks=False)
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o444
            and metadata.st_size == value.size
            and _hash(path, lambda _binding: False, None) == value.digest
        )
    except (OSError, TypeError, ValueError):
        return False


def _quarantine(store: object, value: StoredObject, binding: object) -> None:
    method = getattr(store, "quarantine", None)
    if method is None:
        raise PythonEnvironmentError(
            "mutable cached environment requires package repair"
        )
    method(value, binding)


def _safe_staging_root(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
            raise PythonEnvironmentError("Python environment staging is unsafe")
        path.chmod(0o700)
    except OSError as error:
        raise PythonEnvironmentError(
            "Python environment staging is unavailable"
        ) from error


def _remove_abandoned(root: Path, derivation_digest: str) -> None:
    # Sweep only directories whose derivation lock can be acquired.  A live
    # peer (including a different derivation) therefore keeps its staging.
    for value in root.glob("python-environment-*.partial"):
        parts = value.name.removesuffix(".partial").split("-")
        candidate = parts[2] if len(parts) >= 4 and len(parts[2]) == 64 else None
        if candidate is None:
            _remove_tree(value)
            continue
        if candidate == derivation_digest:
            _remove_tree(value)
            continue
        lock_path = root / f".{candidate}.lock"
        descriptor: int | None = None
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            if descriptor is not None:
                os.close(descriptor)
            continue
        try:
            _remove_tree(value)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _environment_lock(root: Path, derivation_digest: str) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            root / f".{derivation_digest}.lock",
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
            raise PythonEnvironmentError("Python environment lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except PythonEnvironmentError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise PythonEnvironmentError("Python environment lock is unavailable") from error


def _remove_tree(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise PythonEnvironmentError("Python environment staging is unsafe")
    for child in path.rglob("*"):
        if child.is_symlink():
            raise PythonEnvironmentError("Python environment staging is unsafe")
    shutil.rmtree(path)


def _hash(
    path: Path,
    cancelled: Callable[[object], bool],
    binding: object,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            if cancelled(binding):
                raise PythonEnvironmentCancelled(
                    "Python environment build was cancelled"
                )
            chunk = source.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _digest_list(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) > _MAX_INPUTS
    ):
        raise PythonEnvironmentError(f"Python {label} are invalid")
    result = tuple(_raw_digest(item, label) for item in value)
    if len(set(result)) != len(result):
        raise PythonEnvironmentError(f"Python {label} are duplicated")
    return result


def _raw_digest(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PythonEnvironmentError(f"{label} is invalid")
    match = _DIGEST.fullmatch(value)
    if match is None:
        raise PythonEnvironmentError(f"{label} is invalid")
    return match.group(1)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


__all__ = [
    "PythonEnvironmentBuilder",
    "PythonEnvironmentCancelled",
    "PythonEnvironmentError",
    "PythonEnvironmentSpec",
    "PythonRuntimeIdentity",
    "SourceBuild",
    "WheelBuildSandbox",
]
