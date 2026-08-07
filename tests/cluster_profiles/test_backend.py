from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import dataclass

import pytest

from cluster_profiles.backend import SshBackend
from cluster_profiles.fleet import Fleet, ManagementEndpoint, NodeId, NodeRecord


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: bytes = b"ok\n"
    stderr: bytes = b""


class FakeExec:
    def __init__(self, result: FakeCompleted | BaseException | None = None) -> None:
        self.result = result or FakeCompleted()
        self.argv: tuple[str, ...] = ()
        self.input_bytes: bytes | None = None
        self.timeout: float | None = None
        self.shell: bool | None = None
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        input: bytes | None,
        timeout: float,
        shell: bool,
    ) -> FakeCompleted:
        self.argv = argv
        self.calls.append(argv)
        self.input_bytes = input
        self.timeout = timeout
        self.shell = shell
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_backend_never_uses_shell_and_pins_strict_ssh_options() -> None:
    fake_exec = FakeExec()

    result = SshBackend(fake_exec).run(
        "spark1", ("profile-status", "--json"), 10
    )

    assert fake_exec.shell is False
    assert "BatchMode=yes" in fake_exec.argv
    assert "ForwardAgent=no" in fake_exec.argv
    assert "IdentitiesOnly=yes" in fake_exec.argv
    assert "StrictHostKeyChecking=yes" in fake_exec.argv
    assert "ConnectTimeout=10" in fake_exec.argv
    assert fake_exec.argv[-2:] == ("dgx-spark-1", "profile-status --json")
    assert fake_exec.input_bytes is None
    assert result.returncode == 0
    assert result.stdout == b"ok\n"


def test_backend_uses_explicit_developer_transport_override(monkeypatch) -> None:
    monkeypatch.setenv("SPARK_SSH_BIN", "/opt/custom/ssh-wrapper")
    fake_exec = FakeExec()

    SshBackend(fake_exec).run("spark1", ("true",), 10)

    assert fake_exec.argv[0] == "/opt/custom/ssh-wrapper"


def _fleet(count: int) -> Fleet:
    records = {}
    for index in range(count):
        node_id = NodeId.parse(f"spk_{index:032x}")
        records[node_id] = NodeRecord(
            id=node_id,
            display_name=f"node-{index}",
            hostname=f"node-{index}.local",
            management=ManagementEndpoint(f"10.0.0.{index + 1}", "operator", 2200 + index),
            labels={},
            lifecycle="ready",
        )
    return Fleet(2, records)


def test_backend_targets_every_configured_node() -> None:
    fleet = _fleet(16)
    fake_exec = FakeExec()
    backend = SshBackend.from_fleet(fleet, executor=fake_exec)

    for node_id in fleet.nodes:
        backend.run(node_id, ("true",), 10)

    assert len(fake_exec.calls) == 16
    for index, call in enumerate(fake_exec.calls):
        assert ("-p", str(2200 + index)) == call[call.index("-p") : call.index("-p") + 2]
        assert call[-2] == f"operator@10.0.0.{index + 1}"


def test_fleet_backend_rejects_unknown_legacy_alias() -> None:
    backend = SshBackend.from_fleet(_fleet(1), executor=FakeExec())

    with pytest.raises(ValueError, match="unknown node"):
        backend.run("spark1", ("true",), 10)


def test_run_script_delivers_fixed_bytes_on_stdin() -> None:
    fake_exec = FakeExec()
    script = b"printf '{}\\n'\n"

    SshBackend(fake_exec).run_script("spark2", script, ("--json",), 10)

    assert fake_exec.input_bytes == script
    assert fake_exec.shell is False
    assert fake_exec.argv[-2:] == ("dgx-spark-2", "bash -s -- --json")


@pytest.mark.parametrize("script_mode", [False, True])
def test_remote_argv_is_one_posix_quoted_command_and_cannot_be_evaluated(
    script_mode: bool,
) -> None:
    fake_exec = FakeExec()
    arguments = (
        "white space",
        "; printf evaluated",
        "$(printf evaluated)",
        "*",
        "single'quote",
        'double"quote',
        "",
    )
    backend = SshBackend(fake_exec)
    if script_mode:
        script = b'printf "%s\\0" "$@"\n'
        backend.run_script("spark1", script, arguments, 10)
        expected = ("bash", "-s", "--", *arguments)
    else:
        probe = (
            sys.executable,
            "-c",
            "import json,sys; print(json.dumps(sys.argv[1:]))",
            *arguments,
        )
        backend.run("spark1", probe, 10)
        expected = probe

    alias_index = fake_exec.argv.index("dgx-spark-1")
    assert len(fake_exec.argv[alias_index + 1 :]) == 1
    remote_command = fake_exec.argv[alias_index + 1]
    assert shlex.split(remote_command) == list(expected)
    if script_mode:
        completed = subprocess.run(
            ("/bin/sh", "-c", remote_command),
            input=script,
            check=True,
            capture_output=True,
            shell=False,
        )
        assert completed.stdout.split(b"\0")[:-1] == [
            argument.encode() for argument in arguments
        ]
    else:
        completed = subprocess.run(
            ("/bin/sh", "-c", remote_command),
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )
        assert json.loads(completed.stdout) == list(arguments)


@pytest.mark.parametrize("bad", ["bad\narg", "bad\rarg", "bad\0arg"])
def test_remote_argv_rejects_nul_and_newlines(bad: str) -> None:
    backend = SshBackend(FakeExec())

    with pytest.raises(ValueError, match="remote argv"):
        backend.run("spark1", ("command", bad), 10)
    with pytest.raises(ValueError, match="remote argv"):
        backend.run_script("spark1", b"true\n", (bad,), 10)


def test_unknown_node_is_rejected_before_execution() -> None:
    fake_exec = FakeExec()

    with pytest.raises(ValueError, match="unknown node"):
        SshBackend(fake_exec).run("spark3", ("true",), 10)

    assert fake_exec.argv == ()


def test_timeout_is_a_bounded_command_result() -> None:
    timeout = subprocess.TimeoutExpired(
        cmd=("ssh",), timeout=3, output=b"partial output", stderr=b"partial error"
    )

    result = SshBackend(FakeExec(timeout), output_limit_bytes=7).run(
        "spark1", ("slow",), 3
    )

    assert result.returncode is None
    assert result.timed_out is True
    assert result.stdout == b"partial"
    assert result.stderr == b"partial"
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_nonzero_remote_exit_is_returned_without_raising() -> None:
    result = SshBackend(
        FakeExec(FakeCompleted(returncode=23, stdout=b"", stderr=b"failed\n"))
    ).run("spark2", ("profile-health",), 10)

    assert result.returncode == 23
    assert result.stderr == b"failed\n"
    assert result.ok is False
    assert result.timed_out is False


def test_oversized_output_is_truncated_and_marked() -> None:
    result = SshBackend(
        FakeExec(FakeCompleted(stdout=b"a" * 20, stderr=b"b" * 21)),
        output_limit_bytes=8,
    ).run("spark1", ("noisy",), 10)

    assert result.stdout == b"a" * 8
    assert result.stderr == b"b" * 8
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


@pytest.mark.parametrize("timeout", [0, -1])
def test_timeout_must_be_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        SshBackend(FakeExec()).run("spark1", ("true",), timeout)
