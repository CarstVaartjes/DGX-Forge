"""Strict, bounded SSH execution for Spark controller operations."""

from __future__ import annotations

import math
import shlex
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

_DEFAULT_ALIASES = {"spark1": "dgx-spark-1", "spark2": "dgx-spark-2"}


class _Completed(Protocol):
    returncode: int
    stdout: bytes
    stderr: bytes


Executor = Callable[..., _Completed]


class _ReadableStream(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _WritableStream(Protocol):
    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class CommandResult:
    """A bounded record of one SSH command attempt."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass(frozen=True)
class _BoundedCompleted:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


class _BoundedBuffer:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: list[bytes] = []
        self._length = 0
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        remaining = self._limit - self._length
        if remaining > 0:
            kept = chunk[:remaining]
            self._parts.append(kept)
            self._length += len(kept)
        if len(chunk) > remaining:
            self.truncated = True

    def getvalue(self) -> bytes:
        return b"".join(self._parts)


def _drain(stream: _ReadableStream, output: _BoundedBuffer) -> None:
    read = stream.read
    try:
        while chunk := read(8192):
            output.append(chunk)
    finally:
        stream.close()


def _feed(stream: _WritableStream, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except BrokenPipeError:
        pass
    finally:
        try:
            stream.close()
        except BrokenPipeError:
            pass


def _bounded_subprocess(
    argv: tuple[str, ...],
    *,
    input: bytes | None,
    timeout: float,
    shell: bool,
    output_limit_bytes: int,
) -> _BoundedCompleted:
    """Run while draining all output but retaining at most the configured limit."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = _BoundedBuffer(output_limit_bytes)
    stderr = _BoundedBuffer(output_limit_bytes)
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    writer: threading.Thread | None = None
    if input is not None:
        assert process.stdin is not None
        writer = threading.Thread(target=_feed, args=(process.stdin, input), daemon=True)
        writer.start()

    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        if writer is not None:
            writer.join()
        for reader in readers:
            reader.join()
        raise subprocess.TimeoutExpired(
            cmd=argv,
            timeout=error.timeout,
            output=stdout.getvalue() + (b"\0" if stdout.truncated else b""),
            stderr=stderr.getvalue() + (b"\0" if stderr.truncated else b""),
        ) from error
    if writer is not None:
        writer.join()
    for reader in readers:
        reader.join()
    return _BoundedCompleted(
        returncode,
        stdout.getvalue(),
        stderr.getvalue(),
        stdout.truncated,
        stderr.truncated,
    )


def _validate_remote_argv(argv: tuple[str, ...]) -> None:
    if any(not isinstance(argument, str) for argument in argv):
        raise TypeError("remote argv must contain only strings")
    if any(character in argument for argument in argv for character in ("\0", "\n", "\r")):
        raise ValueError("remote argv must not contain NUL or newline characters")


class SshBackend:
    """Invoke configured Spark SSH aliases without a local shell."""

    def __init__(
        self,
        executor: Executor | None = None,
        *,
        node_aliases: Mapping[str, str] | None = None,
        connect_timeout_seconds: int = 10,
        output_limit_bytes: int = 65_536,
    ) -> None:
        if connect_timeout_seconds <= 0:
            raise ValueError("connect timeout must be positive")
        if output_limit_bytes <= 0:
            raise ValueError("output limit must be positive")
        self._executor = executor
        self._aliases = dict(node_aliases or _DEFAULT_ALIASES)
        self._connect_timeout = connect_timeout_seconds
        self._output_limit = output_limit_bytes

    def run(
        self, node: str, argv: tuple[str, ...], timeout: float
    ) -> CommandResult:
        """Run one explicit remote command and return bounded output."""
        if not argv:
            raise ValueError("remote argv must not be empty")
        return self._execute(node, argv, timeout, input_bytes=None)

    def run_script(
        self,
        node: str,
        script: bytes,
        argv: tuple[str, ...],
        timeout: float,
    ) -> CommandResult:
        """Send fixed caller-owned Bash bytes over stdin without persistence."""
        if not isinstance(script, bytes):
            raise TypeError("script must be bytes")
        return self._execute(
            node,
            ("bash", "-s", "--", *argv),
            timeout,
            input_bytes=script,
        )

    def _execute(
        self,
        node: str,
        remote_argv: tuple[str, ...],
        timeout: float,
        *,
        input_bytes: bytes | None,
    ) -> CommandResult:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            alias = self._aliases[node]
        except KeyError as error:
            raise ValueError(f"unknown node: {node}") from error
        _validate_remote_argv((alias, *remote_argv))
        remote_command = shlex.join(remote_argv)
        connect_timeout = min(self._connect_timeout, max(1, math.ceil(timeout)))
        command = (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            alias,
            remote_command,
        )
        try:
            if self._executor is None:
                completed = _bounded_subprocess(
                    command,
                    input=input_bytes,
                    timeout=timeout,
                    shell=False,
                    output_limit_bytes=self._output_limit,
                )
            else:
                completed = self._executor(
                    command,
                    input=input_bytes,
                    timeout=timeout,
                    shell=False,
                )
        except subprocess.TimeoutExpired as error:
            stdout, stdout_truncated = self._bound(error.output or b"")
            stderr, stderr_truncated = self._bound(error.stderr or b"")
            return CommandResult(
                returncode=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        stdout, stdout_truncated = self._bound(completed.stdout)
        stderr, stderr_truncated = self._bound(completed.stderr)
        stdout_truncated |= bool(getattr(completed, "stdout_truncated", False))
        stderr_truncated |= bool(getattr(completed, "stderr_truncated", False))
        return CommandResult(
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def _bound(self, output: bytes) -> tuple[bytes, bool]:
        if not isinstance(output, bytes):
            raise TypeError("SSH executor output must be bytes")
        return output[: self._output_limit], len(output) > self._output_limit
