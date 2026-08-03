"""Host-local maintenance and encrypted backup primitives."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath


class OfflineConflict(RuntimeError):
    pass


class BackupError(RuntimeError):
    pass


class OfflineLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> "OfflineLock":
        if self._descriptor is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise OfflineConflict("another offline maintenance operation is active") from None
        self._descriptor = descriptor
        return self

    def __exit__(self, *_args: object) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None


class OnlineLock:
    """Shared lifetime lock held by every online API/worker process."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> "OnlineLock":
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        self._descriptor = descriptor
        return self

    def __exit__(self, *_args: object) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None


def require_offline(state_path: Path, *, probe: Callable[[], bool]) -> OfflineLock:
    lock = OfflineLock(state_path / "offline.lock")
    lock.__enter__()
    if probe():
        lock.__exit__()
        raise OfflineConflict("control plane is running; stop API and worker first")
    return lock


def _files(paths: Sequence[Path]) -> list[tuple[str, bytes]]:
    collected: list[tuple[str, bytes]] = []
    for source in paths:
        if source.is_symlink() or not source.exists():
            raise BackupError(f"backup source is unsafe or missing: {source}")
        if source.is_file():
            collected.append((source.name, source.read_bytes()))
            continue
        for child in sorted(source.rglob("*")):
            if child.is_symlink():
                raise BackupError(f"backup source contains a symlink: {child}")
            if child.is_file():
                collected.append((f"{source.name}/{child.relative_to(source).as_posix()}", child.read_bytes()))
    return collected


def _run_transform(command: Sequence[str], content: bytes, action: str) -> bytes:
    if not command:
        raise BackupError(f"external {action} command is required")
    completed = subprocess.run(command, input=content, capture_output=True, check=False)
    if completed.returncode != 0:
        raise BackupError(f"external {action} command failed")
    return completed.stdout


def create_backup(
    database_dump: Path,
    config_paths: Sequence[Path],
    output: Path,
    *,
    encrypt_command: Sequence[str],
) -> None:
    if output.exists() or output.is_symlink():
        raise BackupError("backup output must be a new path")
    entries = sorted(_files((database_dump, *config_paths)))
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in entries}
    manifest = json.dumps(
        {"format": "dgx-control-backup-v1", "files": hashes},
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    archive_content = _archive_bytes([*entries, ("manifest.json", manifest)])
    encrypted = _run_transform(encrypt_command, archive_content, "encryption")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(encrypted)
        destination.flush()
        os.fsync(destination.fileno())


def _archive_bytes(entries: Sequence[tuple[str, bytes]]) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            info.mtime = 0
            bundle.addfile(info, io.BytesIO(content))
    return archive.getvalue()


def _verified_files(backup: Path, decrypt_command: Sequence[str]) -> tuple[dict[str, object], dict[str, bytes]]:
    decrypted = _run_transform(decrypt_command, backup.read_bytes(), "decryption")
    try:
        with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:") as bundle:
            members = bundle.getmembers()
            for member in members:
                path = PurePosixPath(member.name)
                if member.issym() or member.islnk() or member.isdir() or path.is_absolute() or ".." in path.parts:
                    raise BackupError("backup archive contains an unsafe member")
            files = {member.name: bundle.extractfile(member).read() for member in members}
        manifest_raw = files.pop("manifest.json")
        manifest = json.loads(manifest_raw)
        if manifest.get("format") != "dgx-control-backup-v1" or not isinstance(manifest.get("files"), dict):
            raise BackupError("backup manifest is invalid")
        expected = manifest["files"]
        if set(expected) != set(files):
            raise BackupError("backup file set differs from manifest")
        for name, content in files.items():
            if expected[name] != hashlib.sha256(content).hexdigest():
                raise BackupError("backup checksum verification failed")
        canonical_entries = [
            *((name, files[name]) for name in sorted(files)),
            ("manifest.json", manifest_raw),
        ]
        if decrypted != _archive_bytes(canonical_entries):
            raise BackupError("backup archive is not canonical or was modified")
        return manifest, files
    except BackupError:
        raise
    except Exception as error:
        raise BackupError("backup archive or manifest is unreadable") from error


def inspect_backup(backup: Path, *, decrypt_command: Sequence[str]) -> dict[str, object]:
    return _verified_files(backup, decrypt_command)[0]


def extract_backup(backup: Path, destination: Path, *, decrypt_command: Sequence[str]) -> dict[str, object]:
    manifest, files = _verified_files(backup, decrypt_command)
    if destination.exists():
        raise BackupError("restore staging destination must not already exist")
    destination.mkdir(parents=True, mode=0o700)
    for name, content in files.items():
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
    return manifest


def _api_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dgx-control-offline")
    parser.add_argument("--state-path", type=Path, default=Path("/srv/dgx-forge/state"))
    parser.add_argument("--health-url", default="https://127.0.0.1/api/v1/healthz")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    init = commands.add_parser("init")
    init.add_argument("--repository", type=Path, required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--database-dump", type=Path, required=True)
    backup.add_argument("--config", type=Path, action="append", default=[])
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--encrypt-command", required=True)
    inspect = commands.add_parser("inspect-backup")
    inspect.add_argument("backup", type=Path)
    inspect.add_argument("--decrypt-command", required=True)
    extract = commands.add_parser("extract-backup")
    extract.add_argument("backup", type=Path)
    extract.add_argument("destination", type=Path)
    extract.add_argument("--decrypt-command", required=True)
    commands.add_parser("migrate")
    admin = commands.add_parser("create-admin")
    admin.add_argument("--subject", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps({"api_running": _api_running(args.health_url)}))
            return 0
        if args.command == "backup":
            create_backup(args.database_dump, args.config, args.output, encrypt_command=shlex.split(args.encrypt_command))
            return 0
        if args.command == "inspect-backup":
            print(json.dumps(inspect_backup(args.backup, decrypt_command=shlex.split(args.decrypt_command)), sort_keys=True))
            return 0
        if args.command == "extract-backup":
            extract_backup(args.backup, args.destination, decrypt_command=shlex.split(args.decrypt_command))
            return 0
        lock = require_offline(args.state_path, probe=lambda: _api_running(args.health_url))
        with lock:
            if args.command == "init":
                for path in (args.state_path, args.repository):
                    path.mkdir(parents=True, exist_ok=True, mode=0o700)
                return 0
            if args.command == "migrate":
                from alembic import command
                from alembic.config import Config
                from .settings import Settings

                settings = Settings.from_env_and_secrets()
                config = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
                config.set_main_option("sqlalchemy.url", settings.database_url)
                command.upgrade(config, "head")
                return 0
            if args.command == "create-admin":
                from datetime import UTC, datetime
                from .db import build_engine, session_factory
                from .models import User
                from .settings import Settings

                settings = Settings.from_env_and_secrets()
                sessions = session_factory(build_engine(settings.database_url))
                with sessions.begin() as session:
                    session.add(User(subject=args.subject, role="administrator"))
                return 0
            raise BackupError(f"unsupported offline command: {args.command}")
    except OfflineConflict as error:
        print(f"dgx-control-offline: {error}", file=__import__("sys").stderr)
        return 3
    except (BackupError, OSError) as error:
        print(f"dgx-control-offline: {error}", file=__import__("sys").stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
