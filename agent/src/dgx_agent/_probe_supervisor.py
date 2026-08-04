"""Isolated Linux subreaper for one fixed probe process tree."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_PDEATHSIG = 1
_CLEANUP_RESERVE_SECONDS = 0.04
_STOP = False


def _request_stop(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def _set_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable child subreaper")


def _set_parent_death_signal(expected_parent: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot set parent-death signal")
    if os.getppid() != expected_parent:
        raise OSError("guardian parent exited during setup")


def _direct_children() -> tuple[int, ...]:
    parent = os.getpid()
    children: list[int] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            raw = Path(entry.path, "stat").read_text(encoding="ascii")
            remainder = raw[raw.rfind(")") + 2 :].split()
            if int(remainder[1]) == parent:
                children.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ValueError, IndexError, OSError):
            continue
    return tuple(children)


def _signal_pid(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass


def _signal_group(process_group: int, signum: int) -> None:
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        pass


def _reap() -> None:
    while True:
        try:
            child, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if child == 0:
            return


def _cleanup(process_group: int, absolute_deadline: float) -> None:
    _signal_group(process_group, signal.SIGTERM)
    soft_end = min(absolute_deadline, time.monotonic() + 0.015)
    while True:
        _reap()
        children = _direct_children()
        if not children:
            return
        for child in children:
            _signal_pid(child, signal.SIGTERM)
        if time.monotonic() >= soft_end:
            break
        time.sleep(0.005)
    for child in _direct_children():
        _signal_pid(child, signal.SIGKILL)
    _signal_group(process_group, signal.SIGKILL)
    reap_end = time.monotonic() + 0.03
    while time.monotonic() < reap_end:
        _reap()
        children = _direct_children()
        if not children:
            return
        for child in children:
            _signal_pid(child, signal.SIGKILL)
        time.sleep(0.005)
    _reap()


def _report(descriptor: int, *fields: object) -> None:
    body = (":".join(str(field) for field in fields) + "\n").encode("ascii")
    if len(body) > 32 or os.write(descriptor, body) != len(body):
        raise OSError("supervisor status write failed")


def _guard(argv: list[str]) -> int:
    """Keep an authenticated subreaper alive around the untrusted tool."""
    if len(argv) < 10:
        return 126
    gate_fd = int(argv[2])
    result_fd = int(argv[3])
    executable_fd = int(argv[4])
    support_fd = int(argv[5])
    absolute_deadline = float(argv[6])
    expected_parent = int(argv[7])
    cwd = argv[8]
    tool_argv = tuple(argv[9:])
    inherited = (executable_fd,) if support_fd < 0 else (executable_fd, support_fd)
    execution_deadline = absolute_deadline - _CLEANUP_RESERVE_SECONDS
    _set_subreaper()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    _set_parent_death_signal(expected_parent)
    try:
        release = os.read(gate_fd, 1)
        os.close(gate_fd)
        if release != b"G":
            return 126
        if time.monotonic() >= execution_deadline:
            _report(result_fd, "timeout", 0)
            return 0
        process = subprocess.Popen(
            list(tool_argv),
            executable=f"/proc/self/fd/{executable_fd}",
            shell=False,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=dict(os.environ),
            close_fds=True,
            pass_fds=inherited,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        _report(result_fd, "launch", 0)
        return 0
    try:
        while process.poll() is None and not _STOP:
            if time.monotonic() >= execution_deadline:
                break
            time.sleep(0.005)
        timed_out = time.monotonic() >= execution_deadline
        if process.poll() is None:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(
                    timeout=max(
                        0.0,
                        min(0.015, absolute_deadline - time.monotonic()),
                    )
                )
            except subprocess.TimeoutExpired:
                _signal_group(process.pid, signal.SIGKILL)
                try:
                    process.wait(timeout=0.02)
                except subprocess.TimeoutExpired:
                    _signal_pid(process.pid, signal.SIGKILL)
        returncode = process.poll()
        _cleanup(process.pid, absolute_deadline)
        if timed_out or _STOP or time.monotonic() >= absolute_deadline:
            _report(result_fd, "timeout", 0)
            return 0
        if returncode is None:
            _report(result_fd, "internal", 0)
            return 0
        _report(result_fd, "ok", returncode if 0 <= returncode <= 123 else 125)
        return 0
    finally:
        _cleanup(process.pid, absolute_deadline)


def main(argv: list[str]) -> int:
    if len(argv) < 8:
        return 126
    status_fd = int(argv[1])
    acknowledgement_fd = int(argv[2])
    executable_fd = int(argv[3])
    support_fd = int(argv[4])
    absolute_deadline = float(argv[5])
    cwd = argv[6]
    tool_argv = tuple(argv[7:])
    inherited = (executable_fd,) if support_fd < 0 else (executable_fd, support_fd)
    _set_subreaper()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    execution_deadline = absolute_deadline - _CLEANUP_RESERVE_SECONDS
    if time.monotonic() >= execution_deadline:
        _report(status_fd, "done", "timeout", 0)
        return 0
    gate_read = -1
    gate_write = -1
    guardian_status_read = -1
    guardian_status_write = -1
    try:
        gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
        guardian_status_read, guardian_status_write = os.pipe2(os.O_CLOEXEC)
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__)),
                "--guard",
                str(gate_read),
                str(guardian_status_write),
                str(executable_fd),
                str(support_fd),
                repr(absolute_deadline),
                str(os.getpid()),
                cwd,
                *tool_argv,
            ],
            executable=sys.executable,
            shell=False,
            stdin=subprocess.DEVNULL,
            cwd="/",
            env=dict(os.environ),
            close_fds=True,
            pass_fds=(*inherited, gate_read, guardian_status_write),
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        _report(status_fd, "done", "launch", 0)
        return 0
    finally:
        if gate_read >= 0:
            os.close(gate_read)
        if guardian_status_write >= 0:
            os.close(guardian_status_write)
    try:
        _report(status_fd, "start", process.pid)
        if os.read(acknowledgement_fd, 1) != b"A":
            raise OSError("launcher acknowledgement failed")
        os.close(acknowledgement_fd)
        acknowledgement_fd = -1
        if os.write(gate_write, b"G") != 1:
            raise OSError("launcher release failed")
        os.close(gate_write)
        gate_write = -1
        while process.poll() is None and not _STOP:
            if time.monotonic() >= execution_deadline:
                break
            time.sleep(0.005)
        timed_out = time.monotonic() >= execution_deadline
        if process.poll() is None:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=max(0.0, min(0.015, absolute_deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                _signal_group(process.pid, signal.SIGKILL)
                try:
                    process.wait(timeout=0.02)
                except subprocess.TimeoutExpired:
                    _signal_pid(process.pid, signal.SIGKILL)
        returncode = process.poll()
        _cleanup(process.pid, absolute_deadline)
        guardian_status = os.read(guardian_status_read, 33)
        if timed_out or _STOP or time.monotonic() >= absolute_deadline:
            _report(status_fd, "done", "timeout", 0)
            return 0
        if (
            returncode != 0
            or len(guardian_status) > 32
            or not guardian_status.endswith(b"\n")
        ):
            _report(status_fd, "done", "internal", 0)
            return 0
        try:
            kind, raw_code = guardian_status[:-1].decode("ascii").split(":", 1)
            code = int(raw_code)
        except (UnicodeDecodeError, ValueError):
            _report(status_fd, "done", "internal", 0)
            return 0
        if kind == "ok" and 0 <= code <= 125:
            _report(status_fd, "done", "ok", code)
        elif kind in {"timeout", "launch"} and code == 0:
            _report(status_fd, "done", kind, 0)
        else:
            _report(status_fd, "done", "internal", 0)
        return 0
    finally:
        if gate_write >= 0:
            os.close(gate_write)
        if guardian_status_read >= 0:
            os.close(guardian_status_read)
        if acknowledgement_fd >= 0:
            os.close(acknowledgement_fd)
        _cleanup(process.pid, absolute_deadline)


if __name__ == "__main__":
    try:
        entrypoint = _guard if sys.argv[1:2] == ["--guard"] else main
        raise SystemExit(entrypoint(sys.argv))
    except (OSError, ValueError):
        try:
            _report(int(sys.argv[1]), "done", "internal", 0)
        except (IndexError, OSError, ValueError):
            pass
        raise SystemExit(126)
