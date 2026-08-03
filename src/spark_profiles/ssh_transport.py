"""Cross-platform OpenSSH transport selection for developer-machine commands."""

from __future__ import annotations

import os
import platform
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_OVERRIDES = {"ssh": "SPARK_SSH_BIN", "scp": "SPARK_SCP_BIN"}


@dataclass(frozen=True)
class TransportSelection:
    """Selected command plus the local path syntax it consumes."""

    binary: str
    path_style: str


def select_transport(
    command: str,
    *,
    environ: Mapping[str, str] | None = None,
    platform_release: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> TransportSelection:
    """Select native OpenSSH, a WSL Windows bridge, or an explicit wrapper."""
    try:
        override_name = _OVERRIDES[command]
    except KeyError as error:
        raise ValueError(f"unsupported SSH transport command: {command}") from error
    environment = os.environ if environ is None else environ
    release = platform.release() if platform_release is None else platform_release
    is_wsl = bool(environment.get("WSL_INTEROP") or environment.get("WSL_DISTRO_NAME"))
    is_wsl = is_wsl or "microsoft" in release.lower()
    windows_command = f"{command}.exe"
    use_windows_bridge = bool(is_wsl and which(windows_command))

    path_style = environment.get("SPARK_SCP_PATH_STYLE") if command == "scp" else None
    if path_style is not None and path_style not in {"posix", "windows"}:
        raise ValueError("SPARK_SCP_PATH_STYLE must be posix or windows")
    if override := environment.get(override_name):
        return TransportSelection(override, path_style or "posix")
    if use_windows_bridge:
        default_style = "windows" if command == "scp" else "posix"
        return TransportSelection(windows_command, path_style or default_style)
    return TransportSelection(command, path_style or "posix")


def select_transport_binary(
    command: str,
    *,
    environ: Mapping[str, str] | None = None,
    platform_release: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Return only the binary for callers that do not transfer local paths."""
    return select_transport(
        command,
        environ=environ,
        platform_release=platform_release,
        which=which,
    ).binary
