"""Deterministically report legacy identity tokens in a source tree."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


_LEGACY_TOKEN = re.compile(r"(?:spark|dgx)", re.IGNORECASE)
_EXTERNAL_EVIDENCE_ROOTS = (
    Path("manifests"),
    Path("inventory/raw"),
    Path("tests/fixtures/external"),
)
_SKIPPED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "target",
}


def verify(root: Path) -> dict[str, object]:
    """Return a stable inventory of legacy identity tokens under *root*."""

    root = root.resolve()
    owned_matches: list[dict[str, object]] = []
    external_matches: list[dict[str, object]] = []

    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names[:] = sorted(name for name in names if name not in _SKIPPED_DIRECTORIES)
        for name in sorted(files):
            path = Path(directory, name)
            if path.is_symlink() or not path.is_file():
                continue
            text = _read_text(path)
            if text is None:
                continue
            relative_path = path.relative_to(root)
            matches = _matches(relative_path, text)
            if _is_external_evidence(relative_path):
                external_matches.extend(matches)
            else:
                owned_matches.extend(matches)

    owned_matches.sort(key=_match_key)
    external_matches.sort(key=_match_key)
    return {
        "external_matches": external_matches,
        "owned_matches": owned_matches,
        "status": "failed" if owned_matches else "passed",
    }


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_external_evidence(path: Path) -> bool:
    return any(path.is_relative_to(evidence_root) for evidence_root in _EXTERNAL_EVIDENCE_ROOTS)


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
