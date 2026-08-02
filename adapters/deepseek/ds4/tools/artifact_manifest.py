"""Verify the pinned DS4 GGUF checkpoint pair without following symlinks."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HASH_CHUNK_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_KEYS = frozenset({"schema_version", "artifacts", "total_bytes"})
_ARTIFACT_KEYS = frozenset(
    {"name", "repository", "revision", "path", "size", "sha256"}
)


class ManifestError(ValueError):
    """Raised when a DS4 artifact manifest is malformed or unsafe."""


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ManifestError(f"unsafe manifest path: {value!r}")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ManifestError(f"unsafe manifest path: {value!r}")
    return value


def _require_non_negative_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestError(f"manifest {key} must be a non-negative integer")
    return value


def _require_exact_keys(
    mapping: Mapping[str, Any], expected: frozenset[str], subject: str
) -> None:
    unknown = sorted(set(mapping) - expected)
    missing = sorted(expected - set(mapping))
    if unknown:
        raise ManifestError(f"unknown {subject} keys: {', '.join(unknown)}")
    if missing:
        raise ManifestError(f"missing {subject} keys: {', '.join(missing)}")


def _validate_manifest(manifest: object) -> list[dict[str, Any]]:
    if not isinstance(manifest, Mapping):
        raise ManifestError("manifest must be an object")
    _require_exact_keys(manifest, _MANIFEST_KEYS, "manifest")
    if manifest["schema_version"] != 1:
        raise ManifestError("unsupported manifest schema_version")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ManifestError("manifest must contain exactly two artifacts")

    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise ManifestError("artifact entry must be an object")
        _require_exact_keys(artifact, _ARTIFACT_KEYS, "artifact")
        name = artifact["name"]
        if not isinstance(name, str) or name not in {"base", "drafter"}:
            raise ManifestError("artifact name must be base or drafter")
        if name in seen_names:
            raise ManifestError(f"duplicate artifact name: {name}")
        seen_names.add(name)
        path = _safe_relative_path(artifact["path"])
        if path in seen_paths:
            raise ManifestError(f"duplicate manifest path: {path}")
        seen_paths.add(path)
        repository = artifact["repository"]
        if not isinstance(repository, str) or not repository:
            raise ManifestError(f"artifact repository must be non-empty for {path}")
        revision = artifact["revision"]
        if not isinstance(revision, str) or not _GIT_SHA_RE.fullmatch(revision):
            raise ManifestError(f"artifact revision must be a full Git SHA for {path}")
        size = _require_non_negative_int(artifact, "size")
        sha256 = artifact["sha256"]
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise ManifestError(f"invalid SHA-256 for {path}")
        normalized.append(
            {
                "name": name,
                "repository": repository,
                "revision": revision,
                "path": path,
                "size": size,
                "sha256": sha256,
            }
        )
    if seen_names != {"base", "drafter"}:
        raise ManifestError("manifest must contain base and drafter artifacts")
    total_bytes = _require_non_negative_int(manifest, "total_bytes")
    if total_bytes != sum(artifact["size"] for artifact in normalized):
        raise ManifestError("manifest total_bytes mismatch")
    return sorted(normalized, key=lambda artifact: artifact["path"])


def _open_regular_file(root: Path, relative_path: str) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ManifestError("O_NOFOLLOW is required to verify artifacts safely")
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    directory_flags = root_flags
    file_flags = os.O_RDONLY | nofollow
    root_fd = os.open(root, root_flags)
    current_fd = root_fd
    try:
        components = relative_path.split("/")
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(components[-1], file_flags, dir_fd=current_fd)
    finally:
        os.close(current_fd)
    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
        os.close(file_fd)
        raise OSError(errno.EINVAL, "artifact is not a regular file", relative_path)
    return file_fd


def _hash_file(file_fd: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(file_fd, "rb", closefd=True) as artifact:
        while chunk := artifact.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_status(root: Path, artifact: Mapping[str, Any]) -> str:
    try:
        file_fd = _open_regular_file(root, artifact["path"])
    except FileNotFoundError:
        return "missing"
    except (ManifestError, OSError):
        return "unsafe"
    try:
        if os.fstat(file_fd).st_size != artifact["size"]:
            os.close(file_fd)
            return "size_mismatch"
        actual_sha256 = _hash_file(file_fd)
    except OSError:
        return "unsafe"
    if actual_sha256 != artifact["sha256"]:
        return "sha256_mismatch"
    return "verified"


def verify_manifest(manifest_path: Path, root: Path) -> dict[str, Any]:
    """Validate a manifest and verify every named artifact under ``root``."""

    with manifest_path.open(encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    artifacts = _validate_manifest(manifest)
    results = [
        {"path": artifact["path"], "status": _artifact_status(root, artifact)}
        for artifact in artifacts
    ]
    return {
        "artifacts": results,
        "ok": all(result["status"] == "verified" for result in results),
        "total_bytes": manifest["total_bytes"],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify", help="verify a pinned DS4 artifact pair")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--root", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the artifact verifier and print its deterministic JSON report."""

    args = _parse_args(argv)
    try:
        report = verify_manifest(args.manifest, args.root)
    except (ManifestError, OSError, json.JSONDecodeError) as error:
        json.dump({"error": str(error), "ok": False}, sys.stdout, sort_keys=True)
        print()
        return 2
    json.dump(report, sys.stdout, sort_keys=True)
    print()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
