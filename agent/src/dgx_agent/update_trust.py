"""Single-writer python-tuf trust boundary for signed Spark releases."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import ssl
import stat
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import urllib3
from tuf.api.exceptions import DownloadError, DownloadHTTPError, RepositoryError
from tuf.ngclient import FetcherInterface, Updater
from tuf.ngclient.config import UpdaterConfig

from .deadlines import DeadlineBindingError, MonotonicDeadline
from .releases import (
    ReleaseDescriptor,
    ReleaseRequest,
    ReleaseValidationError,
    semantic_version,
)

_BOOTSTRAP_MARKER = ".bootstrap-established"
_LOCK_NAME = ".updater.lock"
_TARGET_LIMIT = 1024 * 1024
_TUF_FILE = re.compile(
    r"(?:[1-9][0-9]*\.root|timestamp|snapshot|targets|[a-z0-9][a-z0-9._-]{0,126})\.json\Z"
)
_TARGET_FILE = re.compile(r"[a-z0-9][a-z0-9._-]{0,126}\Z")


class TUFTrustError(RuntimeError):
    """Signed release authorization failed closed."""


def _interruptible_tuf_call[T](
    deadline: MonotonicDeadline,
    operation: Callable[[], T],
) -> T:
    """Interrupt python-tuf Python work at line boundaries using this thread only."""
    previous_trace = sys.gettrace()

    def deadline_trace(frame: Any, event: str, argument: Any) -> Any:
        _tuf_deadline(deadline)
        return deadline_trace

    _tuf_deadline(deadline)
    sys.settrace(deadline_trace)
    try:
        return operation()
    finally:
        sys.settrace(previous_trace)


class BoundedHTTPSFetcher(FetcherInterface):
    """Direct, no-redirect HTTPS transport fixed to the control TUF routes."""

    def __init__(
        self,
        control_origin: str,
        ssl_context: ssl.SSLContext,
        *,
        connect_timeout: float = 3.0,
        read_timeout: float = 10.0,
        pool: Any | None = None,
    ) -> None:
        parsed = urlsplit(control_origin)
        try:
            port = parsed.port
        except ValueError as error:
            raise TUFTrustError("TUF control origin is invalid") from error
        canonical = f"https://{parsed.hostname or ''}"
        if port is not None:
            canonical += f":{port}"
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or canonical != control_origin
        ):
            raise TUFTrustError("TUF control origin is invalid")
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 < float(value) <= 60
            for value in (connect_timeout, read_timeout)
        ):
            raise TUFTrustError("TUF HTTPS timeout is invalid")
        self._origin = canonical
        self._deadline = float("inf")
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._pool = pool or urllib3.PoolManager(
            num_pools=1,
            maxsize=1,
            block=True,
            ssl_context=ssl_context,
            retries=False,
        )

    def set_deadline(self, absolute_monotonic: float) -> None:
        if not isinstance(absolute_monotonic, (int, float)) or absolute_monotonic <= time.monotonic():
            raise TUFTrustError("TUF fetch deadline has elapsed")
        self._deadline = float(absolute_monotonic)

    def _fetch(self, url: str):
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path
        if (
            origin != self._origin
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or not _allowed_tuf_path(path)
        ):
            raise DownloadError("TUF URL is outside the reviewed route")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadError("TUF fetch deadline elapsed")
        timeout = urllib3.Timeout(
            connect=min(self._connect_timeout, remaining),
            read=min(self._read_timeout, remaining, 0.25),
            total=remaining,
        )
        response = self._pool.request(
            "GET",
            url,
            headers={"User-Agent": "dgx-forge-agent/0.1.0"},
            redirect=False,
            retries=False,
            preload_content=False,
            timeout=timeout,
        )
        try:
            if response.status != 200:
                raise DownloadHTTPError("TUF HTTPS request failed", response.status)
            while True:
                if time.monotonic() >= self._deadline:
                    raise DownloadError("TUF fetch deadline elapsed")
                reader = getattr(response, "read1", response.read)
                chunk = reader(64 * 1024)
                if time.monotonic() >= self._deadline:
                    raise DownloadError("TUF fetch deadline elapsed")
                if not chunk:
                    break
                yield chunk
        finally:
            response.release_conn()


class TUFReleaseTrust:
    """Authorize one release through a fresh, exclusively locked Updater."""

    def __init__(
        self,
        metadata_root: Path,
        target_root: Path,
        metadata_base_url: str,
        target_base_url: str,
        bootstrap_root: bytes,
        fetcher: FetcherInterface,
        registry_origin: str,
        repository: str,
        architecture: str,
    ) -> None:
        self._metadata_root = Path(metadata_root)
        self._target_root = Path(target_root)
        self._metadata_base_url = metadata_base_url
        self._target_base_url = target_base_url
        self._bootstrap_root = bytes(bootstrap_root)
        if not callable(getattr(fetcher, "set_deadline", None)):
            raise TUFTrustError("TUF fetcher does not enforce claim deadlines")
        self._fetcher = fetcher
        self._registry_origin = registry_origin
        self._repository = repository
        self._architecture = architecture
        metadata = urlsplit(metadata_base_url)
        target = urlsplit(target_base_url)
        try:
            metadata_port = metadata.port
            target_port = target.port
        except ValueError as error:
            raise TUFTrustError("TUF repository routes are invalid") from error
        expected_origin = f"https://{metadata.hostname or ''}"
        if metadata_port is not None:
            expected_origin += f":{metadata_port}"
        if (
            metadata_base_url != expected_origin + "/agent/v1/tuf/metadata/"
            or target_base_url != expected_origin + "/agent/v1/tuf/targets/"
            or target.scheme != "https"
            or target.hostname != metadata.hostname
            or target_port != metadata_port
            or metadata.username
            or metadata.password
            or target.username
            or target.password
            or metadata.scheme != "https"
            or metadata.query
            or metadata.fragment
            or target.query
            or target.fragment
        ):
            raise TUFTrustError("TUF repository routes are invalid")

    def authorize(
        self, request: ReleaseRequest, deadline: datetime | MonotonicDeadline
    ) -> ReleaseDescriptor:
        try:
            fixed_deadline = MonotonicDeadline.bind(deadline)
            fixed_deadline.check()
        except DeadlineBindingError:
            raise TUFTrustError("TUF authorization deadline has elapsed")
        absolute_deadline = fixed_deadline.absolute_monotonic
        check_deadline = lambda: _check_deadline(absolute_deadline)
        check_deadline()
        _secure_directory(self._metadata_root, fixed_deadline)
        _secure_directory(self._target_root, fixed_deadline)
        check_deadline()
        lock = os.open(
            self._metadata_root / _LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        updater_started = False
        try:
            check_deadline()
            try:
                check_deadline()
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                check_deadline()
            except OSError as error:
                raise TUFTrustError("TUF metadata cache is already in use") from error
            self._fetcher.set_deadline(absolute_deadline)
            marker = self._metadata_root / _BOOTSTRAP_MARKER
            _remove_stale_entry(
                marker.with_name(marker.name + ".new"), fixed_deadline
            )
            _remove_stale_entry(
                self._metadata_root / ".root-link.new", fixed_deadline
            )
            _validate_cache(self._metadata_root, fixed_deadline)
            _validate_empty_target_cache(
                self._target_root, fixed_deadline
            )
            check_deadline()
            bootstrap = (
                None
                if _established_root_is_openable(
                    self._metadata_root, marker, fixed_deadline
                )
                else self._bootstrap_root
            )
            check_deadline()
            updater = _interruptible_tuf_call(
                fixed_deadline,
                lambda: Updater(
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
                        app_user_agent="dgx-forge-agent/0.1.0",
                    ),
                    bootstrap=bootstrap,
                ),
            )
            check_deadline()
            updater_started = True
            if bootstrap is not None:
                _harden_cache(self._metadata_root, fixed_deadline)
                _fsync_cache(self._metadata_root, fixed_deadline)
                _write_marker(
                    marker, hashlib.sha256(
                        _cached_root_bytes(
                            self._metadata_root, fixed_deadline
                        )
                    ).hexdigest(), fixed_deadline
                )
            check_deadline()
            _interruptible_tuf_call(fixed_deadline, updater.refresh)
            check_deadline()
            target_info = _interruptible_tuf_call(
                fixed_deadline,
                lambda: updater.get_targetinfo(request.target_name),
            )
            check_deadline()
            if target_info is None:
                raise TUFTrustError("TUF target is not authorized")
            if (
                target_info.length > _TARGET_LIMIT
                or set(target_info.hashes) != {"sha256"}
            ):
                raise TUFTrustError("TUF target bounds are invalid")
            custom = target_info.unrecognized_fields.get("custom")
            if not isinstance(custom, dict) or set(custom) != {"release"}:
                raise TUFTrustError("TUF target bindings are missing")
            signed_descriptor = ReleaseDescriptor.parse(custom["release"])
            target_fd = os.memfd_create(
                "dgx-tuf-target", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
            )
            try:
                check_deadline()
                _interruptible_tuf_call(
                    fixed_deadline,
                    lambda: updater.download_target(
                        target_info, f"/proc/self/fd/{target_fd}"
                    ),
                )
                check_deadline()
                _seal_target_fd(target_fd, fixed_deadline)
                check_deadline()
                os.lseek(target_fd, 0, os.SEEK_SET)
                check_deadline()
                target_bytes = _read_regular_fd(
                    target_fd, _TARGET_LIMIT, fixed_deadline
                )
            finally:
                os.close(target_fd)
            parsed = json.loads(
                target_bytes.decode("utf-8"), object_pairs_hook=_unique_object
            )
            target_descriptor = ReleaseDescriptor.parse(parsed)
            if target_descriptor != signed_descriptor:
                raise TUFTrustError("TUF target bindings do not match target bytes")
            if not target_descriptor.agrees_with(request):
                raise TUFTrustError("release request disagrees with TUF target")
            if (
                target_descriptor.registry_origin != self._registry_origin
                or target_descriptor.repository != self._repository
                or target_descriptor.architecture != self._architecture
                or not (
                    target_descriptor.protocol_min_version
                    <= 1
                    <= target_descriptor.protocol_max_version
                )
                or not (
                    semantic_version(target_descriptor.agent_min_version)
                    <= semantic_version("0.1.0")
                    <= semantic_version(target_descriptor.agent_max_version)
                )
            ):
                raise TUFTrustError("release target is incompatible with local policy")
            _harden_cache(self._metadata_root, fixed_deadline)
            _harden_cache(self._target_root, fixed_deadline)
            _fsync_cache(self._metadata_root, fixed_deadline)
            _fsync_cache(self._target_root, fixed_deadline)
            _write_marker(
                marker,
                hashlib.sha256(
                    _cached_root_bytes(
                        self._metadata_root, fixed_deadline
                    )
                ).hexdigest(),
                fixed_deadline,
            )
            check_deadline()
            return target_descriptor
        except TUFTrustError:
            if updater_started:
                try:
                    _persist_accepted_cache(
                        self._metadata_root, self._target_root,
                        fixed_deadline,
                    )
                except OSError as persistence_error:
                    raise TUFTrustError(
                        "TUF accepted metadata was not durable"
                    ) from persistence_error
            raise
        except (
            DownloadError,
            RepositoryError,
            ReleaseValidationError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            OSError,
            ValueError,
        ) as error:
            if updater_started:
                try:
                    _persist_accepted_cache(
                        self._metadata_root, self._target_root,
                        fixed_deadline,
                    )
                except OSError as persistence_error:
                    raise TUFTrustError(
                        "TUF accepted metadata was not durable"
                    ) from persistence_error
            raise TUFTrustError("signed release authorization failed") from error
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock)


def _secure_directory(
    path: Path, deadline: MonotonicDeadline | None = None
) -> None:
    if not path.is_absolute():
        raise TUFTrustError("TUF cache path is invalid")
    _tuf_deadline(deadline)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _tuf_deadline(deadline)
        for index, component in enumerate(path.parts[1:]):
            _tuf_deadline(deadline)
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                _tuf_deadline(deadline)
                os.mkdir(component, 0o700, dir_fd=descriptor)
                _tuf_deadline(deadline)
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
            _tuf_deadline(deadline)
            _tuf_deadline(deadline)
            metadata = os.fstat(descriptor)
            _tuf_deadline(deadline)
            final = index == len(path.parts[1:]) - 1
            mode = stat.S_IMODE(metadata.st_mode)
            if (
                metadata.st_uid not in {0, os.geteuid()}
                or (mode & 0o077 if final else mode & 0o022 and not mode & stat.S_ISVTX)
            ):
                raise TUFTrustError("TUF cache directory is unsafe")
    except OSError as error:
        raise TUFTrustError("TUF cache directory is unsafe") from error
    finally:
        os.close(descriptor)


def _validate_cache(
    root: Path, deadline: MonotonicDeadline | None = None
) -> None:
    _tuf_deadline(deadline)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _tuf_deadline(deadline)
        _validate_cache_fd(root_fd, Path(), deadline)
    finally:
        os.close(root_fd)


def _validate_cache_fd(
    directory_fd: int,
    prefix: Path,
    deadline: MonotonicDeadline | None = None,
) -> None:
    for name in _tuf_names(directory_fd, deadline):
        relative = prefix / name
        _tuf_deadline(deadline)
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _tuf_deadline(deadline)
        if stat.S_ISLNK(metadata.st_mode):
            if relative != Path("root.json"):
                raise TUFTrustError("TUF cache contains an unsafe link")
            _tuf_deadline(deadline)
            target = os.readlink(name, dir_fd=directory_fd)
            _tuf_deadline(deadline)
            parts = Path(target).parts
            if (
                len(parts) != 2
                or parts[0] != "root_history"
                or not parts[1].endswith(".root.json")
                or not parts[1].split(".", 1)[0].isdigit()
            ):
                raise TUFTrustError("TUF root cache link is unsafe")
            continue
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise TUFTrustError("TUF cache contains an unsafe file type")
        if metadata.st_uid not in {0, os.geteuid()}:
            raise TUFTrustError("TUF cache ownership is unsafe")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise TUFTrustError("TUF cache permissions are unsafe")
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                _tuf_deadline(deadline)
                _validate_cache_fd(child, relative, deadline)
            finally:
                os.close(child)


def _write_marker(
    path: Path,
    root_digest: str,
    deadline: MonotonicDeadline | None = None,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", root_digest):
        raise TUFTrustError("TUF root marker digest is invalid")
    _tuf_deadline(deadline)
    parent = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        temporary_name = path.name + ".new"
        _remove_stale_entry_fd(parent, temporary_name, deadline)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            _tuf_deadline(deadline)
            content = f"sha256:{root_digest}\n".encode("ascii")
            offset = 0
            while offset < len(content):
                _tuf_deadline(deadline)
                written = os.write(descriptor, content[offset:])
                _tuf_deadline(deadline)
                if written <= 0:
                    raise TUFTrustError("TUF root marker write was incomplete")
                offset += written
            _tuf_deadline(deadline)
            os.fsync(descriptor)
            _tuf_deadline(deadline)
        finally:
            os.close(descriptor)
        _tuf_deadline(deadline)
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        # Once publication occurs, make the marker durable through the exact
        # directory identity used by replace before reporting expiry.
        os.fsync(parent)
        _tuf_deadline(deadline)
    finally:
        os.close(parent)


def _remove_stale_entry(
    path: Path, deadline: MonotonicDeadline | None = None
) -> None:
    _tuf_deadline(deadline)
    parent = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        _remove_stale_entry_fd(parent, path.name, deadline)
    finally:
        os.close(parent)


def _remove_stale_entry_fd(
    parent: int,
    name: str,
    deadline: MonotonicDeadline | None = None,
) -> None:
    _tuf_deadline(deadline)
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _tuf_deadline(deadline)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode):
        raise TUFTrustError("TUF temporary state is unsafe")
    _tuf_deadline(deadline)
    os.unlink(name, dir_fd=parent)
    _tuf_deadline(deadline)
    os.fsync(parent)
    _tuf_deadline(deadline)


def _established_root_is_openable(
    root: Path,
    marker: Path,
    deadline: MonotonicDeadline | None = None,
) -> bool:
    digest = _marker_root_digest(marker, deadline)
    if digest is None:
        return False
    try:
        if hashlib.sha256(_cached_root_bytes(root, deadline)).hexdigest() == digest:
            return True
    except TUFTrustError:
        pass
    history = root / "root_history"
    history_fd = -1
    try:
        _tuf_deadline(deadline)
        history_fd = os.open(
            history,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        _tuf_deadline(deadline)
        matches: list[tuple[int, str]] = []
        for name in _tuf_names(history_fd, deadline):
            match = re.fullmatch(r"([1-9][0-9]*)\.root\.json", name)
            if match is None:
                continue
            _tuf_deadline(deadline)
            descriptor = os.open(
                name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=history_fd,
            )
            try:
                _tuf_deadline(deadline)
                content = _read_regular_fd(
                    descriptor, 256 * 1024, deadline
                )
            finally:
                os.close(descriptor)
            if hashlib.sha256(content).hexdigest() == digest:
                matches.append((int(match.group(1)), name))
        if matches:
            _, name = max(matches)
            _replace_root_pointer(
                root, f"root_history/{name}", deadline
            )
            return True
    except (FileNotFoundError, OSError):
        pass
    finally:
        if history_fd >= 0:
            os.close(history_fd)
    raise TUFTrustError(
        "TUF established root is missing; operator recovery is required"
    )


def _marker_root_digest(
    path: Path, deadline: MonotonicDeadline | None = None
) -> str | None:
    try:
        _tuf_deadline(deadline)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        _tuf_deadline(deadline)
        metadata = os.fstat(descriptor)
        _tuf_deadline(deadline)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != 72
        ):
            raise TUFTrustError("TUF bootstrap marker is unsafe")
        _tuf_deadline(deadline)
        raw = os.read(descriptor, 73)
        _tuf_deadline(deadline)
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise TUFTrustError("TUF bootstrap marker is unsafe") from error
    match = re.fullmatch(r"sha256:([0-9a-f]{64})\n", value)
    if match is None:
        raise TUFTrustError("TUF bootstrap marker is unsafe")
    return match.group(1)


def _cached_root_bytes(
    root: Path, deadline: MonotonicDeadline | None = None
) -> bytes:
    try:
        _tuf_deadline(deadline)
        target = os.readlink(root / "root.json")
        _tuf_deadline(deadline)
    except OSError as error:
        raise TUFTrustError("TUF cached root is unavailable") from error
    if not re.fullmatch(r"root_history/[1-9][0-9]*\.root\.json", target):
        raise TUFTrustError("TUF cached root pointer is unsafe")
    _tuf_deadline(deadline)
    descriptor = os.open(
        root / target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        _tuf_deadline(deadline)
        return _read_regular_fd(descriptor, 256 * 1024, deadline)
    finally:
        os.close(descriptor)


def _replace_root_pointer(
    root: Path,
    target: str,
    deadline: MonotonicDeadline | None = None,
) -> None:
    _tuf_deadline(deadline)
    parent = os.open(
        root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    temporary = ".root-link.new"
    try:
        _tuf_deadline(deadline)
        try:
            _tuf_deadline(deadline)
            os.unlink(temporary, dir_fd=parent)
            _tuf_deadline(deadline)
        except FileNotFoundError:
            pass
        _tuf_deadline(deadline)
        os.symlink(target, temporary, dir_fd=parent)
        _tuf_deadline(deadline)
        os.replace(temporary, "root.json", src_dir_fd=parent, dst_dir_fd=parent)
        # The root pointer is authority-bearing once replaced: make the rename
        # durable before propagating an elapsed deadline.
        os.fsync(parent)
        _tuf_deadline(deadline)
    finally:
        os.close(parent)


def _read_regular(
    path: Path,
    limit: int,
    deadline: MonotonicDeadline | None = None,
) -> bytes:
    _tuf_deadline(deadline)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _tuf_deadline(deadline)
        return _read_regular_fd(descriptor, limit, deadline)
    finally:
        os.close(descriptor)


def _read_regular_fd(
    descriptor: int,
    limit: int,
    deadline: MonotonicDeadline | None = None,
) -> bytes:
    try:
        _tuf_deadline(deadline)
        metadata = os.fstat(descriptor)
        _tuf_deadline(deadline)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise TUFTrustError("TUF target is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            _tuf_deadline(deadline)
            chunk = os.read(
                descriptor, min(64 * 1024, limit + 1 - total)
            )
            _tuf_deadline(deadline)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise TUFTrustError("TUF target is too large")
            chunks.append(chunk)
        if total != metadata.st_size:
            raise TUFTrustError("TUF target size changed")
        return b"".join(chunks)
    except OSError as error:
        raise TUFTrustError("TUF target is unsafe") from error


def _validate_empty_target_cache(
    root: Path, deadline: MonotonicDeadline | None = None
) -> None:
    _tuf_deadline(deadline)
    descriptor = os.open(
        root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        names = _tuf_names(descriptor, deadline)
        try:
            if next(names, None) is not None:
                raise TUFTrustError("TUF target cache is not empty")
        finally:
            names.close()
    finally:
        os.close(descriptor)


def _seal_target_fd(
    descriptor: int, deadline: MonotonicDeadline | None = None
) -> None:
    _tuf_deadline(deadline)
    fcntl.fcntl(
        descriptor,
        fcntl.F_ADD_SEALS,
        fcntl.F_SEAL_SEAL
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_WRITE,
    )
    _tuf_deadline(deadline)


def _harden_cache(
    root: Path, deadline: MonotonicDeadline | None = None
) -> None:
    _tuf_deadline(deadline)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _tuf_deadline(deadline)
        _harden_cache_fd(root_fd, deadline)
    finally:
        os.close(root_fd)


def _harden_cache_fd(
    directory_fd: int, deadline: MonotonicDeadline | None = None
) -> None:
    for name in _tuf_names(directory_fd, deadline):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _tuf_deadline(deadline)
        if stat.S_ISLNK(metadata.st_mode):
            continue
        _tuf_deadline(deadline)
        os.chmod(
            name,
            0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        _tuf_deadline(deadline)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                _tuf_deadline(deadline)
                _harden_cache_fd(child, deadline)
            finally:
                os.close(child)


def _fsync_cache(
    root: Path, deadline: MonotonicDeadline | None = None
) -> None:
    _tuf_deadline(deadline)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _tuf_deadline(deadline)
        _fsync_cache_fd(root_fd, deadline)
    finally:
        os.close(root_fd)


def _fsync_cache_fd(
    directory_fd: int, deadline: MonotonicDeadline | None = None
) -> None:
    for name in _tuf_names(directory_fd, deadline):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _tuf_deadline(deadline)
        if stat.S_ISLNK(metadata.st_mode):
            continue
        descriptor = os.open(
            name,
            (os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            | (os.O_DIRECTORY if stat.S_ISDIR(metadata.st_mode) else 0),
            dir_fd=directory_fd,
        )
        try:
            _tuf_deadline(deadline)
            if stat.S_ISDIR(metadata.st_mode):
                _fsync_cache_fd(descriptor, deadline)
            _tuf_deadline(deadline)
            os.fsync(descriptor)
            _tuf_deadline(deadline)
        finally:
            os.close(descriptor)
    _tuf_deadline(deadline)
    os.fsync(directory_fd)
    _tuf_deadline(deadline)


def _persist_accepted_cache(
    metadata_root: Path,
    target_root: Path,
    deadline: MonotonicDeadline | None = None,
) -> None:
    _tuf_deadline(deadline)
    _harden_cache(metadata_root, deadline)
    _harden_cache(target_root, deadline)
    _fsync_cache(metadata_root, deadline)
    _fsync_cache(target_root, deadline)


def _allowed_tuf_path(path: str) -> bool:
    metadata_prefix = "/agent/v1/tuf/metadata/"
    targets_prefix = "/agent/v1/tuf/targets/"
    if path.startswith(metadata_prefix):
        return bool(_TUF_FILE.fullmatch(path[len(metadata_prefix):]))
    if path.startswith(targets_prefix):
        return bool(_TARGET_FILE.fullmatch(path[len(targets_prefix):]))
    return False


def _check_deadline(absolute_deadline: float) -> None:
    if time.monotonic() >= absolute_deadline:
        raise TUFTrustError("TUF authorization deadline has elapsed")


def _tuf_deadline(deadline: MonotonicDeadline | None) -> None:
    if deadline is None:
        return
    try:
        deadline.check()
    except DeadlineBindingError as error:
        raise TUFTrustError("TUF authorization deadline has elapsed") from error


def _tuf_names(
    directory_fd: int, deadline: MonotonicDeadline | None
):
    _tuf_deadline(deadline)
    entries = os.scandir(directory_fd)
    try:
        while True:
            _tuf_deadline(deadline)
            try:
                entry = next(entries)
            except StopIteration:
                break
            _tuf_deadline(deadline)
            yield entry.name
    finally:
        entries.close()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise TUFTrustError("TUF target contains duplicate fields")
        document[key] = value
    return document
