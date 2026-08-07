from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cluster_profiles.fleet import ManagementEndpoint
from cluster_profiles.install.remote import (
    OpenSshInstallTransport,
    RemoteResult,
    UnsafeInstallArgument,
)


class RecordingExec:
    def __init__(self, *, stdout: bytes = b"ok\n", stderr: bytes = b"") -> None:
        self.calls: list[dict[str, object]] = []
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": tuple(argv), **kwargs})
        return subprocess.CompletedProcess(argv, 0, self.stdout, self.stderr)


@pytest.fixture
def endpoint() -> ManagementEndpoint:
    return ManagementEndpoint(
        host="alpha.local",
        user="operator",
        port=2222,
        credential_ref="secret://ssh/admin",
    )


def test_remote_command_pins_noninteractive_safe_options(
    endpoint: ManagementEndpoint,
) -> None:
    execute = RecordingExec()
    transport = OpenSshInstallTransport(
        execute=execute,
        ssh_bin="/usr/bin/ssh",
        scp_bin="/usr/bin/scp",
    )

    result = transport.run(endpoint, ("hostname",), b"", timeout=10)

    argv = execute.calls[0]["argv"]
    assert argv[0] == "/usr/bin/ssh"
    assert "BatchMode=yes" in argv
    assert "ForwardAgent=no" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "ConnectTimeout=10" in argv
    assert ("-p", "2222") == argv[argv.index("-p") : argv.index("-p") + 2]
    assert "operator@alpha.local" in argv
    assert execute.calls[0]["input"] == b""
    assert execute.calls[0]["shell"] is False
    assert result == RemoteResult(0, b"ok\n", b"")


def test_install_transport_uses_shared_binary_selector(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: ManagementEndpoint,
) -> None:
    monkeypatch.setenv("VONK_SSH_BIN", "/opt/shared/ssh")
    monkeypatch.setenv("VONK_SCP_BIN", "/opt/shared/scp")
    execute = RecordingExec()
    transport = OpenSshInstallTransport(execute=execute)

    transport.run(endpoint, ("true",), b"", 10)

    assert execute.calls[0]["argv"][0] == "/opt/shared/ssh"


def test_remote_command_quotes_each_remote_argument(
    endpoint: ManagementEndpoint,
) -> None:
    execute = RecordingExec()
    transport = OpenSshInstallTransport(execute=execute)

    transport.run(
        endpoint,
        ("printf", "%s", "value with spaces; touch /tmp/pwned"),
        b"",
        timeout=10,
    )

    remote_command = execute.calls[0]["argv"][-1]
    assert remote_command == "printf %s 'value with spaces; touch /tmp/pwned'"


@pytest.mark.parametrize(
    "argv",
    [
        ("--password=secret",),
        ("private-key", "value"),
        ("Authorization: Bearer token",),
        ("safe", "value\x00bad"),
        (),
    ],
)
def test_remote_boundary_rejects_sensitive_empty_or_nul_arguments(
    endpoint: ManagementEndpoint,
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(UnsafeInstallArgument):
        OpenSshInstallTransport().run(endpoint, argv, b"", 10)


def test_remote_boundary_bounds_stdout_and_stderr(
    endpoint: ManagementEndpoint,
) -> None:
    execute = RecordingExec(stdout=b"a" * 20, stderr=b"b" * 20)
    transport = OpenSshInstallTransport(execute=execute, output_limit_bytes=8)

    result = transport.run(endpoint, ("true",), b"", 10)

    assert result.stdout == b"a" * 8
    assert result.stderr == b"b" * 8


def test_copy_uses_scp_without_forwarding_and_rejects_unsafe_destination(
    tmp_path: Path,
    endpoint: ManagementEndpoint,
) -> None:
    source = tmp_path / "installer"
    source.write_text("payload")
    execute = RecordingExec()
    transport = OpenSshInstallTransport(execute=execute, scp_bin="/usr/bin/scp")

    result = transport.copy(endpoint, source, "/tmp/vonk-installer", mode=0o755)

    argv = execute.calls[0]["argv"]
    assert argv[0] == "/usr/bin/scp"
    assert "ForwardAgent=no" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert argv[-2] == str(source)
    assert argv[-1] == "operator@alpha.local:/tmp/vonk-installer"
    assert len(execute.calls) == 2
    assert execute.calls[1]["argv"][-1] == "chmod 0755 -- /tmp/vonk-installer"
    assert result.returncode == 0

    with pytest.raises(UnsafeInstallArgument):
        transport.copy(endpoint, source, "/tmp/file;reboot", mode=0o755)


def test_copy_rejects_symlink_source_and_unsupported_mode(
    tmp_path: Path,
    endpoint: ManagementEndpoint,
) -> None:
    source = tmp_path / "source"
    source.write_text("payload")
    symlink = tmp_path / "linked"
    symlink.symlink_to(source)
    transport = OpenSshInstallTransport()

    with pytest.raises(UnsafeInstallArgument, match="regular non-symlink"):
        transport.copy(endpoint, symlink, "/tmp/file", mode=0o644)
    with pytest.raises(UnsafeInstallArgument, match="mode"):
        transport.copy(endpoint, source, "/tmp/file", mode=0o777)
