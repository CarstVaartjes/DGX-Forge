"""Persisted python-tuf trust boundary for DGX-Forge platform releases."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tuf.api.exceptions import DownloadError, RepositoryError
from tuf.api.metadata import Metadata
from tuf.ngclient import FetcherInterface, Updater
from tuf.ngclient.config import UpdaterConfig

_TARGET_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_MAX_TARGET_BYTES = 16 * 1024 * 1024
_LOCK_NAME = ".platform-updater.lock"
_STATE_NAME = "trusted-state.json"


class UpdateTrustError(RuntimeError):
    """Platform update metadata or target authorization failed closed."""


@dataclass(frozen=True)
class TrustedTarget:
    name: str
    length: int
    sha256: str
    data: bytes


class UpdateTrust:
    """Refresh one platform TUF repository and return verified target bytes."""

    def __init__(
        self,
        metadata_root: Path,
        target_root: Path,
        metadata_base_url: str,
        target_base_url: str,
        bootstrap_root: bytes,
        fetcher: FetcherInterface,
    ) -> None:
        self._metadata_root = Path(metadata_root)
        self._target_root = Path(target_root)
        self._metadata_base_url = _repository_url(metadata_base_url, "metadata")
        self._target_base_url = _repository_url(target_base_url, "targets")
        if _origin(self._metadata_base_url) != _origin(self._target_base_url):
            raise UpdateTrustError("platform TUF routes must share one HTTPS origin")
        if not isinstance(bootstrap_root, bytes) or not bootstrap_root:
            raise UpdateTrustError("platform TUF bootstrap root is invalid")
        self._bootstrap_root = bytes(bootstrap_root)
        self._fetcher = fetcher
        self._updater: Updater | None = None
        self._thread_lock = threading.Lock()

    def refresh(self) -> None:
        with self._thread_lock, self._exclusive_cache():
            try:
                bootstrap = (
                    None
                    if (self._metadata_root / "root.json").is_file()
                    else self._bootstrap_root
                )
                updater = Updater(
                    str(self._metadata_root),
                    self._metadata_base_url,
                    str(self._target_root),
                    self._target_base_url,
                    self._fetcher,
                    UpdaterConfig(
                        max_root_rotations=32,
                        max_delegations=16,
                        root_max_length=256 * 1024,
                        timestamp_max_length=64 * 1024,
                        snapshot_max_length=1024 * 1024,
                        targets_max_length=2 * 1024 * 1024,
                        prefix_targets_with_hash=False,
                        app_user_agent="dgx-forge-platform-updater/0.1.0",
                    ),
                    bootstrap=bootstrap,
                )
                updater.refresh()
                state = _metadata_versions(self._metadata_root)
                _verify_version_floor(self._metadata_root / _STATE_NAME, state)
                _write_state(self._metadata_root, state)
                self._updater = updater
            except UpdateTrustError:
                raise
            except (DownloadError, RepositoryError, OSError, ValueError, TypeError) as error:
                raise UpdateTrustError("platform TUF refresh failed") from error

    def trusted_target(self, name: str) -> TrustedTarget:
        if not isinstance(name, str) or _TARGET_NAME.fullmatch(name) is None:
            raise UpdateTrustError("platform TUF target name is invalid")
        with self._thread_lock, self._exclusive_cache():
            updater = self._updater
            if updater is None:
                raise UpdateTrustError("platform TUF metadata has not been refreshed")
            temporary: Path | None = None
            try:
                target = updater.get_targetinfo(name)
                if target is None:
                    raise UpdateTrustError("platform TUF target is not authorized")
                if (
                    isinstance(target.length, bool)
                    or not 0 < target.length <= _MAX_TARGET_BYTES
                    or set(target.hashes) != {"sha256"}
                    or not re.fullmatch(r"[0-9a-f]{64}", target.hashes["sha256"])
                ):
                    raise UpdateTrustError("platform TUF target bounds are invalid")
                descriptor, raw_path = tempfile.mkstemp(
                    prefix=".platform-target-", dir=self._target_root
                )
                os.close(descriptor)
                temporary = Path(raw_path)
                updater.download_target(target, str(temporary))
                data = temporary.read_bytes()
                if (
                    len(data) != target.length
                    or hashlib.sha256(data).hexdigest() != target.hashes["sha256"]
                ):
                    raise UpdateTrustError("platform TUF target bytes are invalid")
                return TrustedTarget(
                    name=name,
                    length=target.length,
                    sha256=target.hashes["sha256"],
                    data=data,
                )
            except UpdateTrustError:
                raise
            except (DownloadError, RepositoryError, OSError, ValueError, TypeError) as error:
                raise UpdateTrustError("platform TUF target verification failed") from error
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

    @contextmanager
    def _exclusive_cache(self):
        _secure_directory(self._metadata_root)
        _secure_directory(self._target_root)
        lock_path = self._metadata_root / _LOCK_NAME
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as error:
            raise UpdateTrustError("platform TUF cache lock is unsafe") from error
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise UpdateTrustError("platform TUF cache is already in use") from error
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)


def _repository_url(value: str, kind: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise UpdateTrustError("platform TUF repository URL is invalid") from error
    origin = f"https://{parsed.hostname or ''}"
    if port is not None:
        origin += f":{port}"
    if (
        not isinstance(value, str)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or value != f"{origin}/platform/{kind}/"
    ):
        raise UpdateTrustError("platform TUF repository URL is invalid")
    return value


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}"


def _secure_directory(path: Path) -> None:
    if not path.is_absolute():
        raise UpdateTrustError("platform TUF cache path must be absolute")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise UpdateTrustError("platform TUF cache directory is unsafe") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise UpdateTrustError("platform TUF cache directory is unsafe")
    try:
        os.chmod(path, 0o700)
    except OSError as error:
        raise UpdateTrustError("platform TUF cache directory is unsafe") from error


def _metadata_versions(root: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for role in ("root", "timestamp", "snapshot", "targets"):
        raw = (root / f"{role}.json").read_bytes()
        metadata = Metadata.from_bytes(raw)
        version = metadata.signed.version
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise UpdateTrustError("platform TUF metadata version is invalid")
        result[role] = version
    return result


def _verify_version_floor(path: Path, candidate: dict[str, int]) -> None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    try:
        current = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateTrustError("platform TUF version floor is invalid") from error
    if (
        not isinstance(current, dict)
        or set(current) != {"root", "timestamp", "snapshot", "targets"}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in current.values()
        )
    ):
        raise UpdateTrustError("platform TUF version floor is invalid")
    if any(candidate[role] < current[role] for role in current):
        raise UpdateTrustError("platform TUF metadata rollback was rejected")


def _write_state(root: Path, state: dict[str, int]) -> None:
    content = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = root / _STATE_NAME
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{_STATE_NAME}.", suffix=".new", dir=root
    )
    temporary = Path(raw_temporary)
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise UpdateTrustError("platform TUF version floor write was incomplete")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
