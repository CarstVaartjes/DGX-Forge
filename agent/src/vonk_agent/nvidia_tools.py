"""Pinned NVIDIA tool policy and strict evidence normalization.

The policy is an installation contract.  It contains no network-controlled
values and is expected to be written by the privileged Task-5 installer.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import stat
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

REVIEWED_BUNDLE_VERSION = "0.1.0"
REVIEWED_BUNDLE_SHA256 = (
    "0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3"
)
MAX_POLICY_BYTES = 64 * 1024
MAX_EXECUTABLE_BYTES = 1024 * 1024
MAX_TOOL_OUTPUT_BYTES = 256 * 1024
MAX_TOOL_SECONDS = 15
MAX_FABRIC_PAIRS = 16
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DEVICE_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_SAFE_STATUS = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class InstalledPolicyError(ValueError):
    """The installed policy or a tool document is incompatible or unsafe."""


class InstalledToolSecurityError(InstalledPolicyError):
    """A present installed artifact failed a security/integrity check."""

    error_code = "probe_security_failure"


class ToolName(StrEnum):
    DEVICE_IDENTITY = "device_identity"
    HARDWARE_CONFIG = "hardware_config"
    FIRMWARE_REPORTER = "firmware_reporter"
    OS_BUILD_IDENTITY = "os_build_identity"
    DRIVER_INVENTORY_REPORTER = "driver_inventory_reporter"
    SPARK_DIAGCTL_HEALTH = "spark_diagctl_health"
    RESET_REASON_REPORTER = "reset_reason_reporter"


NVIDIA_TOOL_NAMES = tuple(item.value for item in ToolName)
_EXACT_ARGUMENTS: Mapping[ToolName, tuple[str, ...]] = MappingProxyType(
    {
        ToolName.DEVICE_IDENTITY: ("--stdout-json", "--no-write-file", "--quiet"),
        ToolName.HARDWARE_CONFIG: ("--stdout-json", "--no-write-file", "--quiet"),
        ToolName.FIRMWARE_REPORTER: ("--stdout-json", "--no-write-file", "--quiet"),
        ToolName.OS_BUILD_IDENTITY: ("--stdout-json", "--no-write-file", "--quiet"),
        ToolName.DRIVER_INVENTORY_REPORTER: ("--stdout-json", "--no-write-file", "--quiet"),
        ToolName.SPARK_DIAGCTL_HEALTH: ("--stdout-json", "--no-write-file", "--quiet", "health"),
        ToolName.RESET_REASON_REPORTER: ("--stdout-json", "--no-write-file", "--quiet"),
    }
)
_TOOL_CONTRACT: Mapping[ToolName, tuple[str, str]] = MappingProxyType(
    {
        ToolName.DEVICE_IDENTITY: ("bin/device_identity.py", "1.1.0"),
        ToolName.HARDWARE_CONFIG: ("bin/hardware_config.py", "1.0.0"),
        ToolName.FIRMWARE_REPORTER: ("bin/firmware_reporter.py", "1.0.0"),
        ToolName.OS_BUILD_IDENTITY: ("bin/os_build_identity.py", "1.0.0"),
        ToolName.DRIVER_INVENTORY_REPORTER: ("bin/driver_inventory_reporter.py", "1.0.0"),
        ToolName.SPARK_DIAGCTL_HEALTH: ("bin/spark_diagctl.py", "1.1.0"),
        ToolName.RESET_REASON_REPORTER: ("bin/reset_reason_reporter.py", "1.1.0"),
    }
)
REVIEWED_TOOL_SHA256: Mapping[ToolName, str] = MappingProxyType(
    {
        ToolName.DEVICE_IDENTITY: "110acb65e54092a63d93f8d0448855717323c7251bbaf661a7d6cb41836f2dcf",
        ToolName.HARDWARE_CONFIG: "07c05c03f65e9b707bc18ebd2ec010ac1622701fa0b87858014a5b71fd1af5bb",
        ToolName.FIRMWARE_REPORTER: "c5887cb8b456295ea937a44cf05d8c1a3fa64b2ac8239f35be61e8deb358d387",
        ToolName.OS_BUILD_IDENTITY: "ee2f06d7ae25438ed0a7258eeeecdde76dba24c5c82f9dec510c361b9d75f6f9",
        ToolName.DRIVER_INVENTORY_REPORTER: "f5f90c05f077f1cd6fa387d1f6eac3b7f40b7d859c6e5886c73ec03629fdfc26",
        ToolName.SPARK_DIAGCTL_HEALTH: "03de23664d3a24295ce605075be957328f47c24fa37afb7bbfe60988cbee42c2",
        ToolName.RESET_REASON_REPORTER: "212b49f894e4703cc85743217a0a9d9f2bb5891702266df84b907df960d83774",
    }
)
_REVIEWED_FILE_SIZES: Mapping[str, int] = MappingProxyType(
    {
        "110acb65e54092a63d93f8d0448855717323c7251bbaf661a7d6cb41836f2dcf": 14452,
        "07c05c03f65e9b707bc18ebd2ec010ac1622701fa0b87858014a5b71fd1af5bb": 27452,
        "c5887cb8b456295ea937a44cf05d8c1a3fa64b2ac8239f35be61e8deb358d387": 34784,
        "ee2f06d7ae25438ed0a7258eeeecdde76dba24c5c82f9dec510c361b9d75f6f9": 26007,
        "f5f90c05f077f1cd6fa387d1f6eac3b7f40b7d859c6e5886c73ec03629fdfc26": 33092,
        "03de23664d3a24295ce605075be957328f47c24fa37afb7bbfe60988cbee42c2": 34210,
        "212b49f894e4703cc85743217a0a9d9f2bb5891702266df84b907df960d83774": 29156,
        "35277c9d42c97960434f10e7f8dfda0a7e12cfbe00aec0d86ea88099c5ac9eca": 8072,
        "0b1f72a2056cbb5a3c717e7853b7f4d986a4b91b7920eadab68888b101f1b1da": 15147,
        "6938255c277aa5b3b2e805a2cbfdc52d86c5d19910591cb42272a7eb280e2426": 9200,
        "a3b4329f7500a2f9d95369ba32b3eb563c27a76d6d96d9f98dac1c1fc41b938a": 754,
    }
)
_REVIEWED_SUPPORT_FILES: Mapping[str, str] = MappingProxyType(
    {
        "bin/common/asset_id.py": "35277c9d42c97960434f10e7f8dfda0a7e12cfbe00aec0d86ea88099c5ac9eca",
        "bin/common/cli_base.py": "0b1f72a2056cbb5a3c717e7853b7f4d986a4b91b7920eadab68888b101f1b1da",
        "bin/common/output.py": "6938255c277aa5b3b2e805a2cbfdc52d86c5d19910591cb42272a7eb280e2426",
        "bin/common/__init__.py": "a3b4329f7500a2f9d95369ba32b3eb563c27a76d6d96d9f98dac1c1fc41b938a",
    }
)


@dataclass(frozen=True)
class FabricPair:
    interface: str
    hca: str


@dataclass(frozen=True)
class ToolPolicy:
    name: ToolName
    version: str
    executable: Path
    sha256: str
    arguments: tuple[str, ...]
    timeout_seconds: int
    output_limit_bytes: int


@dataclass(frozen=True)
class SupportFilePolicy:
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class HealthPolicy:
    executable: Path
    sha256: str
    cpu_sample_ms: int
    fabric_pairs: tuple[FabricPair, ...]
    timeout_seconds: int
    output_limit_bytes: int

    @property
    def arguments(self) -> tuple[str, ...]:
        values: list[str] = ["--json", "--cpu-sample-ms", str(self.cpu_sample_ms)]
        for pair in self.fabric_pairs:
            values.extend(("--interface", pair.interface, "--hca", pair.hca))
        return tuple(values)


@dataclass(frozen=True)
class InstalledPolicy:
    schema_version: int
    bundle_version: str
    bundle_sha256: str
    bundle_root: Path
    tools: tuple[ToolPolicy, ...]
    support_files: tuple[SupportFilePolicy, ...]
    health: HealthPolicy
    _test_only_allow_unprivileged: bool = False

    @classmethod
    def load(cls, path: Path) -> InstalledPolicy:
        return cls._load(Path(path), test_only_allow_unprivileged=False)

    @classmethod
    def _load_for_test(cls, path: Path) -> InstalledPolicy:
        return cls._load(Path(path), test_only_allow_unprivileged=True)

    @classmethod
    def _load(
        cls, path: Path, *, test_only_allow_unprivileged: bool
    ) -> InstalledPolicy:
        raw = _read_policy(
            path, test_only_allow_unprivileged=test_only_allow_unprivileged
        )
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        except UnicodeDecodeError as error:
            raise InstalledPolicyError("installed policy must be UTF-8") from error
        except json.JSONDecodeError as error:
            raise InstalledPolicyError("installed policy must be valid JSON") from error
        root = _object(
            document,
            {
                "schema_version",
                "bundle_version",
                "bundle_sha256",
                "bundle_root",
                "tools",
                "support_files",
                "health",
            },
            "installed policy",
        )
        if _integer(root["schema_version"], 1, 1, "schema version") != 1:
            raise InstalledPolicyError("unsupported installed policy schema")
        if root["bundle_version"] != REVIEWED_BUNDLE_VERSION:
            raise InstalledPolicyError("bundle version is not reviewed")
        if root["bundle_sha256"] != REVIEWED_BUNDLE_SHA256:
            raise InstalledPolicyError("bundle digest is not reviewed")
        bundle_root = _absolute(root["bundle_root"], "bundle root")
        tools_value = root["tools"]
        if not isinstance(tools_value, list) or len(tools_value) != len(ToolName):
            raise InstalledPolicyError("installed policy must contain every reviewed tool")
        parsed: dict[ToolName, ToolPolicy] = {}
        for value in tools_value:
            tool = _tool(value, bundle_root)
            if tool.name in parsed:
                raise InstalledPolicyError("installed policy contains duplicate tools")
            parsed[tool.name] = tool
        if set(parsed) != set(ToolName):
            raise InstalledPolicyError("installed policy tool set is incomplete")
        support = _support_files(root["support_files"])
        health = _health(root["health"])
        return cls(
            1,
            REVIEWED_BUNDLE_VERSION,
            REVIEWED_BUNDLE_SHA256,
            bundle_root,
            tuple(parsed[name] for name in ToolName),
            support,
            health,
            test_only_allow_unprivileged,
        )

    def verify_installed(self) -> Mapping[ToolName, bool]:
        root_status = _verify_directory(
            self.bundle_root,
            test_only_allow_unprivileged=self._test_only_allow_unprivileged,
        )
        if not root_status:
            return MappingProxyType({name: False for name in ToolName})
        verify_reviewed_support_files(self)
        result: dict[ToolName, bool] = {}
        for tool in self.tools:
            result[tool.name] = _verify_executable(
                tool.executable,
                tool.sha256,
                test_only_allow_unprivileged=self._test_only_allow_unprivileged,
            )
        return MappingProxyType(result)

    def verify_health_collector(self) -> bool:
        return _verify_executable(
            self.health.executable,
            self.health.sha256,
            test_only_allow_unprivileged=self._test_only_allow_unprivileged,
        )

    def bundle_root_available(self) -> bool:
        return _verify_directory(
            self.bundle_root,
            test_only_allow_unprivileged=self._test_only_allow_unprivileged,
        )


@dataclass(frozen=True)
class NormalizedToolDocument:
    ok: bool
    data: Mapping[str, Any]


def parse_tool_document(
    name: ToolName, raw: bytes, *, limit: int
) -> NormalizedToolDocument:
    if not isinstance(name, ToolName):
        raise InstalledPolicyError("unknown NVIDIA tool")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_TOOL_OUTPUT_BYTES:
        raise InstalledPolicyError("tool output limit is invalid")
    if len(raw) > limit:
        raise InstalledPolicyError("NVIDIA tool output is too large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except UnicodeDecodeError as error:
        raise InstalledPolicyError("NVIDIA tool output must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise InstalledPolicyError("NVIDIA tool output must be valid JSON") from error
    envelope = _object(value, {"ok", "data", "errors", "meta"}, "NVIDIA envelope")
    if type(envelope["ok"]) is not bool:
        raise InstalledPolicyError("NVIDIA envelope status is invalid")
    if not isinstance(envelope["errors"], list) or len(envelope["errors"]) > 64:
        raise InstalledPolicyError("NVIDIA envelope errors are invalid")
    if not isinstance(envelope["meta"], dict):
        raise InstalledPolicyError("NVIDIA envelope metadata is invalid")
    data = envelope["data"]
    if data is None and envelope["ok"] is False:
        data = {}
    elif not isinstance(data, dict):
        raise InstalledPolicyError("NVIDIA envelope data is invalid")
    normalized = _NORMALIZERS[name](data)
    return NormalizedToolDocument(envelope["ok"], _freeze(normalized))


def normalize_tool_document(
    name: ToolName, raw: bytes, *, limit: int
) -> Mapping[str, Any]:
    return parse_tool_document(name, raw, limit=limit).data


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise InstalledPolicyError("JSON document contains duplicate fields")
        value[key] = item
    return value


def _object(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise InstalledPolicyError(f"{name} fields are invalid")
    return value


def _integer(value: Any, minimum: int, maximum: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise InstalledPolicyError(f"{name} is invalid")
    return value


def _absolute(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InstalledPolicyError(f"{name} is invalid")
    pure = PurePosixPath(value)
    if not pure.is_absolute() or str(pure) != value or any(part in {".", ".."} for part in pure.parts):
        raise InstalledPolicyError(f"{name} must be an absolute canonical path")
    return Path(value)


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise InstalledPolicyError(f"{name} digest is invalid")
    return value


def _tool(value: Any, root: Path) -> ToolPolicy:
    item = _object(
        value,
        {"name", "version", "executable", "sha256", "arguments", "timeout_seconds", "output_limit_bytes"},
        "tool policy",
    )
    try:
        name = ToolName(item["name"])
    except (TypeError, ValueError) as error:
        raise InstalledPolicyError("tool name is not reviewed") from error
    executable = _absolute(item["executable"], "tool executable")
    relative_path, reviewed_version = _TOOL_CONTRACT[name]
    if executable != root / relative_path:
        raise InstalledPolicyError("tool executable is not the reviewed fixed path")
    if item["version"] != reviewed_version:
        raise InstalledPolicyError("tool version is not reviewed")
    digest = _digest(item["sha256"], "tool")
    if digest != REVIEWED_TOOL_SHA256[name]:
        raise InstalledPolicyError("tool digest is not reviewed")
    arguments = item["arguments"]
    if not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
        raise InstalledPolicyError("tool arguments are invalid")
    rendered = tuple(arguments)
    if rendered != _EXACT_ARGUMENTS[name]:
        raise InstalledPolicyError("tool arguments are not the reviewed safe subset")
    if any(len(argument.encode("utf-8")) > 256 or "/" in argument or "\\" in argument or "\x00" in argument for argument in rendered):
        raise InstalledPolicyError("tool arguments are unsafe")
    return ToolPolicy(
        name,
        reviewed_version,
        executable,
        digest,
        rendered,
        _integer(item["timeout_seconds"], 1, MAX_TOOL_SECONDS, "tool timeout"),
        _integer(item["output_limit_bytes"], 1, MAX_TOOL_OUTPUT_BYTES, "tool output limit"),
    )


def _health(value: Any) -> HealthPolicy:
    item = _object(
        value,
        {"executable", "sha256", "cpu_sample_ms", "fabric_pairs", "timeout_seconds", "output_limit_bytes"},
        "health policy",
    )
    pairs_value = item["fabric_pairs"]
    if not isinstance(pairs_value, list) or not 1 <= len(pairs_value) <= MAX_FABRIC_PAIRS:
        raise InstalledPolicyError("health fabric pairs are invalid")
    pairs: list[FabricPair] = []
    interfaces: set[str] = set()
    hcas: set[str] = set()
    for raw in pairs_value:
        pair = _object(raw, {"interface", "hca"}, "health fabric pair")
        interface, hca = pair["interface"], pair["hca"]
        if not isinstance(interface, str) or not _DEVICE_NAME.fullmatch(interface):
            raise InstalledPolicyError("health interface is invalid")
        if not isinstance(hca, str) or not _DEVICE_NAME.fullmatch(hca):
            raise InstalledPolicyError("health HCA is invalid")
        if interface in interfaces or hca in hcas:
            raise InstalledPolicyError("health fabric pair is duplicated")
        interfaces.add(interface)
        hcas.add(hca)
        pairs.append(FabricPair(interface, hca))
    return HealthPolicy(
        _absolute(item["executable"], "health executable"),
        _digest(item["sha256"], "health collector"),
        _integer(item["cpu_sample_ms"], 1, 10_000, "CPU sample"),
        tuple(pairs),
        _integer(item["timeout_seconds"], 1, MAX_TOOL_SECONDS, "health timeout"),
        _integer(item["output_limit_bytes"], 1, MAX_TOOL_OUTPUT_BYTES, "health output limit"),
    )


def _support_files(value: Any) -> tuple[SupportFilePolicy, ...]:
    if not isinstance(value, list) or len(value) != len(_REVIEWED_SUPPORT_FILES):
        raise InstalledPolicyError("reviewed support file set is incomplete")
    parsed: dict[str, SupportFilePolicy] = {}
    for raw in value:
        item = _object(raw, {"relative_path", "sha256", "size_bytes"}, "support file policy")
        relative = item["relative_path"]
        if not isinstance(relative, str) or relative not in _REVIEWED_SUPPORT_FILES:
            raise InstalledPolicyError("support file path is not reviewed")
        if relative in parsed:
            raise InstalledPolicyError("support file policy is duplicated")
        digest = _digest(item["sha256"], "support file")
        if digest != _REVIEWED_SUPPORT_FILES[relative]:
            raise InstalledPolicyError("support file digest is not reviewed")
        size = _integer(item["size_bytes"], 1, MAX_EXECUTABLE_BYTES, "support file size")
        if size != _REVIEWED_FILE_SIZES[digest]:
            raise InstalledPolicyError("support file size is not reviewed")
        parsed[relative] = SupportFilePolicy(relative, digest, size)
    if set(parsed) != set(_REVIEWED_SUPPORT_FILES):
        raise InstalledPolicyError("reviewed support file set is incomplete")
    return tuple(parsed[name] for name in sorted(parsed))


def _read_policy(path: Path, *, test_only_allow_unprivileged: bool) -> bytes:
    parent, leaf = _open_parent(
        path, test_only_allow_unprivileged=test_only_allow_unprivileged
    )
    descriptor = -1
    try:
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
        metadata = os.fstat(descriptor)
        _trusted(
            metadata,
            directory=False,
            executable=False,
            test_only_allow_unprivileged=test_only_allow_unprivileged,
        )
        if not stat.S_ISREG(metadata.st_mode):
            raise InstalledPolicyError("installed policy must be a regular file")
        if metadata.st_size > MAX_POLICY_BYTES:
            raise InstalledPolicyError("installed policy is too large")
        raw = os.read(descriptor, MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise InstalledPolicyError("installed policy is too large")
        return raw
    except InstalledPolicyError:
        raise
    except OSError as error:
        raise InstalledPolicyError("installed policy cannot be read safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _open_parent(
    path: Path,
    *,
    test_only_allow_unprivileged: bool = False,
    check_deadline: Callable[[], None] | None = None,
) -> tuple[int, str]:
    if not path.is_absolute() or len(path.parts) < 2:
        raise InstalledPolicyError("path must be absolute")
    _check(check_deadline)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    succeeded = False
    try:
        _check(check_deadline)
        for component in path.parts[1:-1]:
            _check(check_deadline)
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            try:
                _check(check_deadline)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
            _check(check_deadline)
            metadata = os.fstat(descriptor)
            _check(check_deadline)
            _trusted(
                metadata,
                directory=True,
                executable=False,
                test_only_allow_unprivileged=test_only_allow_unprivileged,
            )
            _check(check_deadline)
        succeeded = True
        return descriptor, path.name
    except InstalledPolicyError:
        raise
    except OSError as error:
        raise InstalledPolicyError("path must not traverse symlinks") from error
    finally:
        if not succeeded:
            os.close(descriptor)


def _trusted(
    metadata: os.stat_result,
    *,
    directory: bool,
    executable: bool,
    test_only_allow_unprivileged: bool = False,
) -> None:
    permitted_owners = {0}
    if test_only_allow_unprivileged:
        permitted_owners.add(os.geteuid())
    if metadata.st_uid not in permitted_owners:
        raise InstalledToolSecurityError("installed path owner is unsafe")
    mode = stat.S_IMODE(metadata.st_mode)
    if test_only_allow_unprivileged and directory and mode & stat.S_ISVTX:
        return
    if mode & 0o022 or mode & (stat.S_ISUID | stat.S_ISGID):
        raise InstalledToolSecurityError("installed path mode is unsafe")
    if executable and mode & 0o111 == 0:
        raise InstalledToolSecurityError("installed executable mode is unsafe")


def _verify_directory(
    path: Path, *, test_only_allow_unprivileged: bool = False
) -> bool:
    try:
        parent, leaf = _open_parent(
            path,
            test_only_allow_unprivileged=test_only_allow_unprivileged,
        )
    except InstalledPolicyError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return False
        raise InstalledToolSecurityError("bundle root is unsafe") from error
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return False
        metadata = os.fstat(descriptor)
        _trusted(
            metadata,
            directory=True,
            executable=False,
            test_only_allow_unprivileged=test_only_allow_unprivileged,
        )
        return True
    except InstalledPolicyError:
        raise
    except OSError as error:
        raise InstalledToolSecurityError("bundle root is unsafe") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _verify_executable(
    path: Path,
    expected_digest: str,
    *,
    test_only_allow_unprivileged: bool = False,
) -> bool:
    descriptor = open_verified_executable(
        path,
        expected_digest,
        _test_only_allow_unprivileged=test_only_allow_unprivileged,
    )
    if descriptor is None:
        return False
    os.close(descriptor)
    return True


def open_verified_executable(
    path: Path,
    expected_digest: str,
    *,
    _test_only_allow_unprivileged: bool = False,
    _check_deadline: Callable[[], None] | None = None,
) -> int | None:
    """Return a sealed snapshot so later inode mutations cannot change execution."""
    return _open_verified_file(
        path,
        expected_digest,
        executable=True,
        test_only_allow_unprivileged=_test_only_allow_unprivileged,
        check_deadline=_check_deadline,
    )


def verify_reviewed_support_files(
    policy: InstalledPolicy,
    *,
    _check_deadline: Callable[[], None] | None = None,
) -> None:
    """Verify that the exact reviewed imports can be snapshotted safely."""
    descriptor = open_verified_support_archive(
        policy, _check_deadline=_check_deadline
    )
    os.close(descriptor)


def open_verified_support_archive(
    policy: InstalledPolicy,
    *,
    _check_deadline: Callable[[], None] | None = None,
) -> int:
    """Return a deterministic sealed ZIP containing only reviewed import bytes."""
    common = policy.bundle_root / "bin" / "common"
    _check(_check_deadline)
    try:
        parent, leaf = _open_parent(
            common,
            test_only_allow_unprivileged=policy._test_only_allow_unprivileged,
            check_deadline=_check_deadline,
        )
    except InstalledPolicyError as error:
        raise InstalledToolSecurityError("reviewed support directory is unsafe") from error
    descriptor = -1
    try:
        _check(_check_deadline)
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
        _check(_check_deadline)
        _check(_check_deadline)
        metadata = os.fstat(descriptor)
        _check(_check_deadline)
        _trusted(
            metadata,
            directory=True,
            executable=False,
            test_only_allow_unprivileged=policy._test_only_allow_unprivileged,
        )
        _check(_check_deadline)
        expected_names = {Path(item.relative_path).name for item in policy.support_files}
        _check(_check_deadline)
        installed_names = set(os.listdir(descriptor))
        _check(_check_deadline)
        if installed_names != expected_names:
            raise InstalledToolSecurityError("reviewed support directory has unexpected entries")
    except InstalledPolicyError:
        raise
    except OSError as error:
        raise InstalledToolSecurityError("reviewed support directory is unsafe") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
    support_bytes: dict[str, bytes] = {}
    for item in policy.support_files:
        snapshot = _open_verified_file(
            policy.bundle_root / item.relative_path,
            item.sha256,
            executable=False,
            expected_size=item.size_bytes,
            test_only_allow_unprivileged=policy._test_only_allow_unprivileged,
            check_deadline=_check_deadline,
        )
        if snapshot is None:
            raise InstalledToolSecurityError("reviewed support file is unavailable")
        try:
            _check(_check_deadline)
            support_bytes[Path(item.relative_path).name] = os.pread(
                snapshot, item.size_bytes, 0
            )
            _check(_check_deadline)
        finally:
            os.close(snapshot)
    rendered = io.BytesIO()
    with zipfile.ZipFile(rendered, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(support_bytes):
            _check(_check_deadline)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100444 << 16
            archive.writestr(info, support_bytes[name])
            _check(_check_deadline)
    return _sealed_memfd(
        "vonk-agent-reviewed-support",
        rendered.getvalue(),
        check_deadline=_check_deadline,
    )


def _open_verified_file(
    path: Path,
    expected_digest: str,
    *,
    executable: bool,
    expected_size: int | None = None,
    test_only_allow_unprivileged: bool = False,
    check_deadline: Callable[[], None] | None = None,
) -> int | None:
    _check(check_deadline)
    try:
        parent, leaf = _open_parent(
            path,
            test_only_allow_unprivileged=test_only_allow_unprivileged,
            check_deadline=check_deadline,
        )
    except InstalledPolicyError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return None
        raise InstalledToolSecurityError("installed executable path is unsafe") from error
    descriptor = -1
    snapshot = -1
    try:
        try:
            _check(check_deadline)
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            _check(check_deadline)
        except FileNotFoundError:
            return None
        _check(check_deadline)
        metadata = os.fstat(descriptor)
        _check(check_deadline)
        _trusted(
            metadata,
            directory=False,
            executable=executable,
            test_only_allow_unprivileged=test_only_allow_unprivileged,
        )
        _check(check_deadline)
        if not stat.S_ISREG(metadata.st_mode):
            raise InstalledToolSecurityError("installed executable is not a regular file")
        locked_size = expected_size if expected_size is not None else _REVIEWED_FILE_SIZES.get(expected_digest)
        if locked_size is not None and metadata.st_size != locked_size:
            raise InstalledToolSecurityError("installed executable size mismatch")
        if metadata.st_size > MAX_EXECUTABLE_BYTES:
            raise InstalledToolSecurityError("installed executable is too large")
        digest = hashlib.sha256()
        _check(check_deadline)
        total = 0
        _check(check_deadline)
        snapshot = os.memfd_create(
            "vonk-agent-verified-artifact", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        _check(check_deadline)
        while True:
            _check(check_deadline)
            chunk = os.read(descriptor, 64 * 1024)
            _check(check_deadline)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EXECUTABLE_BYTES:
                raise InstalledToolSecurityError("installed executable is too large")
            digest.update(chunk)
            _write_all(snapshot, chunk, check_deadline=check_deadline)
        if digest.hexdigest() != expected_digest:
            raise InstalledToolSecurityError("installed executable digest mismatch")
        _check(check_deadline)
        after = os.fstat(descriptor)
        _check(check_deadline)
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise InstalledToolSecurityError("installed executable changed during verification")
        _seal(snapshot)
        _check(check_deadline)
        os.lseek(snapshot, 0, os.SEEK_SET)
        verified = snapshot
        snapshot = -1
        return verified
    except InstalledPolicyError:
        raise
    except OSError as error:
        raise InstalledToolSecurityError("installed executable cannot be verified") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if snapshot >= 0:
            os.close(snapshot)
        os.close(parent)


def _sealed_memfd(
    name: str,
    content: bytes,
    *,
    check_deadline: Callable[[], None] | None = None,
) -> int:
    _check(check_deadline)
    descriptor = os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        _check(check_deadline)
        _write_all(descriptor, content, check_deadline=check_deadline)
        _seal(descriptor)
        _check(check_deadline)
        os.lseek(descriptor, 0, os.SEEK_SET)
        sealed = descriptor
        descriptor = -1
        return sealed
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_all(
    descriptor: int,
    content: bytes,
    *,
    check_deadline: Callable[[], None] | None = None,
) -> None:
    offset = 0
    while offset < len(content):
        _check(check_deadline)
        written = os.write(descriptor, content[offset:])
        _check(check_deadline)
        if written <= 0:
            raise InstalledToolSecurityError("verified snapshot could not be written")
        offset += written


def _check(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        callback()


def _seal(descriptor: int) -> None:
    seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
    fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)


def _safe_string(value: Any, *, maximum: int = 256) -> str | None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        return None
    if any(ord(character) < 32 for character in value) or "/" in value or "\\" in value:
        return None
    candidate = value.strip("{}")
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", candidate):
        return None
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", value):
        return None
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        pass
    else:
        return None
    return value


def _safe_status_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    return lowered if _SAFE_STATUS.fullmatch(lowered) else None


def _number(value: Any, minimum: float, maximum: float) -> int | float | None:
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?%?", value):
        stripped = value.removesuffix("%")
        value = float(stripped) if "." in stripped else int(stripped)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return None
    return value


def _selected(source: Mapping[str, Any], strings=(), numbers=(), statuses=(), booleans=()) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in strings:
        value = _safe_string(source.get(key))
        if value is not None:
            output[key] = value
    for key, minimum, maximum in numbers:
        value = _number(source.get(key), minimum, maximum)
        if value is not None:
            output[key] = value
    for key in statuses:
        value = _safe_status_value(source.get(key))
        if value is not None:
            output[key] = value
    for key in booleans:
        value = source.get(key)
        if type(value) is bool:
            output[key] = value
    return output


def _device(data: Mapping[str, Any]) -> dict[str, Any]:
    return _selected(data, strings=("sys_vendor", "product_name", "product_version"))


def _hardware(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    platform = _selected(_map(data.get("platform_dmi")), strings=("sys_vendor", "product_name", "product_version"))
    if platform:
        result["platform"] = platform
    cpu_source = _map(data.get("cpu"))
    cpu = _selected(
        cpu_source,
        strings=("architecture",),
        numbers=(("logical_cpus", 1, 8192), ("sockets", 1, 1024), ("cores_per_socket", 1, 8192), ("threads_per_core", 1, 128), ("max_mhz", 0, 1_000_000), ("min_mhz", 0, 1_000_000)),
    )
    models = _strings(cpu_source.get("model_names"), 16, 256)
    if models:
        cpu["model_names"] = models
    if cpu:
        result["cpu"] = cpu
    memory = _selected(
        _map(data.get("memory")),
        numbers=(("mem_total_bytes", 0, 2**63 - 1), ("mem_free_bytes", 0, 2**63 - 1), ("mem_available_bytes", 0, 2**63 - 1)),
    )
    if memory:
        result["memory"] = memory
    list_specs = {
        "storage": (("type", "model", "tran"), (("size_bytes", 0, 2**63 - 1),), (), ("rota",)),
        "network": (("operstate", "driver", "driver_version", "firmware_version", "pci_vendor_id", "pci_device_id"), (("mtu", 0, 1_000_000), ("speed_mbps", 0, 1_000_000)), (), ("is_virtual", "is_wireless")),
        "gpu": (("name", "driver_version"), (("index", 0, 1024), ("memory_total_mib", 0, 2**53)), (), ()),
        "pci": (("class_text", "vendor_device_id", "description"), (), (), ()),
    }
    for key, spec in list_specs.items():
        records = _records_allowlisted(data.get(key), *spec)
        if records:
            result[key] = records
    return result


def _os(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    os_source = _map(data.get("os"))
    os_release = _map(os_source.get("os_release"))
    release = _selected(os_release, strings=("ID", "VERSION_ID", "VERSION", "PRETTY_NAME", "UBUNTU_CODENAME"))
    kernel = _selected(_map(os_source.get("kernel")), strings=("uname_r",))
    if release or kernel:
        result["os"] = {**({"os_release": release} if release else {}), **({"kernel": kernel} if kernel else {})}
    dgx_release = _selected(
        _map(_map(data.get("dgx")).get("dgx_release")),
        strings=("DGX_SWBUILD_VERSION", "DGX_SWBUILD_DATE", "DGX_COMMIT_ID"),
    )
    if dgx_release:
        result["dgx"] = {"dgx_release": dgx_release}
    baseline_source = _map(data.get("baseline"))
    baseline: dict[str, Any] = {}
    fingerprint = baseline_source.get("fingerprint_sha256")
    if isinstance(fingerprint, str) and _DIGEST.fullmatch(fingerprint):
        baseline["fingerprint_sha256"] = fingerprint
    package_records: list[dict[str, str]] = []
    packages = baseline_source.get("packages")
    if isinstance(packages, dict):
        packages = [{"name": key, "version": value} for key, value in packages.items()]
        package_records = list(_records_allowlisted(packages, ("name", "version"), (), (), (), limit=128))
    snaps = _records_allowlisted(baseline_source.get("snaps"), ("name", "version", "rev"), (), (), (), limit=128)
    normalized_snaps = []
    for snap in snaps:
        item = dict(snap)
        if "rev" in item:
            item["revision"] = item.pop("rev")
        normalized_snaps.append(item)
    if package_records:
        baseline["packages"] = package_records
    if normalized_snaps:
        baseline["snaps"] = normalized_snaps
    if baseline:
        result["baseline"] = baseline
    return result


def _diagnostics(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    cpu_source = _map(data.get("cpu"))
    cpu = _selected(cpu_source, numbers=(("cpu_count", 0, 8192), ("cpu_usage_percent", 0, 100)))
    loads = _selected(_map(cpu_source.get("load_average")), numbers=(("1min", 0, 1_000_000), ("5min", 0, 1_000_000), ("15min", 0, 1_000_000)))
    if loads:
        cpu["load_average"] = loads
    if cpu:
        result["cpu"] = cpu
    memory = _selected(_map(data.get("memory")), numbers=(("mem_total_kb", 0, 2**53), ("mem_free_kb", 0, 2**53), ("mem_available_kb", 0, 2**53), ("mem_used_percent", 0, 100)))
    if memory:
        result["memory"] = memory
    filesystems = _map(data.get("disk")).get("filesystems")
    disk_count = 0
    percents: list[float] = []
    if isinstance(filesystems, list):
        for item in filesystems[:64]:
            if isinstance(item, dict):
                disk_count += 1
                percent = _number(item.get("use_percent"), 0, 100)
                if percent is not None:
                    percents.append(float(percent))
    if disk_count:
        result["disk"] = {"count": disk_count, **({"maximum_used_percent": max(percents)} if percents else {})}
    interfaces = _map(data.get("network")).get("interfaces")
    if isinstance(interfaces, list):
        network = {"interface_count": min(len(interfaces), 64), "up_count": 0, "rx_bytes": 0, "tx_bytes": 0}
        for item in interfaces[:64]:
            if not isinstance(item, dict):
                continue
            if str(item.get("state", "")).lower() == "up":
                network["up_count"] += 1
            for key in ("rx_bytes", "tx_bytes"):
                amount = _number(item.get(key), 0, 2**63 - 1)
                if amount is not None:
                    network[key] += int(amount)
        result["network"] = network
    thermal_source = _map(data.get("thermal"))
    if type(thermal_source.get("sensors_available")) is bool:
        result["thermal"] = {"sensors_available": thermal_source["sensors_available"]}
    gpu_source = _map(data.get("gpu"))
    gpu_records = _records_allowlisted(
        gpu_source.get("gpus"),
        ("name",),
        (("index", 0, 1024), ("temp_c", -100, 300), ("util_gpu_percent", 0, 100), ("util_mem_percent", 0, 100), ("mem_used_mib", 0, 2**53), ("mem_total_mib", 0, 2**53), ("power_draw_w", 0, 100_000), ("power_limit_w", 0, 100_000)),
        (), (),
    )
    if gpu_records or type(gpu_source.get("nvidia_smi_available")) is bool:
        result["gpu"] = {"gpus": gpu_records}
        if type(gpu_source.get("nvidia_smi_available")) is bool:
            result["gpu"]["nvidia_smi_available"] = gpu_source["nvidia_smi_available"]
    return result


def _reset(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current = _selected(_map(data.get("current_boot")), strings=("kernel",), numbers=(("uptime_seconds", 0, 2**63 - 1),))
    last = _selected(_map(data.get("last_reset")), statuses=("reason_code",), numbers=(("confidence", 0, 100),))
    if current:
        result["current_boot"] = current
    if last:
        result["last_reset"] = last
    return result


def _firmware(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    platform = _selected(_map(data.get("platform_dmi")), strings=("bios_vendor", "bios_version", "bios_date", "sys_vendor", "product_name", "product_version"))
    if platform:
        result["platform"] = platform
    fwupd_source = _map(data.get("fwupd"))
    fwupd = _selected(fwupd_source, strings=("fwupdmgr_version",), booleans=("available",))
    devices = _records_allowlisted(fwupd_source.get("devices"), ("name", "current_version", "minimum_version", "vendor"), (), ("update_state",), ())
    if devices:
        fwupd["devices"] = devices
    if fwupd:
        result["fwupd"] = fwupd
    specs = {
        "nics": (("driver", "driver_version", "firmware_version"), (), (), ("is_wireless",)),
        "nvme": (("model", "firmware_rev"), (), (), ()),
        "gpu": (("name", "driver_version", "vbios_version", "gsp_firmware_version", "inforom"), (("index", 0, 1024),), (), ()),
        "pci": (("class_text", "vendor_device_id", "description"), (), (), ()),
    }
    for key, spec in specs.items():
        records = _records_allowlisted(data.get(key), *spec)
        if records:
            result[key] = records
    return result


def _drivers(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    kernel = _selected(_map(data.get("kernel")), strings=("uname_r",))
    if kernel:
        result["kernel"] = kernel
    manifest: list[dict[str, Any]] = []
    raw_manifest = data.get("drivers_manifest")
    if isinstance(raw_manifest, list):
        for raw in raw_manifest[:128]:
            if not isinstance(raw, dict):
                continue
            item = _selected(raw, strings=("module",))
            modinfo = _map(raw.get("modinfo"))
            for key in ("version", "license", "firmware"):
                value = _safe_string(modinfo.get(key))
                if value is not None:
                    item[key] = value
            if item:
                manifest.append(item)
    manifest.sort(key=lambda item: canonical_bytes(item))
    if manifest:
        result["drivers_manifest"] = manifest
    gpu = _records_allowlisted(data.get("gpu"), ("name", "driver_version"), (), (), ())
    nics = _records_allowlisted(data.get("nics"), ("driver", "driver_version", "firmware_version"), (), (), ())
    if gpu:
        result["gpu"] = gpu
    if nics:
        result["nics"] = nics
    return result


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any, limit: int, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = {item for item in (_safe_string(raw, maximum=maximum) for raw in value[:limit]) if item is not None}
    return sorted(result)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _records_allowlisted(value: Any, strings=(), numbers=(), statuses=(), booleans=(), *, limit: int = 64) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, list):
        for raw in value[:limit]:
            if isinstance(raw, dict):
                selected = _selected(raw, strings=strings, numbers=numbers, statuses=statuses, booleans=booleans)
                if selected:
                    result.append(selected)
    result.sort(key=canonical_bytes)
    return result


_NORMALIZERS = {
    ToolName.DEVICE_IDENTITY: _device,
    ToolName.HARDWARE_CONFIG: _hardware,
    ToolName.FIRMWARE_REPORTER: _firmware,
    ToolName.OS_BUILD_IDENTITY: _os,
    ToolName.DRIVER_INVENTORY_REPORTER: _drivers,
    ToolName.SPARK_DIAGCTL_HEALTH: _diagnostics,
    ToolName.RESET_REASON_REPORTER: _reset,
}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(value[key]) for key in sorted(value)})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value
