"""Host-local maintenance and encrypted backup primitives."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Self

from tuf.ngclient import Urllib3Fetcher

from spark_profiles.platform_release import PlatformRelease, PlatformReleaseError
from spark_profiles.update_trust import UpdateTrust, UpdateTrustError

from .upgrade import (
    AmbiguousMigrationError,
    ControlUpgrade,
    UpgradeConflict,
    UpgradeError,
    UpgradeRecoveryRequired,
)


class OfflineConflict(RuntimeError):
    pass


class BackupError(RuntimeError):
    pass


class HostUpgradeBoundary:
    """Fixed-argv Docker/Compose boundary for a NAS control-host upgrade."""

    def __init__(
        self,
        *,
        state_root: Path,
        compose_file: Path,
        backup_script: Path,
        encrypt_command: str,
        health_url: str,
    ) -> None:
        self._state_root = Path(state_root)
        self._compose_file = Path(compose_file)
        self._backup_script = Path(backup_script)
        self._encrypt_command = encrypt_command
        self._health_url = health_url
        self._environment: dict[str, str] = {}
        if not encrypt_command:
            raise BackupError("DGX_BACKUP_ENCRYPT_COMMAND is required for upgrade")
        for path, label in (
            (self._compose_file, "Compose file"),
            (self._backup_script, "backup script"),
        ):
            if path.is_symlink() or not path.is_file():
                raise BackupError(f"{label} is unsafe or missing")

    def control_is_running(self) -> bool:
        output = self._run((*self._compose(), "ps", "--status", "running", "--services"))
        services = set(output.decode("utf-8").splitlines())
        return bool(services & {"control-api", "control-worker"})

    def available_bytes(self) -> int:
        existing = self._state_root
        while not existing.exists():
            if existing.parent == existing:
                raise BackupError("upgrade state filesystem is unavailable")
            existing = existing.parent
        return shutil.disk_usage(existing).free

    def pull(self, references: tuple[str, ...]) -> None:
        for reference in references:
            self._run(("docker", "pull", reference))

    def render_compose(self, environment: dict[str, str]) -> bytes:
        self._environment = dict(environment)
        return self._run((*self._compose(), "config"), environment=self._environment)

    def backup(self, generation_id: str) -> dict[str, object]:
        backup_root = self._state_root / "backups"
        backup_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        output = backup_root / f"{generation_id}.age"
        self._run((str(self._backup_script), str(output), self._encrypt_command))
        if output.is_symlink() or not output.is_file():
            raise BackupError("upgrade backup was not created")
        content = output.read_bytes()
        return {
            "path": str(output),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def stop_worker(self) -> None:
        self._run((*self._compose(), "stop", "control-worker"), environment=self._environment)

    def migrate(self, revision: str) -> None:
        try:
            self._run(
                (
                    *self._compose(),
                    "run",
                    "--rm",
                    "control-api",
                    "python",
                    "-m",
                    "alembic",
                    "upgrade",
                    revision,
                ),
                environment=self._environment,
            )
        except BackupError as error:
            raise AmbiguousMigrationError(
                "database migration command failed and its outcome is unknown"
            ) from error

    def start_api(self, generation_path: Path) -> None:
        self._environment = _read_generation_environment(generation_path)
        self._run((*self._compose(), "up", "-d", "control-api"), environment=self._environment)

    def readiness(self) -> dict[str, object]:
        for attempt in range(30):
            if _api_running(self._health_url):
                return {"status": "ready", "probe": "caddy", "attempt": attempt + 1}
            time.sleep(1)
        from .upgrade import UpgradeReadinessError

        raise UpgradeReadinessError("candidate control API readiness deadline elapsed")

    def start_worker(self) -> None:
        self._run((*self._compose(), "up", "-d", "control-worker"), environment=self._environment)

    def stop_api(self) -> None:
        self._run((*self._compose(), "stop", "control-api"), environment=self._environment)

    def restore_generation(self, generation_path: Path) -> None:
        self._environment = _read_generation_environment(generation_path)
        self._run(
            (*self._compose(), "up", "-d", "control-api", "control-worker"),
            environment=self._environment,
        )

    def _compose(self) -> tuple[str, ...]:
        return ("docker", "compose", "-f", str(self._compose_file))

    def _run(
        self,
        argv: Sequence[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> bytes:
        completed = subprocess.run(
            tuple(argv),
            capture_output=True,
            check=False,
            env={**os.environ, **(environment or {})},
        )
        if completed.returncode != 0:
            raise BackupError(f"upgrade command failed: {shlex.join(argv)}")
        if len(completed.stdout) > 1024 * 1024 or len(completed.stderr) > 1024 * 1024:
            raise BackupError("upgrade command output exceeded its bound")
        return completed.stdout


class _PlanOnlyUpgradeBoundary:
    def __getattr__(self, name: str):
        raise UpgradeConflict(f"dry-run boundary cannot execute {name}")


class OfflineLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> Self:
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

    def __enter__(self) -> Self:
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


def _read_generation_environment(generation_path: Path) -> dict[str, str]:
    path = generation_path / "platform.env"
    if path.is_symlink() or not path.is_file():
        raise BackupError("control generation environment is unsafe or missing")
    expected = {
        "CONTROL_API_IMAGE",
        "CONTROL_WORKER_IMAGE",
        "DGX_PLATFORM_BUILD_DIGEST",
        "DGX_PLATFORM_RELEASE_DIGEST",
        "DGX_PLATFORM_VERSION",
    }
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if not separator or name not in expected or name in values or not value:
            raise BackupError("control generation environment is invalid")
        values[name] = value
    if set(values) != expected:
        raise BackupError("control generation environment is incomplete")
    return values


def _load_trusted_release(path: Path, state_root: Path) -> PlatformRelease:
    bootstrap_name = "DGX_PLATFORM_TUF_ROOT"
    bootstrap_value = os.environ.get(bootstrap_name, "")
    metadata_url = os.environ.get("DGX_PLATFORM_TUF_METADATA_URL", "")
    target_url = os.environ.get("DGX_PLATFORM_TUF_TARGET_URL", "")
    if not bootstrap_value:
        raise BackupError(f"{bootstrap_name} is required for upgrade --apply")
    bootstrap = Path(bootstrap_value)
    if bootstrap.is_symlink() or not bootstrap.is_file():
        raise BackupError(f"{bootstrap_name} must name a regular non-symlink file")
    if not metadata_url:
        raise BackupError("DGX_PLATFORM_TUF_METADATA_URL is required for upgrade --apply")
    if not target_url:
        raise BackupError("DGX_PLATFORM_TUF_TARGET_URL is required for upgrade --apply")
    if path.is_symlink() or not path.is_file():
        raise BackupError("platform release target is unsafe or missing")
    trust_root = state_root / "platform-tuf"
    trust = UpdateTrust(
        metadata_root=trust_root / "metadata",
        target_root=trust_root / "targets",
        metadata_base_url=metadata_url,
        target_base_url=target_url,
        bootstrap_root=bootstrap.read_bytes(),
        fetcher=Urllib3Fetcher(),
    )
    trust.refresh()
    target = trust.trusted_target(path.name)
    if target.data != path.read_bytes():
        raise BackupError("platform release differs from its TUF-authorized target")
    return PlatformRelease.load(path)


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
    repository_root = Path(__file__).resolve().parents[3]
    upgrade = commands.add_parser("upgrade")
    upgrade.add_argument("--release", type=Path, required=True)
    upgrade.add_argument("--apply", action="store_true")
    upgrade.add_argument(
        "--compose-file",
        type=Path,
        default=repository_root / "deploy/compose/compose.yaml",
    )
    upgrade.add_argument(
        "--backup-script",
        type=Path,
        default=repository_root / "deploy/compose/bin/backup-control-plane",
    )
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--generation", required=True)
    rollback.add_argument("--apply", action="store_true")
    rollback.add_argument(
        "--compose-file",
        type=Path,
        default=repository_root / "deploy/compose/compose.yaml",
    )
    rollback.add_argument(
        "--backup-script",
        type=Path,
        default=repository_root / "deploy/compose/bin/backup-control-plane",
    )
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
        if args.command in {"upgrade", "rollback"}:
            if args.apply:
                boundary = HostUpgradeBoundary(
                    state_root=args.state_path,
                    compose_file=args.compose_file,
                    backup_script=args.backup_script,
                    encrypt_command=os.environ.get("DGX_BACKUP_ENCRYPT_COMMAND", ""),
                    health_url=args.health_url,
                )
            else:
                boundary = _PlanOnlyUpgradeBoundary()
            service = ControlUpgrade(args.state_path, boundary)
            if args.command == "upgrade":
                release = (
                    _load_trusted_release(args.release, args.state_path)
                    if args.apply
                    else PlatformRelease.load(args.release)
                )
                plan = service.plan(release)
                if not args.apply:
                    print(json.dumps({**asdict(plan), "mode": "plan"}, sort_keys=True))
                    return 0
                print(json.dumps(asdict(service.apply(plan, release)), sort_keys=True))
                return 0
            active = service.active_generation()
            if not args.apply:
                print(
                    json.dumps(
                        {
                            "active_generation": active,
                            "mode": "plan",
                            "target_generation": args.generation,
                        },
                        sort_keys=True,
                    )
                )
                return 0
            print(json.dumps(asdict(service.rollback(args.generation)), sort_keys=True))
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
    except UpgradeRecoveryRequired as error:
        print(f"dgx-control-offline: {error}", file=__import__("sys").stderr)
        return 4
    except (
        BackupError,
        OSError,
        PlatformReleaseError,
        UpdateTrustError,
        UpgradeError,
    ) as error:
        print(f"dgx-control-offline: {error}", file=__import__("sys").stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
