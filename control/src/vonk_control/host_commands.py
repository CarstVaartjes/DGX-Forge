"""Bounded, shell-free subprocess execution for root control-host operations."""

from __future__ import annotations

import hashlib
import os
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class HostCommandError(RuntimeError):
    """A command violated policy or did not complete successfully."""

    def __init__(self, reason: str, *, result: CommandResult | None = None) -> None:
        super().__init__(f"host command failed: {reason}")
        self.reason = reason
        self.result = result


@dataclass(frozen=True)
class CommandPolicy:
    timeout_seconds: float
    stdout_limit: int
    stderr_limit: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= 3600
            or isinstance(self.stdout_limit, bool)
            or not isinstance(self.stdout_limit, int)
            or not 0 <= self.stdout_limit <= 16 * 1024 * 1024
            or isinstance(self.stderr_limit, bool)
            or not isinstance(self.stderr_limit, int)
            or not 0 <= self.stderr_limit <= 16 * 1024 * 1024
        ):
            raise ValueError("command policy is invalid")


@dataclass(frozen=True)
class ArtifactPolicy:
    byte_limit: int
    required_free_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.byte_limit, bool)
            or not isinstance(self.byte_limit, int)
            or not 1 <= self.byte_limit <= 16 * 1024**4
            or isinstance(self.required_free_bytes, bool)
            or not isinstance(self.required_free_bytes, int)
            or not 0 <= self.required_free_bytes <= 16 * 1024**4
        ):
            raise ValueError("artifact policy is invalid")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    stdout_sha256: str = ""
    stderr_sha256: str = ""


@dataclass(frozen=True)
class ArtifactReceipt:
    byte_count: int
    sha256: str


@dataclass
class _Drain:
    limit: int
    content: bytearray
    exceeded: bool = False
    error: OSError | None = None


def _validate_invocation(
    argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]
) -> tuple[Path, dict[str, str]]:
    if (
        not isinstance(argv, tuple)
        or not 1 <= len(argv) <= 128
        or any(
            not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096
            for item in argv
        )
        or not Path(argv[0]).is_absolute()
    ):
        raise ValueError("command argv is invalid")
    root = Path(cwd)
    if (
        not root.is_absolute()
        or not root.is_dir()
        or root.is_symlink()
        or root.resolve(strict=True) != root
    ):
        raise ValueError("command cwd is invalid")
    if not isinstance(env, Mapping) or len(env) > 128:
        raise ValueError("command environment is invalid")
    clean: dict[str, str] = {}
    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or len(key) > 256
            or not isinstance(value, str)
            or "\x00" in value
            or len(value) > 16 * 1024
        ):
            raise ValueError("command environment is invalid")
        clean[key] = value
    return root, clean


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate(process: subprocess.Popen[bytes]) -> None:
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    grace_deadline = time.monotonic() + 0.25
    while _process_group_exists(process_group) and time.monotonic() < grace_deadline:
        process.poll()
        time.sleep(0.005)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=1)


def _bounded_reader(pipe, drain: _Drain) -> None:
    try:
        while True:
            chunk = pipe.read(64 * 1024)
            if not chunk:
                return
            remaining = max(0, drain.limit - len(drain.content))
            drain.content.extend(chunk[:remaining])
            if len(chunk) > remaining:
                drain.exceeded = True
    except OSError as error:
        drain.error = error


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("artifact sink write failed")
        view = view[written:]


def _restore_sink(
    descriptor: int,
    initial_info: os.stat_result,
    initial_size: int,
    initial_offset: int,
) -> None:
    try:
        os.ftruncate(descriptor, initial_size)
        os.lseek(descriptor, initial_offset, os.SEEK_SET)
        restored_info = os.fstat(descriptor)
        restored_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    except OSError as error:
        raise HostCommandError("artifact sink cleanup failure") from error
    if (
        (restored_info.st_dev, restored_info.st_ino)
        != (initial_info.st_dev, initial_info.st_ino)
        or not stat.S_ISREG(restored_info.st_mode)
        or restored_info.st_nlink != 1
        or restored_info.st_size != initial_size
        or restored_offset != initial_offset
    ):
        raise HostCommandError("artifact sink cleanup failure")


def _artifact_capacity_available(
    descriptor: int, policy: ArtifactPolicy, byte_count: int
) -> bool:
    filesystem = os.fstatvfs(descriptor)
    free_bytes = filesystem.f_bavail * filesystem.f_frsize
    remaining_bytes = policy.byte_limit - byte_count
    return free_bytes >= remaining_bytes + policy.required_free_bytes


def _redacted_result(result: CommandResult) -> CommandResult:
    return CommandResult(
        returncode=result.returncode,
        stdout=f"<redacted:{len(result.stdout)} bytes>".encode("ascii"),
        stderr=f"<redacted:{len(result.stderr)} bytes>".encode("ascii"),
        elapsed_seconds=result.elapsed_seconds,
        stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
        stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
    )


class BoundedCommandRunner:
    """Execute fixed argv with exact environment and bounded I/O."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        policy: CommandPolicy,
    ) -> CommandResult:
        root, clean_env = _validate_invocation(argv, cwd, env)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=root,
                env=clean_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise HostCommandError("start failure") from error
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = _Drain(policy.stdout_limit, bytearray())
        stderr = _Drain(policy.stderr_limit, bytearray())
        readers = (
            threading.Thread(target=_bounded_reader, args=(process.stdout, stdout)),
            threading.Thread(target=_bounded_reader, args=(process.stderr, stderr)),
        )
        for reader in readers:
            reader.start()
        reason: str | None = None
        deadline = started + policy.timeout_seconds
        while (
            process.poll() is None
            or any(reader.is_alive() for reader in readers)
            or _process_group_exists(process.pid)
        ):
            if stdout.exceeded or stderr.exceeded:
                reason = "output limit exceeded"
                _terminate(process)
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                _terminate(process)
                break
            time.sleep(0.005)
        for reader in readers:
            reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
        elapsed = time.monotonic() - started
        result = CommandResult(
            returncode=process.returncode,
            stdout=bytes(stdout.content),
            stderr=bytes(stderr.content),
            elapsed_seconds=elapsed,
            stdout_sha256=hashlib.sha256(stdout.content).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr.content).hexdigest(),
        )
        if reason is not None:
            raise HostCommandError(reason, result=_redacted_result(result))
        if stdout.error is not None or stderr.error is not None:
            raise HostCommandError("I/O failure", result=_redacted_result(result))
        if stdout.exceeded or stderr.exceeded:
            raise HostCommandError(
                "output limit exceeded", result=_redacted_result(result)
            )
        if process.returncode != 0:
            raise HostCommandError("nonzero exit", result=_redacted_result(result))
        return result

    def stream(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        source_fd: int | None,
        sink_fd: int,
        command: CommandPolicy,
        artifact: ArtifactPolicy,
    ) -> ArtifactReceipt:
        root, clean_env = _validate_invocation(argv, cwd, env)
        if isinstance(sink_fd, bool) or not isinstance(sink_fd, int) or sink_fd < 0:
            raise ValueError("artifact sink fd is invalid")
        if source_fd is not None and (
            isinstance(source_fd, bool)
            or not isinstance(source_fd, int)
            or source_fd < 0
        ):
            raise ValueError("artifact source fd is invalid")
        try:
            sink_info = os.fstat(sink_fd)
            initial_size = sink_info.st_size
            initial_offset = os.lseek(sink_fd, 0, os.SEEK_CUR)
        except OSError as error:
            raise HostCommandError("artifact sink unavailable") from error
        if (
            not stat.S_ISREG(sink_info.st_mode)
            or sink_info.st_nlink != 1
            or initial_size != 0
            or initial_offset != 0
        ):
            raise HostCommandError("artifact sink is unsafe")
        if source_fd == sink_fd:
            raise ValueError("artifact source and sink must differ")
        if source_fd is not None:
            try:
                source_info = os.fstat(source_fd)
            except OSError as error:
                raise HostCommandError("artifact source unavailable") from error
            if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
                raise HostCommandError("artifact source is unsafe")
            if (source_info.st_dev, source_info.st_ino) == (
                sink_info.st_dev,
                sink_info.st_ino,
            ):
                raise ValueError("artifact source and sink must differ")
        try:
            has_initial_capacity = _artifact_capacity_available(sink_fd, artifact, 0)
        except OSError as error:
            raise HostCommandError("artifact disk reservation unavailable") from error
        if not has_initial_capacity:
            raise HostCommandError("artifact disk reservation unavailable")

        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=root,
                env=clean_env,
                stdin=subprocess.DEVNULL if source_fd is None else source_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise HostCommandError("start failure") from error
        assert process.stdout is not None
        assert process.stderr is not None
        stderr = _Drain(command.stderr_limit, bytearray())
        stderr_reader = threading.Thread(
            target=_bounded_reader, args=(process.stderr, stderr)
        )
        digest = hashlib.sha256()
        count = 0
        artifact_exceeded = False
        reservation_exceeded = False
        write_error: OSError | None = None

        def artifact_reader() -> None:
            nonlocal count, artifact_exceeded, reservation_exceeded, write_error
            try:
                while True:
                    chunk = process.stdout.read(1024 * 1024)
                    if not chunk:
                        return
                    if count + len(chunk) > artifact.byte_limit:
                        artifact_exceeded = True
                        continue
                    if not _artifact_capacity_available(sink_fd, artifact, count):
                        reservation_exceeded = True
                        return
                    _write_all(sink_fd, chunk)
                    digest.update(chunk)
                    count += len(chunk)
                    if not _artifact_capacity_available(sink_fd, artifact, count):
                        reservation_exceeded = True
                        return
            except OSError as error:
                write_error = error

        output_reader = threading.Thread(target=artifact_reader)
        stderr_reader.start()
        output_reader.start()
        reason: str | None = None
        deadline = started + command.timeout_seconds
        while (
            process.poll() is None
            or output_reader.is_alive()
            or stderr_reader.is_alive()
            or _process_group_exists(process.pid)
        ):
            if reservation_exceeded:
                reason = "artifact disk reservation unavailable"
                _terminate(process)
                break
            if artifact_exceeded or stderr.exceeded or write_error is not None:
                reason = "artifact or output limit exceeded"
                _terminate(process)
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                _terminate(process)
                break
            time.sleep(0.005)
        output_reader.join(timeout=1)
        stderr_reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
        if (
            reason is not None
            or artifact_exceeded
            or reservation_exceeded
            or stderr.exceeded
            or stderr.error is not None
            or write_error is not None
            or process.returncode != 0
        ):
            _restore_sink(sink_fd, sink_info, initial_size, initial_offset)
            if reason == "timeout":
                failure = "timeout"
            elif reservation_exceeded:
                failure = "artifact disk reservation unavailable"
            elif process.returncode != 0:
                failure = "nonzero exit"
            elif write_error is not None or stderr.error is not None:
                failure = "I/O failure"
            else:
                failure = "artifact or output limit exceeded"
            raise HostCommandError(failure)
        try:
            os.fsync(sink_fd)
        except OSError as error:
            _restore_sink(sink_fd, sink_info, initial_size, initial_offset)
            raise HostCommandError("artifact fsync failure") from error
        try:
            has_final_capacity = _artifact_capacity_available(sink_fd, artifact, count)
        except OSError as error:
            _restore_sink(sink_fd, sink_info, initial_size, initial_offset)
            raise HostCommandError("artifact disk reservation unavailable") from error
        if not has_final_capacity:
            _restore_sink(sink_fd, sink_info, initial_size, initial_offset)
            raise HostCommandError("artifact disk reservation unavailable")
        try:
            final_info = os.fstat(sink_fd)
            final_offset = os.lseek(sink_fd, 0, os.SEEK_CUR)
        except OSError as error:
            _restore_sink(sink_fd, sink_info, initial_size, initial_offset)
            raise HostCommandError("artifact sink verification failure") from error
        if (
            (final_info.st_dev, final_info.st_ino)
            != (sink_info.st_dev, sink_info.st_ino)
            or not stat.S_ISREG(final_info.st_mode)
            or final_info.st_nlink != 1
            or final_info.st_size != initial_size + count
            or final_offset != initial_offset + count
        ):
            _restore_sink(sink_fd, sink_info, initial_size, initial_offset)
            raise HostCommandError("artifact sink changed")
        return ArtifactReceipt(byte_count=count, sha256=digest.hexdigest())
