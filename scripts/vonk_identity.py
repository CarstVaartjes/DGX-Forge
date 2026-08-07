"""Deterministically report legacy identity tokens in a source tree.

Git checkouts scan tracked and untracked files that are not ignored. Other
roots omit repository metadata and generated cache/build directories. Both
modes scan names and UTF-8 text only for regular source files; binary or
encoded artifact names are omitted too. Declared external evidence roots are
scanned but reported separately, so they never fail the owned-source guard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_LEGACY_TOKEN = re.compile("|".join(("sp" + "ark", "d" + "gx")), re.IGNORECASE)
_EXTERNAL_EVIDENCE_ROOTS = (
    Path("manifests"),
    Path("inventory/raw"),
    Path("tests/fixtures/external"),
)
_SKIPPED_DIRECTORY_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
}
_GENERATED_DIRECTORY = re.compile(
    r"(?:^|[._-])(?:cache|caches|build|dist|out|target|coverage|htmlcov|venv|virtualenv|tox)(?:$|[._-])",
    re.IGNORECASE,
)
_BINARY_SUFFIXES = {
    ".7z",
    ".a",
    ".apk",
    ".asc",
    ".bin",
    ".b64",
    ".base64",
    ".bz2",
    ".deb",
    ".dll",
    ".dylib",
    ".elf",
    ".exe",
    ".gz",
    ".img",
    ".iso",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lz4",
    ".lzma",
    ".o",
    ".pem",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar.gz",
    ".tar",
    ".tgz",
    ".whl",
    ".xz",
    ".zip",
    ".zst",
}
_BINARY_MAGICS = (
    b"\x7fELF",
    b"\x1f\x8b",
    b"BZh",
    b"PK\x03\x04",
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"%PDF-",
    b"MZ",
)


def verify(root: Path) -> dict[str, object]:
    """Return a stable inventory of legacy identity tokens under *root*."""

    root = root.resolve()
    owned_matches: list[dict[str, object]] = []
    external_matches: list[dict[str, object]] = []

    git_paths = _git_visible_paths(root)
    if git_paths is not None:
        _scan_git_paths(root, git_paths, owned_matches, external_matches)
    else:
        _scan_directory(root, owned_matches, external_matches)

    owned_matches.sort(key=_match_key)
    external_matches.sort(key=_match_key)
    return {
        "external_matches": external_matches,
        "owned_matches": owned_matches,
        "status": "failed" if owned_matches else "passed",
    }


def _git_visible_paths(root: Path) -> list[Path] | None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=False,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return sorted(
        {Path(os.fsdecode(raw_path)) for raw_path in completed.stdout.split(b"\0") if raw_path},
        key=lambda path: path.as_posix(),
    )


def _scan_git_paths(
    root: Path,
    paths: list[Path],
    owned_matches: list[dict[str, object]],
    external_matches: list[dict[str, object]],
) -> None:
    paths = [path for path in paths if not _is_skipped_path(path)]
    directories = {
        parent
        for relative_path in paths
        for parent in relative_path.parents
        if parent != Path(".") and not _is_skipped_path(parent)
    }
    for relative_path in sorted(directories, key=lambda path: path.as_posix()):
        _append_matches(
            relative_path,
            _path_matches(relative_path),
            owned_matches,
            external_matches,
        )
    for relative_path in paths:
        _scan_file(root / relative_path, relative_path, owned_matches, external_matches)


def _scan_directory(
    root: Path,
    owned_matches: list[dict[str, object]],
    external_matches: list[dict[str, object]],
) -> None:
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names[:] = sorted(name for name in names if not _is_skipped_directory(name))
        for name in names:
            relative_path = Path(directory, name).relative_to(root)
            _append_matches(relative_path, _path_matches(relative_path), owned_matches, external_matches)
        for name in sorted(files):
            path = Path(directory, name)
            relative_path = path.relative_to(root)
            _scan_file(path, relative_path, owned_matches, external_matches)


def _scan_file(
    path: Path,
    relative_path: Path,
    owned_matches: list[dict[str, object]],
    external_matches: list[dict[str, object]],
) -> None:
    if path.is_symlink() or not path.is_file():
        return
    text = _read_text(path)
    if text is None:
        return
    _append_matches(relative_path, _path_matches(relative_path), owned_matches, external_matches)
    _append_matches(relative_path, _matches(relative_path, text), owned_matches, external_matches)


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if _is_binary(path, raw):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_skipped_directory(name: str) -> bool:
    return name in _SKIPPED_DIRECTORY_NAMES or _GENERATED_DIRECTORY.search(name) is not None


def _is_skipped_path(path: Path) -> bool:
    return any(_is_skipped_directory(part) for part in path.parts)


def _is_binary(path: Path, raw: bytes) -> bool:
    suffixes = "".join(path.suffixes).casefold()
    if (
        path.suffix.casefold() in _BINARY_SUFFIXES
        or suffixes in _BINARY_SUFFIXES
        or raw.startswith(_BINARY_MAGICS)
        or b"\0" in raw
    ):
        return True
    sample = raw[:8192]
    if not sample:
        return False
    control_bytes = sum(byte < 32 and byte not in b"\t\n\r\f\b" for byte in sample)
    return control_bytes / len(sample) > 0.05


def _is_external_evidence(path: Path) -> bool:
    return any(path.is_relative_to(evidence_root) for evidence_root in _EXTERNAL_EVIDENCE_ROOTS)


def _append_matches(
    path: Path,
    matches: list[dict[str, object]],
    owned_matches: list[dict[str, object]],
    external_matches: list[dict[str, object]],
) -> None:
    if _is_external_evidence(path):
        external_matches.extend(matches)
    else:
        owned_matches.extend(matches)


def _path_matches(path: Path) -> list[dict[str, object]]:
    text = path.as_posix()
    return [{"line": 0, "path": text, "text": text}] if _LEGACY_TOKEN.search(text) else []


def _matches(path: Path, text: str) -> list[dict[str, object]]:
    return [
        {"line": line_number, "path": path.as_posix(), "text": line}
        for line_number, line in enumerate(text.splitlines(), start=1)
        if _LEGACY_TOKEN.search(line)
    ]


def _match_key(match: dict[str, object]) -> tuple[str, int, str]:
    return (str(match["path"]), int(match["line"]), str(match["text"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the verification report as JSON")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    result = verify(args.root)
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(result["status"])
        for match in result["owned_matches"]:
            print(f'{match["path"]}:{match["line"]}: {match["text"]}')
    return 1 if result["owned_matches"] else 0


if __name__ == "__main__":
    sys.exit(main())
