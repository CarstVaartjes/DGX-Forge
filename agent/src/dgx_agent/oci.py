"""Digest-only ORAS transport through an installed local policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import fcntl
import os
from pathlib import Path
import stat
import threading
from urllib.parse import urlsplit

from .nvidia_tools import InstalledToolSecurityError, open_verified_executable
from .deadlines import DeadlineBindingError, MonotonicDeadline
from .probe import BoundedProcessRunner, ProcessRequest, ProbeError
from .releases import ReleaseDescriptor


_ORAS_VERSION = "1.3.3"
_OUTPUT_LIMIT = 64 * 1024
_MAX_PULL_SECONDS = 300


class OCIError(RuntimeError):
    error_code = "release_transport_failed"


@dataclass(frozen=True)
class ORASPolicy:
    registry_origin: str
    repository: str
    executable: Path
    executable_sha256: str
    executable_version: str
    auth_path: Path
    ca_path: Path
    client_certificate_path: Path
    client_key_path: Path
    allow_unprivileged_test_files: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.registry_origin)
        try:
            port = parsed.port
        except ValueError as error:
            raise OCIError("registry origin is invalid") from error
        normalized_host = parsed.hostname or ""
        normalized = f"https://{normalized_host}"
        if port is not None:
            normalized += f":{port}"
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or self.registry_origin != normalized
        ):
            raise OCIError("registry origin is invalid")
        if self.executable_version != _ORAS_VERSION:
            raise OCIError("ORAS version is not reviewed")
        for path, private in (
            (self.auth_path, True),
            (self.ca_path, False),
            (self.client_certificate_path, False),
            (self.client_key_path, True),
        ):
            _regular_file(path, private=private)


class ORASClient:
    def __init__(self, policy: ORASPolicy) -> None:
        self._policy = policy
        self._runner = BoundedProcessRunner()
        descriptors: list[int] = []
        try:
            for path, private in (
                (policy.auth_path, True),
                (policy.ca_path, False),
                (policy.client_certificate_path, False),
                (policy.client_key_path, True),
            ):
                descriptors.append(
                    _snapshot_policy_file(
                        path,
                        private=private,
                        allow_unprivileged_test_files=policy.allow_unprivileged_test_files,
                    )
                )
        except Exception:
            for descriptor in descriptors:
                os.close(descriptor)
            raise
        self._policy_fds = tuple(descriptors)
        self._condition = threading.Condition()
        self._active_pulls = 0
        self._closed = False

    def close(self) -> None:
        with self._condition:
            self._closed = True
            while self._active_pulls:
                self._condition.wait()
            descriptors, self._policy_fds = self._policy_fds, ()
        for descriptor in descriptors:
            os.close(descriptor)

    def __del__(self) -> None:
        for descriptor in getattr(self, "_policy_fds", ()):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def pull(
        self,
        descriptor: ReleaseDescriptor,
        destination: Path,
        deadline: datetime | MonotonicDeadline,
    ) -> None:
        if (
            descriptor.registry_origin != self._policy.registry_origin
            or descriptor.repository != self._policy.repository
        ):
            raise OCIError("release transport disagrees with local policy")
        try:
            fixed_deadline = MonotonicDeadline.bind(deadline)
            fixed_deadline.check()
        except DeadlineBindingError as error:
            raise OCIError("release transport deadline has elapsed") from error
        destination = Path(destination)
        if not destination.is_absolute() or destination.is_symlink():
            raise OCIError("release staging destination is unsafe")
        executable_fd = -1
        policy_fds = self._begin_pull()
        try:
            executable_fd = open_verified_executable(
                self._policy.executable,
                self._policy.executable_sha256,
                _test_only_allow_unprivileged=self._policy.allow_unprivileged_test_files,
            )
            if executable_fd is None:
                raise OCIError("reviewed ORAS executable is unavailable")
            host = urlsplit(self._policy.registry_origin).netloc
            reference = (
                f"{host}/{self._policy.repository}@{descriptor.oci_manifest_digest}"
            )
            remaining = fixed_deadline.remaining()
            if remaining <= 0:
                raise OCIError("release transport deadline has elapsed")
            request = ProcessRequest.fixed(
                argv=(
                    f"/proc/self/fd/{executable_fd}",
                    "pull",
                    reference,
                    "--output",
                    str(destination),
                    "--registry-config",
                    f"/proc/self/fd/{policy_fds[0]}",
                    "--ca-file",
                    f"/proc/self/fd/{policy_fds[1]}",
                    "--cert-file",
                    f"/proc/self/fd/{policy_fds[2]}",
                    "--key-file",
                    f"/proc/self/fd/{policy_fds[3]}",
                    "--concurrency",
                    "2",
                ),
                cwd=destination,
                timeout_seconds=min(_MAX_PULL_SECONDS, remaining),
                output_limit_bytes=_OUTPUT_LIMIT,
                executable_fd=executable_fd,
                absolute_deadline=fixed_deadline.absolute_monotonic,
                additional_fds=policy_fds,
            )
            outcome = self._runner.run(request)
            if outcome.returncode != 0:
                raise OCIError("ORAS pull failed")
            if fixed_deadline.remaining() <= 0:
                raise OCIError("release transport deadline has elapsed")
        except (InstalledToolSecurityError, ProbeError, OSError) as error:
            raise OCIError("release transport failed") from error
        finally:
            if executable_fd >= 0:
                os.close(executable_fd)
            self._end_pull()

    def _begin_pull(self) -> tuple[int, ...]:
        with self._condition:
            if self._closed or len(self._policy_fds) != 4:
                raise OCIError("release transport policy is closed")
            self._active_pulls += 1
            return self._policy_fds

    def _end_pull(self) -> None:
        with self._condition:
            self._active_pulls -= 1
            self._condition.notify_all()


def _regular_file(path: Path, *, private: bool) -> None:
    path = Path(path)
    if not path.is_absolute():
        raise OCIError("release policy path is invalid")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise OCIError("release policy file is unavailable") from error
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or (mode != 0o600 if private else bool(mode & 0o022))
        or mode & (stat.S_ISUID | stat.S_ISGID)
    ):
        raise OCIError("release policy file is unsafe")


def _snapshot_policy_file(
    path: Path, *, private: bool, allow_unprivileged_test_files: bool
) -> int:
    path = Path(path)
    if not path.is_absolute() or len(path.parts) < 2:
        raise OCIError("release policy path is invalid")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    source = -1
    snapshot = -1
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            os.close(parent)
            parent = child
            _trusted_ancestry(
                os.fstat(parent),
                allow_unprivileged_test_files=allow_unprivileged_test_files,
            )
        source = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        before = os.fstat(source)
        _trusted_policy_file(
            before,
            private=private,
            allow_unprivileged_test_files=allow_unprivileged_test_files,
        )
        if before.st_size > 1024 * 1024:
            raise OCIError("release policy file is too large")
        snapshot = os.memfd_create(
            "dgx-oras-policy", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        remaining = before.st_size
        while remaining:
            data = os.read(source, min(64 * 1024, remaining))
            if not data:
                raise OCIError("release policy file changed during snapshot")
            offset = 0
            while offset < len(data):
                offset += os.write(snapshot, data[offset:])
            remaining -= len(data)
        if os.read(source, 1):
            raise OCIError("release policy file changed during snapshot")
        after = os.fstat(source)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OCIError("release policy file changed during snapshot")
        fcntl.fcntl(
            snapshot,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE,
        )
        os.lseek(snapshot, 0, os.SEEK_SET)
        result, snapshot = snapshot, -1
        return result
    except OCIError:
        raise
    except OSError as error:
        raise OCIError("release policy file is unsafe") from error
    finally:
        if snapshot >= 0:
            os.close(snapshot)
        if source >= 0:
            os.close(source)
        os.close(parent)


def _trusted_ancestry(
    metadata: os.stat_result, *, allow_unprivileged_test_files: bool
) -> None:
    owners = {0}
    if allow_unprivileged_test_files:
        owners.add(os.geteuid())
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in owners
        or (mode & 0o022 and not (allow_unprivileged_test_files and mode & stat.S_ISVTX))
    ):
        raise OCIError("release policy ancestry is unsafe")


def _trusted_policy_file(
    metadata: os.stat_result,
    *,
    private: bool,
    allow_unprivileged_test_files: bool,
) -> None:
    owners = (
        {os.geteuid()}
        if private
        else ({0, os.geteuid()} if allow_unprivileged_test_files else {0})
    )
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid not in owners
        or (mode != 0o600 if private else bool(mode & 0o022))
        or mode & (stat.S_ISUID | stat.S_ISGID)
    ):
        raise OCIError("release policy file is unsafe")
