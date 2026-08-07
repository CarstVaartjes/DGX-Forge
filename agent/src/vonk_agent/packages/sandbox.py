"""Fail-closed launch plans for unprivileged workload processes."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .backends import BackendInvocation

_FIXED_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
    }
)


class SandboxError(ValueError):
    """A launch cannot be represented by the compiled sandbox policy."""


@dataclass(frozen=True)
class SandboxLaunch:
    executable: str
    argv: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]
    uid: int
    gid: int
    inherited_fds: tuple[int, ...]
    no_new_privileges: bool = True
    ambient_capabilities: tuple[str, ...] = ()
    private_devices: bool = True
    private_mounts: bool = True
    private_network: bool = True


@dataclass(frozen=True)
class SandboxPolicy:
    workload_uid: int
    workload_gid: int
    allowed_devices: tuple[str, ...] = ()
    max_cpu_millis: int = 1_000_000
    max_memory_bytes: int = 2**60
    max_pids: int = 65_536
    _environment: Mapping[str, str] = field(
        default=_FIXED_ENVIRONMENT, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        for value, name in (
            (self.workload_uid, "workload UID"),
            (self.workload_gid, "workload GID"),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= 2**31 - 1
            ):
                raise SandboxError(
                    f"{name} must select a dedicated unprivileged identity"
                )
        if len(self.allowed_devices) > 32 or len(set(self.allowed_devices)) != len(
            self.allowed_devices
        ):
            raise SandboxError("allowed device policy is invalid")
        for value, name, maximum in (
            (self.max_cpu_millis, "CPU", 1_000_000),
            (self.max_memory_bytes, "memory", 2**60),
            (self.max_pids, "PID", 65_536),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= maximum
            ):
                raise SandboxError(f"maximum {name} policy is invalid")

    def plan(
        self,
        invocation: BackendInvocation,
        generation_fd: int,
        executable_fd: int,
    ) -> SandboxLaunch:
        if type(invocation) is not BackendInvocation:
            raise SandboxError("backend invocation is invalid")
        _directory_descriptor(generation_fd)
        _executable_descriptor(executable_fd)
        if generation_fd == executable_fd:
            raise SandboxError("sandbox descriptors are not separated")
        resources = invocation.resources
        if resources.cpu_millis > self.max_cpu_millis:
            raise SandboxError("requested CPU exceeds the sandbox ceiling")
        if resources.memory_bytes > self.max_memory_bytes:
            raise SandboxError("requested memory exceeds the sandbox ceiling")
        if resources.pids_limit > self.max_pids:
            raise SandboxError("requested PIDs exceed the sandbox ceiling")
        if not set(invocation.devices).issubset(self.allowed_devices):
            raise SandboxError("requested device is not declared by node policy")
        executable = f"/proc/self/fd/{executable_fd}"
        cwd = f"/proc/self/fd/{generation_fd}"
        return SandboxLaunch(
            executable=executable,
            argv=(executable, *invocation.arguments),
            cwd=cwd,
            environment=self._environment,
            uid=self.workload_uid,
            gid=self.workload_gid,
            inherited_fds=(generation_fd, executable_fd),
            private_network=invocation.network.mode == "none",
        )


def _directory_descriptor(descriptor: int) -> None:
    try:
        metadata = os.fstat(descriptor)
    except (OSError, TypeError) as error:
        raise SandboxError("generation descriptor is invalid") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise SandboxError("generation descriptor is invalid")


def _executable_descriptor(descriptor: int) -> None:
    try:
        metadata = os.fstat(descriptor)
    except (OSError, TypeError) as error:
        raise SandboxError("executable descriptor is invalid") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise SandboxError("executable descriptor is invalid")
