from __future__ import annotations

from spark_profiles.ssh_transport import select_transport_binary


def test_native_platform_uses_posix_transport_defaults() -> None:
    available = {"ssh": "/usr/bin/ssh", "scp": "/usr/bin/scp", "ssh.exe": "/mnt/c/ssh.exe"}

    assert select_transport_binary(
        "ssh", environ={}, platform_release="6.8.0-linux", which=available.get
    ) == "ssh"
    assert select_transport_binary(
        "scp", environ={}, platform_release="23.6.0-darwin", which=available.get
    ) == "scp"


def test_wsl_uses_windows_transport_when_available() -> None:
    available = {
        "ssh.exe": "/mnt/c/Windows/System32/OpenSSH/ssh.exe",
        "scp.exe": "/mnt/c/Windows/System32/OpenSSH/scp.exe",
    }

    assert select_transport_binary(
        "ssh",
        environ={"WSL_INTEROP": "/run/WSL/1_interop"},
        platform_release="6.6.87.2-microsoft-standard-WSL2",
        which=available.get,
    ) == "ssh.exe"
    assert select_transport_binary(
        "scp",
        environ={},
        platform_release="6.6.87.2-microsoft-standard-WSL2",
        which=available.get,
    ) == "scp.exe"


def test_wsl_falls_back_to_posix_transport_when_windows_binary_is_unavailable() -> None:
    assert select_transport_binary(
        "ssh",
        environ={"WSL_DISTRO_NAME": "Ubuntu"},
        platform_release="6.6.87.2-microsoft-standard-WSL2",
        which=lambda _: None,
    ) == "ssh"


def test_explicit_transport_overrides_all_platform_defaults() -> None:
    environment = {
        "SPARK_SSH_BIN": "/opt/custom/ssh-wrapper",
        "SPARK_SCP_BIN": "/opt/custom/scp-wrapper",
        "WSL_INTEROP": "/run/WSL/1_interop",
    }

    assert select_transport_binary(
        "ssh", environ=environment, platform_release="microsoft", which=lambda _: "/ignored"
    ) == "/opt/custom/ssh-wrapper"
    assert select_transport_binary(
        "scp", environ=environment, platform_release="microsoft", which=lambda _: "/ignored"
    ) == "/opt/custom/scp-wrapper"
