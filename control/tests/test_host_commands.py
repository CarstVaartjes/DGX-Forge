from __future__ import annotations

import hashlib
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_control.host_commands import (
    ArtifactPolicy,
    BoundedCommandRunner,
    CommandPolicy,
    HostCommandError,
)


def _command_policy(*, timeout: float = 2, limit: int = 4096) -> CommandPolicy:
    return CommandPolicy(timeout, limit, limit)


def test_run_uses_exact_environment_cwd_and_closed_stdin(tmp_path: Path) -> None:
    result = BoundedCommandRunner().run(
        (
            sys.executable,
            "-c",
            "import os,sys; print(os.getcwd()); print(os.getenv('ONLY')); print(sys.stdin.read())",
        ),
        cwd=tmp_path,
        env={"ONLY": "present"},
        policy=_command_policy(),
    )

    assert result.stdout == f"{tmp_path}\npresent\n\n".encode()
    assert b"PATH" not in result.stdout
    assert result.stderr == b""


def test_run_rejects_shell_strings_and_relative_executables(tmp_path: Path) -> None:
    runner = BoundedCommandRunner()
    with pytest.raises(ValueError, match="argv"):
        runner.run(  # type: ignore[arg-type]
            "echo unsafe",
            cwd=tmp_path,
            env={},
            policy=_command_policy(),
        )
    with pytest.raises(ValueError, match="argv"):
        runner.run(
            ("python", "-c", "print('unsafe path lookup')"),
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            policy=_command_policy(),
        )


def test_run_terminates_timeout_and_bounds_redacted_output(tmp_path: Path) -> None:
    with pytest.raises(HostCommandError, match="timeout") as timeout:
        BoundedCommandRunner().run(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            cwd=tmp_path,
            env={},
            policy=_command_policy(timeout=0.05),
        )
    assert timeout.value.result is not None
    assert timeout.value.result.elapsed_seconds < 2

    secret = "do-not-render-this-secret"
    with pytest.raises(HostCommandError, match="output limit") as oversized:
        BoundedCommandRunner().run(
            (sys.executable, "-c", f"print('{secret}' * 10000)"),
            cwd=tmp_path,
            env={},
            policy=_command_policy(limit=32),
        )
    assert secret not in str(oversized.value)
    assert oversized.value.result is not None
    assert secret not in repr(oversized.value.__dict__)
    assert oversized.value.result.stdout.startswith(b"<redacted:")
    assert len(oversized.value.result.stdout_sha256) == 64
    int(oversized.value.result.stdout_sha256, 16)


def test_run_kills_process_that_ignores_termination_and_closes_extra_fds(
    tmp_path: Path,
) -> None:
    inherited = os.open(tmp_path / "inherited", os.O_RDONLY | os.O_CREAT, 0o600)
    os.set_inheritable(inherited, True)
    try:
        result = BoundedCommandRunner().run(
            (
                sys.executable,
                "-c",
                (
                    "import os,sys; fd=int(sys.argv[1]);\ntry: os.fstat(fd); "
                    "print('open')\nexcept OSError: print('closed')"
                ),
                str(inherited),
            ),
            cwd=tmp_path,
            env={},
            policy=_command_policy(),
        )
    finally:
        os.close(inherited)
    assert result.stdout == b"closed\n"

    with pytest.raises(HostCommandError, match="timeout") as timeout:
        BoundedCommandRunner().run(
            (
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            ),
            cwd=tmp_path,
            env={},
            policy=_command_policy(timeout=0.2),
        )
    assert timeout.value.result is not None
    assert timeout.value.result.returncode == -signal.SIGKILL


def test_run_timeout_kills_descendants_in_the_command_process_group(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    survived = tmp_path / "child-survived"
    child_program = (
        "import os,signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "time.sleep(0.8); "
        "open(sys.argv[2], 'w').write('survived')"
    )

    try:
        with pytest.raises(HostCommandError, match="timeout"):
            BoundedCommandRunner().run(
                (
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys,time; "
                        "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
                        "sys.argv[2], sys.argv[3]]); "
                        "time.sleep(30)"
                    ),
                    child_program,
                    str(child_pid_file),
                    str(survived),
                ),
                cwd=tmp_path,
                env={},
                policy=_command_policy(timeout=0.2),
            )
        time.sleep(1)
        assert not survived.exists()
    finally:
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text())
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_run_deadline_includes_descendants_after_direct_child_exits(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    child_program = (
        "import os,signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "time.sleep(0.8); print('late output')"
    )
    try:
        with pytest.raises(HostCommandError, match="timeout"):
            BoundedCommandRunner().run(
                (
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys; "
                        "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
                        "sys.argv[2]])"
                    ),
                    child_program,
                    str(child_pid_file),
                ),
                cwd=tmp_path,
                env={},
                policy=_command_policy(timeout=0.2),
            )
    finally:
        if child_pid_file.exists():
            try:
                os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_run_does_not_return_while_detached_output_descendant_is_alive(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    survived = tmp_path / "detached-child-survived"
    child_program = (
        "import os,signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "os.close(1); os.close(2); time.sleep(0.8); "
        "open(sys.argv[2], 'w').write('survived')"
    )
    try:
        with pytest.raises(HostCommandError, match="timeout"):
            BoundedCommandRunner().run(
                (
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys; "
                        "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
                        "sys.argv[2], sys.argv[3]])"
                    ),
                    child_program,
                    str(child_pid_file),
                    str(survived),
                ),
                cwd=tmp_path,
                env={},
                policy=_command_policy(timeout=0.2),
            )
        time.sleep(0.9)
        assert not survived.exists()
    finally:
        if child_pid_file.exists():
            try:
                os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stream_hashes_to_preopened_fd_without_artifact_sized_result(
    tmp_path: Path,
) -> None:
    content = b"artifact-content" * 100_000
    source = tmp_path / "source"
    sink = tmp_path / "sink"
    source.write_bytes(content)
    with source.open("rb") as input_file, sink.open("w+b") as output_file:
        receipt = BoundedCommandRunner().stream(
            ("/bin/cat",),
            cwd=tmp_path,
            env={},
            source_fd=input_file.fileno(),
            sink_fd=output_file.fileno(),
            command=_command_policy(timeout=5),
            artifact=ArtifactPolicy(len(content), 0),
        )

    assert receipt.byte_count == len(content)
    assert receipt.sha256 == hashlib.sha256(content).hexdigest()
    assert sink.read_bytes() == content
    assert not hasattr(receipt, "content")


def test_stream_enforces_limit_and_truncates_incomplete_sink(tmp_path: Path) -> None:
    sink = tmp_path / "sink"
    with sink.open("w+b") as output_file:
        with pytest.raises(HostCommandError, match="limit"):
            BoundedCommandRunner().stream(
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 10000)",
                ),
                cwd=tmp_path,
                env={},
                source_fd=None,
                sink_fd=output_file.fileno(),
                command=_command_policy(),
                artifact=ArtifactPolicy(100, 0),
            )
        assert output_file.seek(0, os.SEEK_END) == 0


def test_stream_surfaces_incomplete_sink_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "sink"
    with sink.open("w+b") as output_file:

        def fail_cleanup(_descriptor: int, _size: int) -> None:
            raise OSError("simulated cleanup failure")

        monkeypatch.setattr(os, "ftruncate", fail_cleanup)
        with pytest.raises(HostCommandError, match="cleanup"):
            BoundedCommandRunner().stream(
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 10000)",
                ),
                cwd=tmp_path,
                env={},
                source_fd=None,
                sink_fd=output_file.fileno(),
                command=_command_policy(),
                artifact=ArtifactPolicy(100, 0),
            )


def test_stream_rejects_duplicate_descriptor_for_source_and_sink(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "sink"
    marker = tmp_path / "command-ran"
    with sink.open("w+b") as output_file:
        source_fd = os.dup(output_file.fileno())
        try:
            with pytest.raises(ValueError, match="must differ"):
                BoundedCommandRunner().stream(
                    (
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).touch()",
                    ),
                    cwd=tmp_path,
                    env={},
                    source_fd=source_fd,
                    sink_fd=output_file.fileno(),
                    command=_command_policy(),
                    artifact=ArtifactPolicy(1024, 0),
                )
        finally:
            os.close(source_fd)
    assert not marker.exists()


def test_stream_deadline_includes_descendants_after_direct_child_exits(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "sink"
    child_pid_file = tmp_path / "child.pid"
    child_program = (
        "import os,signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "time.sleep(0.8); print('late artifact', end='')"
    )
    try:
        with sink.open("w+b") as output_file:
            with pytest.raises(HostCommandError, match="timeout"):
                BoundedCommandRunner().stream(
                    (
                        sys.executable,
                        "-c",
                        (
                            "import subprocess,sys; "
                            "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
                            "sys.argv[2]])"
                        ),
                        child_program,
                        str(child_pid_file),
                    ),
                    cwd=tmp_path,
                    env={},
                    source_fd=None,
                    sink_fd=output_file.fileno(),
                    command=_command_policy(timeout=0.2),
                    artifact=ArtifactPolicy(1024, 0),
                )
            assert output_file.seek(0, os.SEEK_END) == 0
    finally:
        if child_pid_file.exists():
            try:
                os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stream_enforces_worst_case_disk_reservation_before_start(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "sink"
    marker = tmp_path / "command-ran"
    with (
        sink.open("w+b") as output_file,
        pytest.raises(HostCommandError, match="reservation"),
    ):
        BoundedCommandRunner().stream(
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ),
            cwd=tmp_path,
            env={},
            source_fd=None,
            sink_fd=output_file.fileno(),
            command=_command_policy(),
            artifact=ArtifactPolicy(16 * 1024**4, 16 * 1024**4),
        )
    assert not marker.exists()


def test_stream_rechecks_reservation_while_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "sink"
    with sink.open("w+b") as output_file:
        available = os.fstatvfs(output_file.fileno())
        calls = 0

        def disappearing_capacity(_descriptor: int):
            nonlocal calls
            calls += 1
            if calls == 1:
                return available
            return SimpleNamespace(f_bavail=0, f_frsize=available.f_frsize)

        monkeypatch.setattr(os, "fstatvfs", disappearing_capacity)
        with pytest.raises(HostCommandError, match="reservation"):
            BoundedCommandRunner().stream(
                (sys.executable, "-c", "print('artifact', end='')"),
                cwd=tmp_path,
                env={},
                source_fd=None,
                sink_fd=output_file.fileno(),
                command=_command_policy(),
                artifact=ArtifactPolicy(1024, 0),
            )
        assert calls >= 2
        assert output_file.seek(0, os.SEEK_END) == 0


def test_stream_rechecks_reservation_before_returning_empty_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "sink"
    with sink.open("w+b") as output_file:
        available = os.fstatvfs(output_file.fileno())
        calls = 0

        def disappearing_capacity(_descriptor: int):
            nonlocal calls
            calls += 1
            if calls == 1:
                return available
            return SimpleNamespace(f_bavail=0, f_frsize=available.f_frsize)

        monkeypatch.setattr(os, "fstatvfs", disappearing_capacity)
        with pytest.raises(HostCommandError, match="reservation"):
            BoundedCommandRunner().stream(
                (sys.executable, "-c", "pass"),
                cwd=tmp_path,
                env={},
                source_fd=None,
                sink_fd=output_file.fileno(),
                command=_command_policy(),
                artifact=ArtifactPolicy(1024, 0),
            )
        assert calls >= 2


def test_stream_rejects_sink_replacement_before_returning_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "sink"
    replacement = tmp_path / "replacement"
    replacement_fd = os.open(replacement, os.O_RDWR | os.O_CREAT, 0o600)
    real_fsync = os.fsync
    try:
        with sink.open("w+b") as output_file:
            sink_fd = output_file.fileno()

            def replace_then_fsync(descriptor: int) -> None:
                os.dup2(replacement_fd, descriptor)
                real_fsync(descriptor)

            monkeypatch.setattr(os, "fsync", replace_then_fsync)
            with pytest.raises(HostCommandError, match="sink"):
                BoundedCommandRunner().stream(
                    (sys.executable, "-c", "print('artifact', end='')"),
                    cwd=tmp_path,
                    env={},
                    source_fd=None,
                    sink_fd=sink_fd,
                    command=_command_policy(),
                    artifact=ArtifactPolicy(1024, 0),
                )
    finally:
        os.close(replacement_fd)


@pytest.mark.parametrize("race", ["truncate", "append"])
def test_stream_rejects_sink_size_or_offset_race_before_returning_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    sink = tmp_path / "sink"
    real_fsync = os.fsync
    with sink.open("w+b") as output_file:
        sink_fd = output_file.fileno()

        def race_then_fsync(descriptor: int) -> None:
            if race == "truncate":
                os.ftruncate(descriptor, 0)
            else:
                os.write(descriptor, b"untrusted-append")
            real_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", race_then_fsync)
        with pytest.raises(HostCommandError, match="sink"):
            BoundedCommandRunner().stream(
                (sys.executable, "-c", "print('artifact', end='')"),
                cwd=tmp_path,
                env={},
                source_fd=None,
                sink_fd=sink_fd,
                command=_command_policy(),
                artifact=ArtifactPolicy(1024, 0),
            )
