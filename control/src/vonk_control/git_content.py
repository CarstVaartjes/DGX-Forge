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
    base = (
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-C",
        str(repository_root),
        "cat-file",
    )
    object_reference = f"{commit}:{relative_path}"

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
        )

    try:
        size_process = run(base + ("-s", object_reference))
        size_text = size_process.stdout.decode("ascii", "strict").strip()
        size = int(size_text)
        if size_process.returncode != 0 or size < 0 or size > maximum_bytes:
            raise ValueError("immutable repository file is unavailable or oversized")
        process = run(base + ("blob", object_reference))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("immutable repository file cannot be read") from error
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("immutable repository file is unavailable or oversized") from error
    if process.returncode != 0 or len(process.stdout) != size:
        raise ValueError("immutable repository file is unavailable or oversized")
    return process.stdout
