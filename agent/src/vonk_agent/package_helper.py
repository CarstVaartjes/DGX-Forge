"""Socket-facing authorization boundary for privileged package setup."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import socket
import sqlite3
import stat
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from vonk_agent_protocol.workload_packages import (
    SignedPackageObjectReceipt,
    package_helper_grant_signing_bytes,
    package_object_receipt_signing_bytes,
)

from .package_helper_protocol import (
    HelperProtocolError,
    HelperRequest,
    HelperResponse,
    SignedObjectReceipt,
    canonical_helper_document,
    frame_helper_message,
    receive_helper_message,
)
from .packages.backends import Backend, PythonRuntimePolicy
from .packages.oci_backend import OciBackendLauncher, OciRuntimeCapability
from .packages.sandbox import SandboxPolicy

MAX_REPLAY_ENTRIES = 65_536
MAX_SUPERVISOR_STATE_BYTES = 64 * 1024
OCI_RUNTIME_DIGEST_PATH = Path("/etc/dgx-forge-agent/oci-runtime.sha256")


class ReceiptVerifier(Protocol):
    def verify(self, receipt: SignedObjectReceipt) -> bool: ...


class FenceAuthorizer(Protocol):
    def authorize(self, request: HelperRequest, request_digest: str) -> bool: ...


class BackendLauncher(Protocol):
    def launch(
        self, request: HelperRequest, sandbox: SandboxPolicy
    ) -> Mapping[str, object]: ...


class ActiveSlotBoundary(Protocol):
    def verify(self) -> None: ...


class ActiveSlotVerifier:
    """Revalidate the running helper against root-owned active-slot state."""

    def __init__(
        self,
        expected_sha256: str,
        state_path: Path = Path(
            "/var/lib/dgx-forge-agent-supervisor/state.json"
        ),
        *,
        allow_unprivileged_test_file: bool = False,
    ) -> None:
        if (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise HelperProtocolError("package helper slot digest is invalid")
        path = Path(state_path)
        if not path.is_absolute():
            raise HelperProtocolError("supervisor state path is invalid")
        self._expected_sha256 = expected_sha256
        self._state_path = path
        self._owner_uid = os.geteuid() if allow_unprivileged_test_file else 0

    def verify(self) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                self._state_path,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != self._owner_uid
                or stat.S_IMODE(before.st_mode) != 0o644
                or not 1 <= before.st_size <= MAX_SUPERVISOR_STATE_BYTES
            ):
                raise HelperProtocolError("supervisor state file is unsafe")
            raw = os.read(descriptor, MAX_SUPERVISOR_STATE_BYTES + 1)
            after = os.fstat(descriptor)
            identity = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            if len(raw) > MAX_SUPERVISOR_STATE_BYTES or identity(before) != identity(
                after
            ):
                raise HelperProtocolError("supervisor state changed while read")
        except HelperProtocolError:
            raise
        except OSError as error:
            raise HelperProtocolError("supervisor state is unavailable") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            document = json.loads(
                raw.decode("ascii"),
                object_pairs_hook=_unique_state,
                parse_constant=_reject_state_constant,
            )
        except HelperProtocolError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise HelperProtocolError("supervisor state is invalid") from error
        canonical = (
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
        expected_fields = {
            "activation_deadline",
            "active_slot",
            "boot_attempts",
            "expected_sha256",
            "generation",
            "previous_slot",
            "rollback_performed",
            "schema_version",
            "slot_sha256",
            "status",
        }
        active = document.get("active_slot") if isinstance(document, dict) else None
        slots = document.get("slot_sha256") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or set(document) != expected_fields
            or canonical != raw
            or document.get("schema_version") != 1
            or active not in {"A", "B"}
            or not isinstance(slots, dict)
            or set(slots) != {"A", "B"}
            or slots.get(active) != document.get("expected_sha256")
            or document.get("expected_sha256") != self._expected_sha256
            or document.get("status") not in {"stable", "pending"}
        ):
            raise HelperProtocolError("supervisor state active slot is stale")


def _unique_state(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HelperProtocolError("supervisor state contains duplicate fields")
        result[key] = value
    return result


def _reject_state_constant(_value: str) -> None:
    raise HelperProtocolError("supervisor state contains a nonfinite number")


class SystemdCommandRunner:
    """Run only the compiled systemd transient-service command without output."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        pass_fds: tuple[int, ...],
        timeout_seconds: int,
    ) -> int:
        if not argv or argv[0] != "/usr/bin/systemd-run":
            raise HelperProtocolError("package helper command is not compiled")
        try:
            completed = subprocess.run(
                argv,
                executable="/usr/bin/systemd-run",
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd="/",
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                close_fds=True,
                pass_fds=pass_fds,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HelperProtocolError("package backend launch failed") from error
        return completed.returncode

    def cleanup(self, unit_name: str) -> None:
        if not unit_name.startswith("dgx-workload-") or not unit_name.endswith(
            ".service"
        ):
            raise HelperProtocolError("package backend unit name is invalid")
        fixed_environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        for action in ("stop", "reset-failed"):
            try:
                subprocess.run(
                    ("/usr/bin/systemctl", action, unit_name),
                    executable="/usr/bin/systemctl",
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd="/",
                    env=fixed_environment,
                    close_fds=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise HelperProtocolError(
                    "package backend cleanup did not complete"
                ) from error


class SystemdBackendLauncher:
    """Launch a digest-matched adapter in a fixed transient systemd sandbox."""

    def __init__(
        self,
        generations_root: Path,
        *,
        objects_root: Path | None = None,
        runner=None,
        clock: Callable[[], float] = time.monotonic,
        oci_runtime_digest: str | None = None,
    ) -> None:
        root = Path(generations_root)
        if not root.is_absolute():
            raise HelperProtocolError("generation root is invalid")
        self._root = root
        self._objects_root = Path(objects_root or root.parent / "objects" / "sha256")
        if not self._objects_root.is_absolute():
            raise HelperProtocolError("package object root is invalid")
        self._runner = runner or SystemdCommandRunner()
        if not callable(clock):
            raise HelperProtocolError("package launcher clock is invalid")
        self._clock = clock
        self._oci_launcher = None
        runtime_digest = oci_runtime_digest or _installed_oci_runtime_digest()
        if runtime_digest is not None:
            try:
                self._oci_launcher = OciBackendLauncher(
                    self._root,
                    self._root.parent / "oci-runtime",
                    capability=OciRuntimeCapability(runtime_digest),
                    objects_root=self._objects_root,
                    clock=self._clock,
                )
            except HelperProtocolError:
                # A malformed optional capability must never make the native
                # helper unavailable; OCI requests remain fail-closed.
                self._oci_launcher = None
        if not callable(getattr(self._runner, "run", None)) or not callable(
            getattr(self._runner, "cleanup", None)
        ):
            raise HelperProtocolError("systemd command boundary is invalid")

    def launch(
        self, request: HelperRequest, sandbox: SandboxPolicy
    ) -> Mapping[str, object]:
        generation_fd = -1
        executable_fd = -1
        interpreter_fd = -1
        mount_fds: list[int] = []
        try:
            if request.invocation.backend is Backend.OCI:
                if self._oci_launcher is None:
                    raise HelperProtocolError(
                        "OCI runtime capability is not installed"
                    )
                return self._oci_launcher.launch(request, sandbox)
            if request.invocation.backend not in {
                Backend.NATIVE,
                Backend.PYTHON_VENV,
            }:
                raise HelperProtocolError(
                    "package backend is not implemented by the systemd launcher"
                )
            if request.invocation.network.mode != "none":
                raise HelperProtocolError(
                    "restricted network requires an installed network-policy boundary"
                )
            absolute_deadline = (
                self._clock() + request.invocation.resources.timeout_seconds
            )
            _helper_deadline(self._clock, absolute_deadline)
            generation_fd, executable_fd = _open_backend_content(
                self._root,
                request,
                clock=self._clock,
                absolute_deadline=absolute_deadline,
            )
            if request.invocation.backend is Backend.PYTHON_VENV:
                runtime = request.invocation.python_runtime
                if type(runtime) is not PythonRuntimePolicy:
                    raise HelperProtocolError("Python runtime policy is invalid")
                _validate_python_environment(
                    generation_fd,
                    runtime.environment_component,
                    request,
                    clock=self._clock,
                    absolute_deadline=absolute_deadline,
                )
                interpreter_fd = _open_python_interpreter(
                    self._root,
                    request,
                    runtime,
                    clock=self._clock,
                    absolute_deadline=absolute_deadline,
                )
            plan = sandbox.plan(request.invocation, generation_fd, executable_fd)
            try:
                os.fchown(executable_fd, plan.uid, plan.gid)
                os.fchmod(executable_fd, 0o500)
                if interpreter_fd >= 0:
                    os.fchown(interpreter_fd, plan.uid, plan.gid)
                    os.fchmod(interpreter_fd, 0o500)
            except OSError as error:
                raise HelperProtocolError(
                    "backend snapshot identity could not be applied"
                ) from error
            helper_pid = os.getpid()
            source_executable = f"/proc/{helper_pid}/fd/{executable_fd}"
            source_generation = f"/proc/{helper_pid}/fd/{generation_fd}"
            source_interpreter = (
                f"/proc/{helper_pid}/fd/{interpreter_fd}"
                if interpreter_fd >= 0
                else None
            )
            resources = request.invocation.resources
            unit_name = f"dgx-workload-{request.request_id}.service"
            argv = [
                "/usr/bin/systemd-run",
                "--quiet",
                "--wait",
                "--pipe",
                "--collect",
                "--service-type=exec",
                f"--unit={unit_name}",
                f"--uid={plan.uid}",
                f"--gid={plan.gid}",
                "--property=NoNewPrivileges=yes",
                "--property=CapabilityBoundingSet=",
                "--property=AmbientCapabilities=",
                "--property=DevicePolicy=closed",
                "--property=PrivateMounts=yes",
                "--property=ProtectSystem=strict",
                "--property=ProtectHome=yes",
                "--property=RestrictSUIDSGID=yes",
                f"--property=MemoryMax={resources.memory_bytes}",
                f"--property=TasksMax={resources.pids_limit}",
                f"--property=CPUQuota={resources.cpu_millis / 10:.1f}%",
                f"--property=RuntimeMaxSec={resources.timeout_seconds}",
                "--property=TimeoutStopSec=10",
                f"--property=BindReadOnlyPaths={source_executable}:/run/dgx-forge/entrypoint",
                f"--property=BindReadOnlyPaths={source_generation}:/run/dgx-forge/generation",
                "--working-directory=/run/dgx-forge/generation",
            ]
            argv.append("--property=PrivateNetwork=yes")
            if interpreter_fd >= 0:
                runtime = request.invocation.python_runtime
                assert runtime is not None
                environment_root = (
                    f"/run/dgx-forge/generation/components/"
                    f"{runtime.environment_component}"
                )
                assert source_interpreter is not None
                argv.extend(
                    (
                        f"--property=BindReadOnlyPaths={source_interpreter}:/run/dgx-forge/interpreter",
                        f"--property=WorkingDirectory={environment_root}",
                        "--setenv=PYTHONNOUSERSITE=1",
                        f"--setenv=VIRTUAL_ENV={environment_root}",
                        f"--setenv=PATH={environment_root}/bin:/usr/bin:/bin",
                        f"--setenv=PYTHONPATH={environment_root}/lib/python/site-packages",
                    )
                )
            for mount in request.invocation.mounts:
                mount_fd = _open_mount_object(
                    self._objects_root,
                    mount.object_digest,
                    request,
                    clock=self._clock,
                    absolute_deadline=absolute_deadline,
                )
                mount_fds.append(mount_fd)
                source = f"/proc/{helper_pid}/fd/{mount_fd}"
                target = f"/run/dgx-forge/generation/{mount.target}"
                argv.append(f"--property=BindReadOnlyPaths={source}:{target}")
            argv.extend(
                f"--property=DeviceAllow=/dev/{device} rw"
                for device in request.invocation.devices
            )
            command = (
                ("/run/dgx-forge/interpreter", "/run/dgx-forge/entrypoint")
                if interpreter_fd >= 0
                else ("/run/dgx-forge/entrypoint",)
            )
            argv.extend(("--", *command, *request.invocation.arguments))
            fixed_argv = tuple(argv)
            remaining_seconds = math.ceil(absolute_deadline - self._clock())
            if remaining_seconds <= 0:
                raise HelperProtocolError("package helper launch deadline elapsed")
            try:
                returncode = self._runner.run(
                fixed_argv,
                    pass_fds=(
                        generation_fd,
                        executable_fd,
                        *((interpreter_fd,) if interpreter_fd >= 0 else ()),
                        *mount_fds,
                    ),
                    timeout_seconds=remaining_seconds,
                )
            except HelperProtocolError:
                self._runner.cleanup(unit_name)
                raise
            if returncode != 0:
                self._runner.cleanup(unit_name)
                raise HelperProtocolError("package backend launch failed")
            evidence = hashlib.sha256(
                canonical_helper_document(
                    {
                        "request_digest": request.digest,
                        "launch_digest": hashlib.sha256(
                            "\0".join(fixed_argv).encode("utf-8")
                        ).hexdigest(),
                    }
                )
            ).hexdigest()
            return {
                "status": "launched",
                "evidence_digest": evidence,
                "fence": request.fence,
            }
        finally:
            if interpreter_fd >= 0:
                os.close(interpreter_fd)
            if executable_fd >= 0:
                os.close(executable_fd)
            if generation_fd >= 0:
                os.close(generation_fd)
            for mount_fd in mount_fds:
                os.close(mount_fd)


def _installed_oci_runtime_digest() -> str | None:
    """Read the optional root-owned OCI capability pin, never process env."""

    descriptor = -1
    try:
        descriptor = os.open(
            OCI_RUNTIME_DIGEST_PATH,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or not 65 <= metadata.st_size <= 66
        ):
            return None
        raw = os.read(descriptor, 67)
        value = raw.decode("ascii").strip()
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            return None
        return value
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_mount_object(
    objects_root: Path,
    object_digest: str,
    request: HelperRequest,
    *,
    clock: Callable[[], float],
    absolute_deadline: float,
) -> int:
    root_fd = -1
    descriptor = -1
    try:
        _helper_deadline(clock, absolute_deadline)
        receipt = next(
            (item for item in request.receipts if item.object_digest == object_digest),
            None,
        )
        if receipt is None:
            raise HelperProtocolError("mount has no signed receipt")
        root_fd = os.open(
            objects_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        descriptor = os.open(
            object_digest,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or before.st_size != receipt.size
        ):
            raise HelperProtocolError("mount object is unsafe")
        digest = hashlib.sha256()
        while True:
            _helper_deadline(clock, absolute_deadline)
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        _helper_deadline(clock, absolute_deadline)
        if digest.hexdigest() != object_digest or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise HelperProtocolError("mount object digest is invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        result, descriptor = descriptor, -1
        return result
    except HelperProtocolError:
        raise
    except OSError as error:
        raise HelperProtocolError("mount object is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if root_fd >= 0:
            os.close(root_fd)


def _open_backend_content(
    generations_root: Path,
    request: HelperRequest,
    *,
    clock: Callable[[], float],
    absolute_deadline: float,
) -> tuple[int, int]:
    root_fd = -1
    generation_fd = -1
    source_fd = -1
    snapshot_fd = -1
    directory_fd = -1
    try:
        _helper_deadline(clock, absolute_deadline)
        root_fd = os.open(
            generations_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        release_fd = os.open(
            request.invocation.release_digest,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            generation_fd = os.open(
                request.invocation.generation,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=release_fd,
            )
        finally:
            os.close(release_fd)
        directory_fd = os.dup(generation_fd)
        parts = PurePosixPath(request.invocation.entrypoint).parts
        for part in parts[:-1]:
            _helper_deadline(clock, absolute_deadline)
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child
        source_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or not before.st_mode & 0o100
            or not 1 <= before.st_size <= 256 * 1024 * 1024
        ):
            raise HelperProtocolError("backend entrypoint is unsafe")
        snapshot_fd = os.memfd_create(
            "dgx-package-entrypoint", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        os.fchmod(snapshot_fd, 0o500)
        digest = hashlib.sha256()
        while True:
            _helper_deadline(clock, absolute_deadline)
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot_fd, view)
                if written <= 0:
                    raise HelperProtocolError("backend snapshot is incomplete")
                view = view[written:]
        after = os.fstat(source_fd)
        _helper_deadline(clock, absolute_deadline)
        exact_receipt = next(
            (
                receipt
                for receipt in request.receipts
                if receipt.object_digest == digest.hexdigest()
                and receipt.size == before.st_size
            ),
            None,
        )
        if exact_receipt is None or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise HelperProtocolError("backend entrypoint has no signed receipt")
        fcntl.fcntl(
            snapshot_fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE,
        )
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        result = (generation_fd, snapshot_fd)
        generation_fd = snapshot_fd = -1
        return result
    except HelperProtocolError:
        raise
    except OSError as error:
        raise HelperProtocolError("backend content is unavailable") from error
    finally:
        for descriptor in (
            directory_fd,
            source_fd,
            generation_fd,
            snapshot_fd,
            root_fd,
        ):
            if descriptor >= 0:
                os.close(descriptor)


def _validate_python_environment(
    generation_fd: int,
    component: str,
    request: HelperRequest,
    *,
    clock: Callable[[], float],
    absolute_deadline: float,
) -> None:
    """Require the signed Python environment component to be a real directory."""

    descriptor = -1
    components_fd = -1
    receipt_fd = -1
    try:
        _helper_deadline(clock, absolute_deadline)
        receipt_fd = os.open(
            ".dgx-generation.json",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=generation_fd,
        )
        receipt_metadata = os.fstat(receipt_fd)
        if (
            not stat.S_ISREG(receipt_metadata.st_mode)
            or receipt_metadata.st_nlink != 1
            or receipt_metadata.st_size > 1024 * 1024
            or receipt_metadata.st_mode & 0o022
        ):
            raise HelperProtocolError("Python generation receipt is unsafe")
        raw_receipt = os.read(receipt_fd, receipt_metadata.st_size + 1)
        try:
            receipt = json.loads(raw_receipt.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise HelperProtocolError("Python generation receipt is invalid") from error
        if (
            not isinstance(receipt, dict)
            or canonical_helper_document(receipt) + b"\n" != raw_receipt
            or receipt.get("release_digest") != request.invocation.release_digest
            or receipt.get("environment_digest")
            != request.invocation.python_runtime.environment_digest
        ):
            raise HelperProtocolError("Python generation receipt is unbound")
        files = receipt.get("files")
        if not isinstance(files, list) or _generation_tree_digest(generation_fd, clock, absolute_deadline) != files:
            raise HelperProtocolError("Python generation tree is inconsistent")
        root_digest = hashlib.sha256(
            json.dumps(
                files,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if receipt.get("root_object_digest") != root_digest:
            raise HelperProtocolError("Python generation tree digest is invalid")
        prefix = f"components/{component}/"
        environment_files = [
            {
                **item,
                "path": item["path"][len(prefix) :],
            }
            for item in files
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and item["path"].startswith(prefix)
        ]
        environment_tree_digest = hashlib.sha256(
            json.dumps(
                environment_files,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        ).hexdigest()
        if (
            environment_tree_digest
            != request.invocation.python_runtime.environment_tree_digest
        ):
            raise HelperProtocolError("Python environment tree digest is not authorized")
        components_fd = os.open(
            "components",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=generation_fd,
        )
        descriptor = os.open(
            component,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=components_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise HelperProtocolError("Python environment component is unsafe")
    except HelperProtocolError:
        raise
    except OSError as error:
        raise HelperProtocolError("Python environment component is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if components_fd >= 0:
            os.close(components_fd)
        if receipt_fd >= 0:
            os.close(receipt_fd)


def _generation_tree_digest(
    generation_fd: int,
    clock: Callable[[], float],
    absolute_deadline: float,
) -> list[dict[str, object]]:
    """Recompute the immutable materializer manifest without following links."""

    result: list[dict[str, object]] = []

    def walk(directory_fd: int, prefix: str) -> None:
        try:
            names = sorted(os.listdir(f"/proc/{os.getpid()}/fd/{directory_fd}"))
        except OSError as error:
            raise HelperProtocolError("Python generation tree is unavailable") from error
        if len(result) + len(names) > 100_000:
            raise HelperProtocolError("Python generation tree is too large")
        for name in names:
            _helper_deadline(clock, absolute_deadline)
            relative = f"{prefix}/{name}" if prefix else name
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                result.append({"kind": "directory", "mode": mode, "path": relative})
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    digest = hashlib.sha256()
                    while True:
                        _helper_deadline(clock, absolute_deadline)
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                    after = os.fstat(descriptor)
                    if (
                        after.st_dev != metadata.st_dev
                        or after.st_ino != metadata.st_ino
                        or after.st_size != metadata.st_size
                        or after.st_mtime_ns != metadata.st_mtime_ns
                        or after.st_ctime_ns != metadata.st_ctime_ns
                    ):
                        raise HelperProtocolError("Python generation changed while read")
                finally:
                    os.close(descriptor)
                result.append(
                    {
                        "digest": digest.hexdigest(),
                        "kind": "file",
                        "mode": mode,
                        "path": relative,
                        "size": metadata.st_size,
                    }
                )
            else:
                raise HelperProtocolError("Python generation contains a special file")

    walk(generation_fd, "")
    return [item for item in result if item["path"] != ".dgx-generation.json"]


def _open_python_interpreter(
    generations_root: Path,
    request: HelperRequest,
    runtime: PythonRuntimePolicy,
    *,
    clock: Callable[[], float],
    absolute_deadline: float,
) -> int:
    """Open and seal the interpreter named by the signed Python policy."""

    relative = PurePosixPath(
        "components",
        runtime.interpreter_component,
        runtime.interpreter_entrypoint,
    )
    descriptor = _open_generation_file(
        generations_root,
        request,
        relative,
        expected_digest=runtime.interpreter_digest,
        clock=clock,
        absolute_deadline=absolute_deadline,
        label="Python interpreter",
        require_receipt=False,
    )
    return descriptor


def _open_generation_file(
    generations_root: Path,
    request: HelperRequest,
    relative: PurePosixPath,
    *,
    expected_digest: str,
    clock: Callable[[], float],
    absolute_deadline: float,
    label: str,
    require_receipt: bool = True,
) -> int:
    """Snapshot one regular generation file and bind it to a signed receipt."""

    root_fd = -1
    generation_fd = -1
    directory_fd = -1
    source_fd = -1
    snapshot_fd = -1
    try:
        _helper_deadline(clock, absolute_deadline)
        root_fd = os.open(
            generations_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        release_fd = os.open(
            request.invocation.release_digest,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            generation_fd = os.open(
                request.invocation.generation,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=release_fd,
            )
        finally:
            os.close(release_fd)
        directory_fd = os.dup(generation_fd)
        for part in relative.parts[:-1]:
            _helper_deadline(clock, absolute_deadline)
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child
        source_fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or not before.st_mode & 0o100
            or not 1 <= before.st_size <= 256 * 1024 * 1024
        ):
            raise HelperProtocolError(f"{label} is unsafe")
        snapshot_fd = os.memfd_create(
            "dgx-package-interpreter",
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        os.fchmod(snapshot_fd, 0o500)
        digest = hashlib.sha256()
        while True:
            _helper_deadline(clock, absolute_deadline)
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot_fd, view)
                if written <= 0:
                    raise HelperProtocolError(f"{label} snapshot is incomplete")
                view = view[written:]
        after = os.fstat(source_fd)
        exact_receipt = next(
            (
                receipt
                for receipt in request.receipts
                if receipt.object_digest == digest.hexdigest()
                and receipt.object_digest == expected_digest
                and receipt.size == before.st_size
            ),
            None,
        )
        if (require_receipt and exact_receipt is None) or digest.hexdigest() != expected_digest or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise HelperProtocolError(f"{label} has no signed receipt")
        fcntl.fcntl(
            snapshot_fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE,
        )
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        result, snapshot_fd = snapshot_fd, -1
        return result
    except HelperProtocolError:
        raise
    except OSError as error:
        raise HelperProtocolError(f"{label} is unavailable") from error
    finally:
        for descriptor in (
            directory_fd,
            source_fd,
            generation_fd,
            snapshot_fd,
            root_fd,
        ):
            if descriptor >= 0:
                os.close(descriptor)


def _helper_deadline(clock: Callable[[], float], absolute_deadline: float) -> None:
    try:
        current = clock()
    except Exception as error:
        raise HelperProtocolError("package helper clock failed") from error
    if (
        not isinstance(current, (int, float))
        or isinstance(current, bool)
        or not math.isfinite(current)
        or current >= absolute_deadline
    ):
        raise HelperProtocolError("package helper launch deadline elapsed")


class Ed25519ReceiptVerifier:
    """Verify canonical object receipts with one installed public key."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise HelperProtocolError("receipt public key is invalid")
        self._public_key = public_key
        self._key_id = hashlib.sha256(
            public_key.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).hexdigest()

    @property
    def public_key_bytes(self) -> bytes:
        """Return the raw public key for startup role-separation checks."""
        return self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @classmethod
    def from_file(
        cls, path: Path, *, allow_unprivileged_test_file: bool = False
    ) -> Ed25519ReceiptVerifier:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > 4096
                or before.st_mode & 0o022
                or (
                    not allow_unprivileged_test_file
                    and before.st_uid not in {0, os.geteuid()}
                )
            ):
                raise HelperProtocolError("receipt public key file is unsafe")
            raw = os.read(descriptor, 4097)
            after = os.fstat(descriptor)
            if len(raw) > 4096 or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise HelperProtocolError("receipt public key changed")
            public_key = serialization.load_pem_public_key(raw)
            if not isinstance(public_key, Ed25519PublicKey):
                raise HelperProtocolError("receipt public key is not Ed25519")
            return cls(public_key)
        except HelperProtocolError:
            raise
        except (OSError, ValueError) as error:
            raise HelperProtocolError("receipt public key is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def verify(self, receipt: SignedObjectReceipt) -> bool:
        if type(receipt) is not SignedPackageObjectReceipt:
            return False
        try:
            if receipt.signature.key_id != self._key_id:
                return False
            signature = bytes.fromhex(receipt.signature.value)
            self._public_key.verify(
                signature, package_object_receipt_signing_bytes(receipt.claims)
            )
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False


class SignedFenceAuthorizer:
    """Verify a control-signed operation grant and durably consume it once."""

    def __init__(
        self,
        public_key: Ed25519PublicKey,
        replay_database: Path,
        *,
        allow_unprivileged_test_files: bool = False,
        clock: Callable[[], datetime] | None = None,
        max_entries: int = MAX_REPLAY_ENTRIES,
    ) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise HelperProtocolError("fence public key is invalid")
        self._public_key = public_key
        self._key_id = hashlib.sha256(
            public_key.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).hexdigest()
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or not 1 <= max_entries <= MAX_REPLAY_ENTRIES
        ):
            raise HelperProtocolError("helper replay capacity is invalid")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_entries = max_entries
        self._replay_database = _safe_replay_database(
            replay_database,
            allow_unprivileged_test_files=allow_unprivileged_test_files,
        )
        self._lock = threading.Lock()
        try:
            with sqlite3.connect(self._replay_database) as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS consumed_fences ("
                    "request_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, "
                    "attempt INTEGER NOT NULL, fence TEXT NOT NULL, "
                    "request_digest TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                    "UNIQUE(operation_id, attempt, fence))"
                )
        except sqlite3.DatabaseError as error:
            raise HelperProtocolError("helper replay database is invalid") from error

    @property
    def public_key_bytes(self) -> bytes:
        """Return the raw public key for startup role-separation checks."""
        return self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @classmethod
    def from_file(
        cls,
        public_key_path: Path,
        replay_database: Path,
        *,
        allow_unprivileged_test_files: bool = False,
        clock: Callable[[], datetime] | None = None,
        max_entries: int = MAX_REPLAY_ENTRIES,
    ) -> SignedFenceAuthorizer:
        verifier = Ed25519ReceiptVerifier.from_file(
            public_key_path,
            allow_unprivileged_test_file=allow_unprivileged_test_files,
        )
        return cls(
            verifier._public_key,
            replay_database,
            allow_unprivileged_test_files=allow_unprivileged_test_files,
            clock=clock,
            max_entries=max_entries,
        )

    def authorize(self, request: HelperRequest, request_digest: str) -> bool:
        if type(request) is not HelperRequest or request_digest != request.digest:
            return False
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise HelperProtocolError("helper authority clock is invalid")
        now = now.astimezone(UTC)
        claims = request.grant.claims
        expires_at = datetime.fromtimestamp(claims.expires_at, tz=UTC)
        issued_at = datetime.fromtimestamp(claims.issued_at, tz=UTC)
        if (
            expires_at <= now
            or issued_at > now
            or expires_at > issued_at + timedelta(minutes=15)
            or request.grant.signature.key_id != self._key_id
        ):
            return False
        try:
            signature = bytes.fromhex(request.grant.signature.value)
            self._public_key.verify(
                signature, package_helper_grant_signing_bytes(claims)
            )
        except (InvalidSignature, ValueError, TypeError):
            return False
        with self._lock:
            try:
                with sqlite3.connect(
                    self._replay_database, isolation_level=None
                ) as replay:
                    replay.execute("BEGIN IMMEDIATE")
                    replay.execute(
                        "DELETE FROM consumed_fences WHERE expires_at <= ?",
                        (int(now.timestamp()),),
                    )
                    count = replay.execute(
                        "SELECT COUNT(*) FROM consumed_fences"
                    ).fetchone()
                    if count is None or count[0] >= self._max_entries:
                        replay.execute("ROLLBACK")
                        raise HelperProtocolError(
                            "package helper replay capacity is exhausted"
                        )
                    cursor = replay.execute(
                        "INSERT OR IGNORE INTO consumed_fences "
                        "(request_id, operation_id, attempt, fence, request_digest, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            request.request_id,
                            request.operation_id,
                            request.attempt,
                            request.fence,
                            request_digest,
                            int(expires_at.timestamp()),
                        ),
                    )
                    replay.execute("COMMIT")
                    return cursor.rowcount == 1
            except sqlite3.DatabaseError as error:
                raise HelperProtocolError(
                    "package helper fence state is unavailable"
                ) from error


def _safe_replay_database(path: Path, *, allow_unprivileged_test_files: bool) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise HelperProtocolError("helper replay database path is invalid")
    expected_uid = os.geteuid() if allow_unprivileged_test_files else 0
    try:
        parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != expected_uid
            or parent.st_mode & 0o022
        ):
            raise HelperProtocolError("helper replay directory is unsafe")
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != expected_uid
                or metadata.st_mode & 0o177 != 0
            ):
                raise HelperProtocolError("helper replay database is unsafe")
        finally:
            os.close(descriptor)
    except HelperProtocolError:
        raise
    except OSError as error:
        raise HelperProtocolError("helper replay database is unavailable") from error
    return path


class PackageHelper:
    """Validate one already-authenticated package-engine launch request."""

    def __init__(
        self,
        *,
        agent_uid: int,
        sandbox: SandboxPolicy,
        active_slot_verifier: ActiveSlotBoundary,
        receipt_verifier: ReceiptVerifier,
        fence_authorizer: FenceAuthorizer,
        launcher: BackendLauncher,
    ) -> None:
        if (
            not isinstance(agent_uid, int)
            or isinstance(agent_uid, bool)
            or not 1 <= agent_uid <= 2**31 - 1
            or agent_uid == sandbox.workload_uid
        ):
            raise HelperProtocolError("agent peer UID is invalid")
        for boundary, method, name in (
            (active_slot_verifier, "verify", "active slot verifier"),
            (receipt_verifier, "verify", "receipt verifier"),
            (fence_authorizer, "authorize", "fence authorizer"),
            (launcher, "launch", "backend launcher"),
        ):
            if not callable(getattr(boundary, method, None)):
                raise HelperProtocolError(f"{name} is invalid")
        self._agent_uid = agent_uid
        self._sandbox = sandbox
        self._active_slot_verifier = active_slot_verifier
        self._receipt_verifier = receipt_verifier
        self._fence_authorizer = fence_authorizer
        self._launcher = launcher
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def handle(self, peer_uid: int, raw: bytes) -> bytes:
        if type(peer_uid) is not int or peer_uid != self._agent_uid:
            raise HelperProtocolError("package helper peer is not the agent")
        self._active_slot_verifier.verify()
        request = HelperRequest.parse(raw)
        key = (request.request_id, request.fence)
        with self._lock:
            if key in self._seen:
                raise HelperProtocolError("package helper request replay was rejected")
            if len(self._seen) >= MAX_REPLAY_ENTRIES:
                raise HelperProtocolError("package helper replay state is full")
            if not self._fence_authorizer.authorize(request, request.digest):
                raise HelperProtocolError("package helper operation fence is stale")
            for receipt in request.receipts:
                if not self._receipt_verifier.verify(receipt):
                    raise HelperProtocolError(
                        "package helper object receipt is invalid"
                    )
            # Consume before the side effect. A failed launch remains ambiguous and may
            # only be retried under a new authorized request/fence.
            self._seen.add(key)
        try:
            outcome = self._launcher.launch(request, self._sandbox)
        except HelperProtocolError:
            raise
        except Exception as error:
            raise HelperProtocolError("package helper backend launch failed") from error
        if not isinstance(outcome, Mapping) or set(outcome) != {
            "status",
            "evidence_digest",
            "fence",
        }:
            raise HelperProtocolError("package helper result fields are invalid")
        if outcome["fence"] != request.fence:
            raise HelperProtocolError("package helper result fence does not match")
        try:
            response = HelperResponse(
                1,
                request.request_id,
                outcome["status"],
                outcome["evidence_digest"],
                outcome["fence"],
                request.digest,
            )
        except (TypeError, ValueError) as error:
            raise HelperProtocolError("package helper result is invalid") from error
        return response.to_bytes()


def unix_peer_uid(connection: socket.socket) -> int:
    """Return the kernel-authenticated UID for one Unix stream connection."""
    if connection.family != socket.AF_UNIX:
        raise HelperProtocolError("package helper requires a Unix socket")
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as error:
        raise HelperProtocolError(
            "package helper peer credentials are unavailable"
        ) from error
    if uid < 0:
        raise HelperProtocolError("package helper peer credentials are invalid")
    return uid


def serve_connection(
    helper: PackageHelper,
    connection: socket.socket,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Serve one length-framed connection within an absolute socket deadline."""
    peer_uid = unix_peer_uid(connection)
    request = receive_helper_message(connection, timeout_seconds=timeout_seconds)
    response = helper.handle(peer_uid, request)
    previous = connection.gettimeout()
    connection.settimeout(timeout_seconds)
    try:
        connection.sendall(frame_helper_message(response))
    except OSError as error:
        raise HelperProtocolError("package helper response deadline elapsed") from error
    finally:
        connection.settimeout(previous)


def serve_listener(
    helper: PackageHelper,
    listener: socket.socket,
    *,
    connection_timeout_seconds: float = 5.0,
) -> None:
    """Serve a persistent systemd-owned Unix listener, isolating bad peers."""
    if listener.family != socket.AF_UNIX or listener.type & socket.SOCK_STREAM == 0:
        raise HelperProtocolError("package helper listener is invalid")
    while True:
        connection, _ = listener.accept()
        try:
            serve_connection(
                helper,
                connection,
                timeout_seconds=connection_timeout_seconds,
            )
        except HelperProtocolError:
            # A malformed or unauthorized connection must not terminate the
            # persistent authority process or expose diagnostic details.
            pass
        finally:
            connection.close()


def main(argv: list[str] | None = None) -> int:
    """Run the helper only when launched by systemd socket activation."""
    import argparse

    parser = argparse.ArgumentParser(prog="dgx-package-helper")
    parser.add_argument("--listen-fd", type=int, default=3)
    args = parser.parse_args(argv)
    if (
        os.environ.get("LISTEN_PID") != str(os.getpid())
        or os.environ.get("LISTEN_FDS") != "1"
    ):
        raise HelperProtocolError("systemd socket activation is required")
    if args.listen_fd != 3:
        raise HelperProtocolError("listener FD is invalid")
    if os.geteuid() != 0:
        raise HelperProtocolError("package helper must run as root")
    try:
        agent_uid = pwd.getpwnam("dgx-agent").pw_uid
        workload = pwd.getpwnam("dgx-workload")
    except KeyError as error:
        raise HelperProtocolError(
            "package helper identities are not installed"
        ) from error
    sandbox = SandboxPolicy(
        workload_uid=workload.pw_uid,
        workload_gid=workload.pw_gid,
        allowed_devices=(
            "nvidiactl",
            "nvidia-uvm",
            "nvidia-uvm-tools",
            "nvidia-modeset",
            *(f"nvidia{index}" for index in range(16)),
        ),
    )
    receipt_verifier = Ed25519ReceiptVerifier.from_file(
        Path("/etc/dgx-forge-agent/package-receipt-public.pem")
    )
    fence_authorizer = SignedFenceAuthorizer.from_file(
        Path("/etc/dgx-forge-agent/package-fence-public.pem"),
        Path("/var/lib/dgx-forge-package-helper/replay.sqlite3"),
    )
    receipt_key = getattr(receipt_verifier, "public_key_bytes", None)
    fence_key = getattr(fence_authorizer, "public_key_bytes", None)
    if receipt_key is None or fence_key is None:
        raise HelperProtocolError("package helper authority key boundary is invalid")
    if not isinstance(receipt_key, bytes) or not isinstance(fence_key, bytes):
        raise HelperProtocolError("package helper authority key boundary is invalid")
    if receipt_key == fence_key:
        raise HelperProtocolError("package helper grant and receipt keys are not distinct")
    helper = PackageHelper(
        agent_uid=agent_uid,
        sandbox=sandbox,
        active_slot_verifier=ActiveSlotVerifier(
            os.environ.get("DGX_PACKAGE_HELPER_SLOT_SHA256", "")
        ),
        receipt_verifier=receipt_verifier,
        fence_authorizer=fence_authorizer,
        launcher=SystemdBackendLauncher(
            Path("/var/lib/dgx-forge-agent/packages/generations")
        ),
    )
    listener = socket.socket(fileno=args.listen_fd)
    try:
        serve_listener(helper, listener)
    finally:
        listener.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
