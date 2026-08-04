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
_CLEANUP_RESERVE_SECONDS = 0.04
_STOP = False


def _request_stop(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def _set_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable child subreaper")


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


def _report(descriptor: int, status: str, code: int = 0) -> None:
    body = f"{status}:{code}\n".encode("ascii")
    if len(body) > 32 or os.write(descriptor, body) != len(body):
        raise OSError("supervisor status write failed")


def main(argv: list[str]) -> int:
    if len(argv) < 7:
        return 126
    status_fd = int(argv[1])
    executable_fd = int(argv[2])
    support_fd = int(argv[3])
    absolute_deadline = float(argv[4])
    cwd = argv[5]
    tool_argv = tuple(argv[6:])
    inherited = (executable_fd,) if support_fd < 0 else (executable_fd, support_fd)
    _set_subreaper()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    execution_deadline = absolute_deadline - _CLEANUP_RESERVE_SECONDS
    if time.monotonic() >= execution_deadline:
        _report(status_fd, "timeout")
        return 0
    try:
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
        _report(status_fd, "launch")
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
                process.wait(timeout=max(0.0, min(0.015, absolute_deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                _signal_group(process.pid, signal.SIGKILL)
                try:
                    process.wait(timeout=0.02)
                except subprocess.TimeoutExpired:
                    _signal_pid(process.pid, signal.SIGKILL)
        returncode = process.poll()
        _cleanup(process.pid, absolute_deadline)
        if timed_out or _STOP or time.monotonic() >= absolute_deadline:
            _report(status_fd, "timeout")
            return 0
        if returncode is None:
            _report(status_fd, "internal")
            return 0
        _report(status_fd, "ok", returncode if 0 <= returncode <= 123 else 125)
        return 0
    finally:
        _cleanup(process.pid, absolute_deadline)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (OSError, ValueError):
        try:
            _report(int(sys.argv[1]), "internal")
        except (IndexError, OSError, ValueError):
            pass
        raise SystemExit(126)
