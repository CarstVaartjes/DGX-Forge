"""Fail-closed OCI rootfs execution behind the root package helper.

The backend deliberately does not speak to Docker/containerd sockets.  A
workload release contributes only an immutable, digest-verified OCI bundle
component.  The helper derives its path below the fixed generation root,
constructs the OCI process policy from the already validated backend request,
and invokes one pinned ``/usr/bin/runc`` binary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from vonk_agent_protocol import OciBundleMetadata

from ..package_helper_protocol import HelperProtocolError, HelperRequest
from .backends import Backend, BackendInvocation
from .sandbox import SandboxPolicy

_RUNC = Path("/usr/bin/runc")
_DIGEST_PREFIX = "sha256:"
_MAX_CONFIG_BYTES = 256 * 1024


class OciBackendError(HelperProtocolError):
    """The OCI runtime capability or bundle failed closed validation."""


class OciRuntimeRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> int: ...

    def cleanup(self, container_id: str) -> None: ...


class RuncCommandRunner:
    """Execute only the fixed runc executable with a scrubbed environment."""

    def __init__(self, runtime_root: Path, *, executable: Path = _RUNC) -> None:
        if Path(executable) != _RUNC:
            raise OciBackendError("OCI runtime executable is not the installed runc")
        root = Path(runtime_root)
        if not root.is_absolute() or root.is_symlink():
            raise OciBackendError("OCI runtime root is invalid")
        self._root = root
        self._executable = _RUNC

    def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> int:
        if not argv or argv[0] != str(self._executable):
            raise OciBackendError("OCI runtime command is not compiled")
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds < 1:
            raise OciBackendError("OCI runtime timeout is invalid")
        try:
            result = subprocess.run(
                argv,
                executable=str(self._executable),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd="/",
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                close_fds=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OciBackendError("OCI runtime launch failed") from error
        return result.returncode

    def cleanup(self, container_id: str) -> None:
        if not _container_id(container_id):
            raise OciBackendError("OCI container ID is invalid")
        argv = (
            str(self._executable),
            "--root",
            str(self._root),
            "delete",
            "--force",
            container_id,
        )
        try:
            subprocess.run(
                argv,
                executable=str(self._executable),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd="/",
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                close_fds=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OciBackendError("OCI runtime cleanup failed") from error


class OciRuntimeCapability:
    """Verify the separately-installed, digest-pinned runc capability."""

    def __init__(self, expected_digest: str, *, executable: Path = _RUNC) -> None:
        if Path(executable) != _RUNC:
            raise OciBackendError("OCI runtime executable is not fixed")
        if not isinstance(expected_digest, str) or len(expected_digest) != 64 or any(
            character not in "0123456789abcdef" for character in expected_digest
        ):
            raise OciBackendError("OCI runtime digest is invalid")
        self._expected = expected_digest
        self._executable = _RUNC

    @classmethod
    def from_file(
        cls,
        path: Path = Path("/etc/dgx-forge-agent/oci-runtime.sha256"),
        *,
        allow_unprivileged_test_file: bool = False,
    ) -> OciRuntimeCapability:
        """Load a root-owned digest pin, never an environment-controlled value."""

        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size != 65
                or info.st_mode & 0o022
                or (not allow_unprivileged_test_file and info.st_uid != 0)
            ):
                raise OciBackendError("OCI runtime digest file is unsafe")
            raw = os.read(descriptor, 66)
            if len(raw) != 65 or not raw.endswith(b"\n"):
                raise OciBackendError("OCI runtime digest file is invalid")
            expected = raw[:-1].decode("ascii")
            return cls(expected)
        except OciBackendError:
            raise
        except (OSError, UnicodeDecodeError) as error:
            raise OciBackendError("OCI runtime digest file is unavailable") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @property
    def executable(self) -> Path:
        return self._executable

    def verify(self) -> None:
        try:
            metadata = self._executable.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or not metadata.st_mode & 0o111
                or metadata.st_size < 1
            ):
                raise OciBackendError("installed OCI runtime is unsafe")
            digest = _hash_file(self._executable)
        except OciBackendError:
            raise
        except OSError as error:
            raise OciBackendError("installed OCI runtime is unavailable") from error
        if digest != self._expected:
            raise OciBackendError("installed OCI runtime digest is not authorized")


class OciBackendLauncher:
    """Validate one signed bundle and launch it through fixed runc."""

    def __init__(
        self,
        generations_root: Path,
        runtime_root: Path,
        *,
        capability: OciRuntimeCapability,
        objects_root: Path | None = None,
        runner: OciRuntimeRunner | None = None,
        clock=None,
    ) -> None:
        root = Path(generations_root)
        state = Path(runtime_root)
        if not root.is_absolute() or not state.is_absolute():
            raise OciBackendError("OCI backend roots must be absolute")
        if type(capability) is not OciRuntimeCapability:
            raise OciBackendError("OCI runtime capability is invalid")
        self._root = root
        self._runtime_root = state
        self._objects_root = Path(objects_root or root.parent / "objects" / "sha256")
        if not self._objects_root.is_absolute():
            raise OciBackendError("OCI object root is invalid")
        self._capability = capability
        self._runner = runner or RuncCommandRunner(state)
        if not callable(getattr(self._runner, "run", None)) or not callable(getattr(self._runner, "cleanup", None)):
            raise OciBackendError("OCI runtime runner is invalid")
        self._clock = clock or __import__("time").monotonic

    def launch(self, request: HelperRequest, sandbox: SandboxPolicy) -> Mapping[str, object]:
        if not isinstance(request, HelperRequest) or type(sandbox) is not SandboxPolicy:
            raise OciBackendError("OCI helper request is invalid")
        invocation = request.invocation
        if type(invocation) is not BackendInvocation or invocation.backend is not Backend.OCI:
            raise OciBackendError("OCI backend invocation is invalid")
        metadata = invocation.oci_bundle
        if type(metadata) is not OciBundleMetadata:
            raise OciBackendError("signed OCI bundle metadata is required")
        if invocation.network.mode != "none":
            raise OciBackendError("restricted OCI networking requires a reviewed boundary")
        self._capability.verify()
        absolute_deadline = self._clock() + invocation.resources.timeout_seconds
        self._check_deadline(absolute_deadline)
        component = self._component_root(invocation, metadata)
        self._verify_bundle(component, metadata, request)
        self._verify_mounts(invocation, request)
        self._check_deadline(absolute_deadline)
        generation_fd = _open_directory(component)
        executable_fd = _open_entrypoint(component, metadata)
        try:
            sandbox.plan(_oci_invocation(invocation), generation_fd, executable_fd)
        finally:
            os.close(executable_fd)
            os.close(generation_fd)
        self._runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _ensure_private_directory(self._runtime_root)
        container_id = f"dgx-oci-{request.request_id}"
        bundle = self._runtime_root / container_id
        if bundle.exists() or bundle.is_symlink():
            raise OciBackendError("OCI container bundle already exists")
        bundle.mkdir(mode=0o700)
        try:
            config = _runtime_config(
                invocation,
                metadata,
                component,
                sandbox,
                self._objects_root,
            )
            _write_immutable_json(bundle / "config.json", config)
            command = (
                str(self._capability.executable),
                "--root",
                str(self._runtime_root),
                "run",
                "--bundle",
                str(bundle),
                "--systemd-cgroup",
                container_id,
            )
            remaining = math.ceil(absolute_deadline - self._clock())
            if remaining <= 0:
                raise OciBackendError("OCI launch deadline elapsed")
            try:
                status = self._runner.run(command, timeout_seconds=remaining)
            except OciBackendError:
                self._runner.cleanup(container_id)
                raise
            if status != 0:
                self._runner.cleanup(container_id)
                raise OciBackendError("OCI runtime returned a failure")
            evidence = hashlib.sha256(
                json.dumps(
                    {
                        "config_digest": hashlib.sha256(_canonical(config)).hexdigest(),
                        "manifest_digest": metadata.manifest_digest,
                        "request_digest": request.digest,
                        "rootfs_digest": metadata.rootfs_digest,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            return {"status": "launched", "evidence_digest": evidence, "fence": request.fence}
        finally:
            shutil.rmtree(bundle, ignore_errors=True)

    def _component_root(self, invocation: BackendInvocation, metadata: OciBundleMetadata) -> Path:
        release = self._root / invocation.release_digest
        generation = release / invocation.generation
        component = generation / "components" / metadata.component
        _ensure_within(generation, component)
        return component

    def _verify_bundle(self, component: Path, metadata: OciBundleMetadata, request: HelperRequest) -> None:
        _ensure_private_directory(component)
        try:
            names = {item.name for item in component.iterdir()}
        except OSError as error:
            raise OciBackendError("OCI bundle component is unavailable") from error
        if names != {"oci-bundle.json", "oci-manifest.json", "config.json", metadata.rootfs}:
            raise OciBackendError("OCI bundle component layout is invalid")
        manifest = component / "oci-bundle.json"
        oci_manifest = component / "oci-manifest.json"
        config = component / "config.json"
        manifest_raw = _read_canonical_json(manifest)
        try:
            parsed = OciBundleMetadata.parse(manifest_raw)
        except Exception as error:
            raise OciBackendError("OCI bundle manifest is invalid") from error
        if parsed != metadata:
            raise OciBackendError("OCI bundle manifest is not signed and bound")
        image_manifest = _read_canonical_json(oci_manifest)
        if (
            not isinstance(image_manifest, dict)
            or image_manifest.get("schemaVersion") != 2
            or not isinstance(image_manifest.get("config"), dict)
            or image_manifest["config"].get("digest") != metadata.config_digest
            or not isinstance(image_manifest.get("layers"), list)
        ):
            raise OciBackendError("OCI image manifest is not an object")
        if _hash_bytes(_canonical(image_manifest)) != metadata.manifest_digest.removeprefix(_DIGEST_PREFIX):
            raise OciBackendError("OCI image manifest is not signed and bound")
        config_value = _read_canonical_json(config)
        if not isinstance(config_value, dict):
            raise OciBackendError("OCI bundle config is not an object")
        if _hash_bytes(_canonical(config_value)) != metadata.config_digest.removeprefix(_DIGEST_PREFIX):
            raise OciBackendError("OCI bundle config is not signed and bound")
        rootfs = component / metadata.rootfs
        _ensure_within(component, rootfs)
        _verify_rootfs(rootfs, metadata)
        receipts = {item.object_digest for item in request.receipts}
        if request.invocation.oci_bundle_digest not in receipts:
            raise OciBackendError("OCI bundle object has no signed receipt")

    def _verify_mounts(self, invocation: BackendInvocation, request: HelperRequest) -> None:
        receipts = {item.object_digest: item for item in request.receipts}
        for mount in invocation.mounts:
            receipt = receipts.get(mount.object_digest)
            if receipt is None:
                raise OciBackendError("OCI mount has no signed receipt")
            path = self._objects_root / mount.object_digest
            _ensure_within(self._objects_root, path)
            try:
                info = path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_mode & 0o022
                    or info.st_size != receipt.size
                    or _hash_file(path) != mount.object_digest
                ):
                    raise OciBackendError("OCI mount object is unsafe")
            except OciBackendError:
                raise
            except OSError as error:
                raise OciBackendError("OCI mount object is unavailable") from error

    def _check_deadline(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise OciBackendError("OCI launch deadline elapsed")


def _oci_invocation(invocation: BackendInvocation) -> BackendInvocation:
    """Return the invocation unchanged; this makes sandbox policy explicit."""

    return invocation


def _runtime_config(
    invocation: BackendInvocation,
    metadata: OciBundleMetadata,
    component: Path,
    sandbox: SandboxPolicy,
    objects_root: Path,
) -> dict[str, object]:
    resources = invocation.resources
    mounts = []
    for mount in invocation.mounts:
        mounts.append(
            {
                "destination": "/" + mount.target,
                "type": "bind",
                "source": str(objects_root / mount.object_digest),
                "options": ["rbind", "ro"],
            }
        )
    devices = []
    for device in invocation.devices:
        path = Path("/dev") / device
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as error:
            raise OciBackendError("declared OCI device is unavailable") from error
        if not (stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode)):
            raise OciBackendError("declared OCI device is not a device")
        devices.append(
            {
                "type": "c" if stat.S_ISCHR(info.st_mode) else "b",
                "path": "/dev/" + device,
                "major": os.major(info.st_rdev),
                "minor": os.minor(info.st_rdev),
                "fileMode": 0o660,
                "uid": 0,
                "gid": 0,
            }
        )
    return {
        "ociVersion": "1.0.2",
        "process": {
            "args": ["/" + metadata.entrypoint, *invocation.arguments],
            "cwd": "/",
            "terminal": False,
            "user": {"uid": sandbox.workload_uid, "gid": sandbox.workload_gid},
            "noNewPrivileges": True,
            "capabilities": {"bounding": [], "effective": [], "inheritable": [], "permitted": [], "ambient": []},
        },
        "root": {"path": str(component / metadata.rootfs), "readonly": True},
        "hostname": "dgx-workload",
        "mounts": mounts,
        "linux": {
            "namespaces": [
                {"type": "pid"},
                {"type": "mount"},
                {"type": "uts"},
                {"type": "ipc"},
                {"type": "network"},
            ],
            "resources": {
                "memory": {"limit": resources.memory_bytes},
                "cpu": {"quota": resources.cpu_millis * 1_000, "period": 1_000_000},
                "pids": {"limit": resources.pids_limit},
            },
            "devices": devices,
        },
    }


def _verify_rootfs(rootfs: Path, metadata: OciBundleMetadata) -> None:
    _ensure_private_directory(rootfs)
    entries: list[dict[str, object]] = []
    for path in sorted(rootfs.rglob("*")):
        info = path.stat(follow_symlinks=False)
        relative = path.relative_to(rootfs).as_posix()
        if stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
            raise OciBackendError("OCI rootfs contains an unsafe entry")
        if stat.S_ISDIR(info.st_mode):
            entries.append({"kind": "directory", "mode": stat.S_IMODE(info.st_mode), "path": relative})
        elif stat.S_ISREG(info.st_mode):
            entries.append({"digest": _hash_file(path), "kind": "file", "mode": stat.S_IMODE(info.st_mode), "path": relative, "size": info.st_size})
        else:
            raise OciBackendError("OCI rootfs contains a special entry")
    if _hash_bytes(_canonical(entries)) != metadata.rootfs_digest.removeprefix(_DIGEST_PREFIX):
        raise OciBackendError("OCI rootfs digest is not authorized")
    entrypoint = rootfs / metadata.entrypoint
    _ensure_within(rootfs, entrypoint)
    info = entrypoint.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise OciBackendError("OCI rootfs entrypoint is not executable")


def _read_canonical_json(path: Path) -> object:
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > _MAX_CONFIG_BYTES:
            raise OciBackendError("OCI metadata file is unsafe")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
        if raw != _canonical(value):
            raise OciBackendError("OCI metadata is not canonical")
        return value
    except OciBackendError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise OciBackendError("OCI metadata is unavailable") from error


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OciBackendError("OCI metadata has duplicate fields")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise OciBackendError("OCI metadata contains a nonfinite value")


def _write_immutable_json(path: Path, value: object) -> None:
    raw = _canonical(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise OciBackendError("OCI directory is unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or path.is_symlink():
        raise OciBackendError("OCI directory is unsafe")


def _ensure_within(root: Path, child: Path) -> None:
    try:
        child.relative_to(root)
    except ValueError as error:
        raise OciBackendError("OCI path escapes generation") from error
    current = root
    for part in child.relative_to(root).parts:
        current = current / part
        try:
            if current.is_symlink():
                raise OciBackendError("OCI path contains a symlink")
        except OSError as error:
            raise OciBackendError("OCI path is unavailable") from error


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise OciBackendError("OCI component directory is unavailable") from error


def _open_entrypoint(component: Path, metadata: OciBundleMetadata) -> int:
    path = component / metadata.rootfs / metadata.entrypoint
    _ensure_within(component, path)
    try:
        return os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise OciBackendError("OCI entrypoint is unavailable") from error


def _container_id(value: str) -> bool:
    return isinstance(value, str) and value.startswith("dgx-oci-") and len(value) <= 128 and all(
        character.isalnum() or character in "-" for character in value
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


__all__ = ["OciBackendError", "OciBackendLauncher", "OciRuntimeCapability", "RuncCommandRunner"]
