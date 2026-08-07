"""Safe, injectable SSH/SCP boundary for per-node onboarding."""

from __future__ import annotations

import math
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cluster_profiles.fleet import ManagementEndpoint
from cluster_profiles.ssh_transport import select_transport_binary

_SENSITIVE = re.compile(
    r"(?i)(authorization\s*:|bearer\s+|password\b|private[_-]?key\b|secret\b|token\b)"
)
_REMOTE_PATH = re.compile(r"/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+")
_ALLOWED_MODES = frozenset({0o600, 0o644, 0o700, 0o755})


class UnsafeInstallArgument(ValueError):
    """An onboarding transport argument is unsafe or credential-bearing."""


@dataclass(frozen=True)
class RemoteResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class InstallTransport(Protocol):
    def run(
        self,
        endpoint: ManagementEndpoint,
        argv: tuple[str, ...],
        stdin: bytes,
        timeout: float,
    ) -> RemoteResult: ...

    def copy(
        self,
        endpoint: ManagementEndpoint,
        source: Path,
        destination: str,
        mode: int,
    ) -> RemoteResult: ...


_Execute = Callable[..., subprocess.CompletedProcess[bytes]]


class OpenSshInstallTransport:
    def __init__(
        self,
        *,
        execute: _Execute = subprocess.run,
        ssh_bin: str | None = None,
        scp_bin: str | None = None,
        connect_timeout_seconds: int = 10,
        output_limit_bytes: int = 65536,
    ) -> None:
        if connect_timeout_seconds <= 0:
            raise ValueError("connect timeout must be positive")
        if output_limit_bytes <= 0:
            raise ValueError("output limit must be positive")
        self._execute = execute
        self._ssh_bin = ssh_bin or select_transport_binary("ssh")
        self._scp_bin = scp_bin or select_transport_binary("scp")
        self._connect_timeout = connect_timeout_seconds
        self._output_limit = output_limit_bytes

    @staticmethod
    def _target(endpoint: ManagementEndpoint) -> str:
        if (
            not endpoint.host.strip()
            or not endpoint.user.strip()
            or not 1 <= endpoint.port <= 65535
            or any(character in endpoint.host + endpoint.user for character in "\x00\r\n@")
        ):
            raise UnsafeInstallArgument("management endpoint is unsafe")
        return f"{endpoint.user}@{endpoint.host}"

    @staticmethod
    def _validate_argv(argv: tuple[str, ...]) -> None:
        if not argv:
            raise UnsafeInstallArgument("remote command must not be empty")
        for argument in argv:
            if not isinstance(argument, str) or "\x00" in argument:
                raise UnsafeInstallArgument("remote command contains an unsafe argument")
            if _SENSITIVE.search(argument):
                raise UnsafeInstallArgument(
                    "remote command arguments must not contain credentials"
                )

    def _options(self, timeout: float) -> tuple[str, ...]:
        if timeout <= 0:
            raise ValueError("remote timeout must be positive")
        connect_timeout = min(
            self._connect_timeout,
            max(1, math.ceil(timeout)),
        )
        return (
            "-o",
            "BatchMode=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
        )

    def _result(self, completed: subprocess.CompletedProcess[bytes]) -> RemoteResult:
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        return RemoteResult(
            completed.returncode,
            stdout[: self._output_limit],
            stderr[: self._output_limit],
        )

    def run(
        self,
        endpoint: ManagementEndpoint,
        argv: tuple[str, ...],
        stdin: bytes,
        timeout: float,
    ) -> RemoteResult:
        self._validate_argv(argv)
        if not isinstance(stdin, bytes):
            raise TypeError("remote stdin must be bytes")
        command = (
            self._ssh_bin,
            *self._options(timeout),
            "-p",
            str(endpoint.port),
            self._target(endpoint),
            shlex.join(argv),
        )
        completed = self._execute(
            command,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
        return self._result(completed)

    def copy(
        self,
        endpoint: ManagementEndpoint,
        source: Path,
        destination: str,
        mode: int,
    ) -> RemoteResult:
        if source.is_symlink() or not source.is_file():
            raise UnsafeInstallArgument("copy source must be a regular non-symlink file")
        if (
            _REMOTE_PATH.fullmatch(destination) is None
            or ".." in Path(destination).parts
        ):
            raise UnsafeInstallArgument("remote copy destination is unsafe")
        if mode not in _ALLOWED_MODES:
            raise UnsafeInstallArgument("remote copy mode is unsupported")
        timeout = float(self._connect_timeout)
        command = (
            self._scp_bin,
            *self._options(timeout),
            "-P",
            str(endpoint.port),
            str(source),
            f"{self._target(endpoint)}:{destination}",
        )
        copied = self._execute(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
        copy_result = self._result(copied)
        if copy_result.returncode != 0:
            return copy_result
        return self.run(
            endpoint,
            ("chmod", f"{mode:04o}", "--", destination),
            b"",
            timeout,
        )
