"""Cross-platform OpenSSH transport selection for developer-machine commands."""

from __future__ import annotations

import os
import platform
import shutil
from collections.abc import Callable, Mapping

_OVERRIDES = {"ssh": "SPARK_SSH_BIN", "scp": "SPARK_SCP_BIN"}


def select_transport_binary(
    command: str,
    *,
    environ: Mapping[str, str] | None = None,
    platform_release: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Select native OpenSSH, a WSL Windows bridge, or an explicit override."""
    try:
        override_name = _OVERRIDES[command]
    except KeyError as error:
        raise ValueError(f"unsupported SSH transport command: {command}") from error
    environment = os.environ if environ is None else environ
    if override := environment.get(override_name):
        return override
    release = platform.release() if platform_release is None else platform_release
    is_wsl = bool(environment.get("WSL_INTEROP") or environment.get("WSL_DISTRO_NAME"))
    is_wsl = is_wsl or "microsoft" in release.lower()
    windows_command = f"{command}.exe"
    if is_wsl and which(windows_command):
        return windows_command
    return command
