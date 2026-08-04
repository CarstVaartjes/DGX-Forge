"""Strict installed transport and release-runtime policy."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

MAX_POLICY_BYTES = 64 * 1024
MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z"
)
_MACHINE = {"x86_64": 62, "aarch64": 183}


class RuntimePolicyError(ValueError):
    """The installed release runtime policy is unsafe or incompatible."""


@dataclass(frozen=True)
class InstalledORASPolicy:
    executable: Path
    sha256: str
    version: str
    auth_path: Path


@dataclass(frozen=True)
class InstalledTUFPolicy:
    bootstrap_root_path: Path
    bootstrap_root_sha256: str
    metadata_root: Path
    target_root: Path


@dataclass(frozen=True)
class InstalledAdapterPolicy:
    adapter_id: str
    executable_relative_path: str
    timeout_seconds: int
    output_limit_bytes: int


@dataclass(frozen=True)
class RuntimePolicy:
    schema_version: int
    architecture: str
    registry_origin: str
    repository: str
    oras: InstalledORASPolicy
    tuf: InstalledTUFPolicy
    release_root: Path
    staging_root: Path
    adapter: InstalledAdapterPolicy
    _allow_unprivileged_test_files: bool = False

    @classmethod
    def load(cls, path: Path) -> RuntimePolicy:
        return cls._load(Path(path), allow_unprivileged_test_files=False)

    @classmethod
    def _load_for_test(cls, path: Path, prefix: Path) -> RuntimePolicy:
        prefix = Path(prefix)
        if not prefix.is_absolute():
            raise RuntimePolicyError("test installation prefix is invalid")
        return cls._load(
            Path(path),
            allow_unprivileged_test_files=True,
            installation_prefix=prefix,
        )

    @classmethod
    def _load(
        cls,
        path: Path,
        *,
        allow_unprivileged_test_files: bool,
        installation_prefix: Path | None = None,
    ) -> RuntimePolicy:
        raw = _read_file(
            path,
            "runtime policy",
            MAX_POLICY_BYTES,
            mode=0o644,
            root_owned=True,
            allow_unprivileged_test_files=allow_unprivileged_test_files,
        )
        document = _document(raw, "runtime policy")
        root = _object(
            document,
            {
                "schema_version",
                "architecture",
                "registry_origin",
                "repository",
                "oras",
                "tuf",
                "release_root",
                "staging_root",
                "adapter",
            },
            "runtime policy",
        )
        if root["schema_version"] != 1:
            raise RuntimePolicyError("runtime policy schema is unsupported")
        architecture = root["architecture"]
        native = _native_architecture()
        if architecture not in _MACHINE or architecture != native:
            raise RuntimePolicyError("runtime architecture does not match this node")
        registry_origin = _origin(root["registry_origin"])
        repository = root["repository"]
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise RuntimePolicyError("registry repository is invalid")
        oras_raw = _object(
            root["oras"],
            {"executable", "sha256", "version", "auth_path"},
            "ORAS policy",
        )
        if oras_raw["version"] != "1.3.3":
            raise RuntimePolicyError("ORAS version is not reviewed")
        oras_digest = _digest(oras_raw["sha256"], "ORAS executable")
        oras = InstalledORASPolicy(
            _installed_path(
                oras_raw["executable"],
                f"/opt/dgx-forge/third-party/oras/{oras_digest}/oras",
                "ORAS executable",
                installation_prefix,
            ),
            oras_digest,
            "1.3.3",
            _installed_path(
                oras_raw["auth_path"],
                "/var/lib/dgx-forge-agent/registry-auth.json",
                "ORAS auth",
                installation_prefix,
            ),
        )
        tuf_raw = _object(
            root["tuf"],
            {
                "bootstrap_root_path",
                "bootstrap_root_sha256",
                "metadata_root",
                "target_root",
            },
            "TUF policy",
        )
        tuf = InstalledTUFPolicy(
            _installed_path(
                tuf_raw["bootstrap_root_path"],
                "/etc/dgx-forge-agent/tuf-root.json",
                "TUF bootstrap root",
                installation_prefix,
            ),
            _digest(tuf_raw["bootstrap_root_sha256"], "TUF bootstrap root"),
            _installed_path(
                tuf_raw["metadata_root"],
                "/var/lib/dgx-forge-agent/tuf/metadata",
                "TUF metadata root",
                installation_prefix,
            ),
            _installed_path(
                tuf_raw["target_root"],
                "/var/lib/dgx-forge-agent/tuf/targets",
                "TUF target root",
                installation_prefix,
            ),
        )
        adapter_raw = _object(
            root["adapter"],
            {
                "adapter_id",
                "executable_relative_path",
                "timeout_seconds",
                "output_limit_bytes",
            },
            "adapter policy",
        )
        exact_adapter = {
            "adapter_id": "spark-runtime-v1",
            "executable_relative_path": "bin/runtime-adapter",
            "timeout_seconds": 60,
            "output_limit_bytes": 64 * 1024,
        }
        if adapter_raw != exact_adapter:
            raise RuntimePolicyError("compiled adapter policy is not reviewed")
        release_root = _installed_path(
            root["release_root"],
            "/var/lib/dgx-forge/releases",
            "release root",
            installation_prefix,
        )
        staging_root = _installed_path(
            root["staging_root"],
            "/var/lib/dgx-forge/release-staging",
            "staging root",
            installation_prefix,
        )
        return cls(
            1,
            architecture,
            registry_origin,
            repository,
            oras,
            tuf,
            release_root,
            staging_root,
            InstalledAdapterPolicy(**exact_adapter),
            allow_unprivileged_test_files,
        )

    def verify_installed(self) -> None:
        _verify_elf(
            self.oras.executable,
            self.oras.sha256,
            self.architecture,
            allow_unprivileged_test_files=self._allow_unprivileged_test_files,
        )
        _read_file(
            self.oras.auth_path,
            "ORAS auth",
            MAX_POLICY_BYTES,
            mode=0o600,
            root_owned=False,
            allow_unprivileged_test_files=self._allow_unprivileged_test_files,
        )
        bootstrap = _read_file(
            self.tuf.bootstrap_root_path,
            "TUF bootstrap root",
            1024 * 1024,
            mode=0o644,
            root_owned=True,
            allow_unprivileged_test_files=self._allow_unprivileged_test_files,
        )
        if hashlib.sha256(bootstrap).hexdigest() != self.tuf.bootstrap_root_sha256:
            raise RuntimePolicyError("TUF bootstrap root digest is invalid")
        descriptors: list[int] = []
        try:
            for name, path in (
                ("TUF metadata root", self.tuf.metadata_root),
                ("TUF target root", self.tuf.target_root),
                ("release root", self.release_root),
                ("staging root", self.staging_root),
            ):
                descriptors.append(
                    _open_directory(
                        path,
                        name,
                        allow_unprivileged_test_files=self._allow_unprivileged_test_files,
                    )
                )
            if os.fstat(descriptors[2]).st_dev != os.fstat(descriptors[3]).st_dev:
                raise RuntimePolicyError(
                    "release and staging roots use different filesystems"
                )
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    @property
    def allow_unprivileged_test_files(self) -> bool:
        """Expose the test-only ownership relaxation to composed boundaries."""
        return self._allow_unprivileged_test_files

    def read_bootstrap_root(self) -> bytes:
        """Return a freshly descriptor-verified bootstrap root snapshot."""
        raw = _read_file(
            self.tuf.bootstrap_root_path,
            "TUF bootstrap root",
            1024 * 1024,
            mode=0o644,
            root_owned=True,
            allow_unprivileged_test_files=self._allow_unprivileged_test_files,
        )
        if hashlib.sha256(raw).hexdigest() != self.tuf.bootstrap_root_sha256:
            raise RuntimePolicyError("TUF bootstrap root digest is invalid")
        return raw


def _native_architecture() -> str:
    value = platform.machine()
    if value == "AMD64":
        value = "x86_64"
    elif value == "arm64":
        value = "aarch64"
    if value not in _MACHINE:
        raise RuntimePolicyError("node architecture is unsupported")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimePolicyError("runtime policy contains duplicate fields")
        result[key] = value
    return result


def _document(raw: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except RuntimePolicyError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise RuntimePolicyError(f"{name} is invalid JSON") from error
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise RuntimePolicyError(f"{name} is not canonical")
    return value


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _object(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimePolicyError(f"{name} fields are invalid")
    return value


def _absolute(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimePolicyError(f"{name} path is invalid")
    pure = PurePosixPath(value)
    if (
        not pure.is_absolute()
        or str(pure) != value
        or any(part in {"", ".", ".."} for part in pure.parts[1:])
    ):
        raise RuntimePolicyError(f"{name} path is not canonical")
    return Path(value)


def _installed_path(
    value: Any,
    production_path: str,
    name: str,
    installation_prefix: Path | None,
) -> Path:
    actual = _absolute(value, name)
    expected = (
        Path(production_path)
        if installation_prefix is None
        else installation_prefix / production_path.lstrip("/")
    )
    if actual != expected:
        raise RuntimePolicyError(f"{name} is outside its installed location")
    return actual


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise RuntimePolicyError(f"{name} digest is invalid")
    return value


def _origin(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimePolicyError("registry origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise RuntimePolicyError("registry origin is invalid") from error
    canonical = f"https://{parsed.hostname or ''}"
    if port is not None:
        canonical += f":{port}"
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != canonical
    ):
        raise RuntimePolicyError("registry origin is invalid")
    return canonical


def _open_parent(path: Path, *, allow_unprivileged_test_files: bool) -> tuple[int, str]:
    if not path.is_absolute() or len(path.parts) < 2:
        raise RuntimePolicyError("installed path must be absolute")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            owners = {0, os.geteuid()}
            if metadata.st_uid not in owners or (
                mode & 0o022
                and not (allow_unprivileged_test_files and mode & stat.S_ISVTX)
            ):
                raise RuntimePolicyError("installed path ancestry is unsafe")
        return descriptor, path.name
    except RuntimePolicyError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise RuntimePolicyError("installed path traverses a symlink") from error


def _read_file(
    path: Path,
    name: str,
    maximum: int,
    *,
    mode: int,
    root_owned: bool,
    allow_unprivileged_test_files: bool,
) -> bytes:
    parent, leaf = _open_parent(
        path, allow_unprivileged_test_files=allow_unprivileged_test_files
    )
    descriptor = -1
    try:
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
        metadata = os.fstat(descriptor)
        owners = {0}
        if allow_unprivileged_test_files or not root_owned:
            owners.add(os.geteuid())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in owners
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > maximum
        ):
            raise RuntimePolicyError(f"{name} is unsafe")
        raw = os.read(descriptor, maximum + 1)
        if len(raw) > maximum:
            raise RuntimePolicyError(f"{name} is too large")
        return raw
    except RuntimePolicyError:
        raise
    except OSError as error:
        raise RuntimePolicyError(f"{name} cannot be read safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _verify_elf(
    path: Path,
    expected_digest: str,
    architecture: str,
    *,
    allow_unprivileged_test_files: bool,
) -> None:
    raw = _read_file(
        path,
        "ORAS executable",
        MAX_EXECUTABLE_BYTES,
        mode=0o555,
        root_owned=True,
        allow_unprivileged_test_files=allow_unprivileged_test_files,
    )
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise RuntimePolicyError("ORAS executable digest is invalid")
    if len(raw) < 64 or raw[:7] != b"\x7fELF\x02\x01\x01":
        raise RuntimePolicyError("ORAS executable is not ELF")
    elf_type, machine = struct.unpack_from("<HH", raw, 16)
    if elf_type not in {2, 3} or machine != _MACHINE[architecture]:
        raise RuntimePolicyError("ORAS executable architecture is invalid")


def _open_directory(
    path: Path, name: str, *, allow_unprivileged_test_files: bool
) -> int:
    parent, leaf = _open_parent(
        path, allow_unprivileged_test_files=allow_unprivileged_test_files
    )
    try:
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except OSError as error:
        os.close(parent)
        raise RuntimePolicyError(f"{name} is unavailable") from error
    os.close(parent)
    metadata = os.fstat(descriptor)
    owners = {os.geteuid()}
    if allow_unprivileged_test_files:
        owners.add(0)
    if metadata.st_uid not in owners or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise RuntimePolicyError(f"{name} is unsafe")
    return descriptor
