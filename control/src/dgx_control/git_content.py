"""Bounded reads of immutable files from an exact Git commit."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

_COMMIT = re.compile(r"[0-9a-f]{40}")
_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}")


def read_commit_file(
    repository_root: Path,
    commit: str,
    relative_path: str,
    *,
    maximum_bytes: int = 1_048_576,
) -> bytes:
    path = PurePosixPath(relative_path)
    if (
        _COMMIT.fullmatch(commit) is None
        or _PATH.fullmatch(relative_path) is None
        or path.is_absolute()
        or ".." in path.parts
        or maximum_bytes <= 0
    ):
        raise ValueError("immutable repository file reference is invalid")
    try:
        process = subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=never",
                "-C",
                str(repository_root),
                "cat-file",
                "blob",
                f"{commit}:{relative_path}",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("immutable repository file cannot be read") from error
    if process.returncode != 0 or len(process.stdout) > maximum_bytes:
        raise ValueError("immutable repository file is unavailable or oversized")
    return process.stdout
