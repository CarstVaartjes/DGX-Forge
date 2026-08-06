"""Socket-facing authorization boundary for privileged package setup."""

from __future__ import annotations

import fcntl
import hashlib
import os
import pwd
import socket
import sqlite3
import stat
import struct
import subprocess
import threading
from base64 import urlsafe_b64decode
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .package_helper_protocol import (
    HelperProtocolError,
    HelperRequest,
    HelperResponse,
    SignedObjectReceipt,
    canonical_helper_document,
    frame_helper_message,
    receive_helper_message,
)
from .packages.sandbox import SandboxPolicy

MAX_REPLAY_ENTRIES = 65_536


class ReceiptVerifier(Protocol):
    def verify(self, receipt: SignedObjectReceipt) -> bool: ...


class FenceAuthorizer(Protocol):
    def authorize(self, request: HelperRequest, request_digest: str) -> bool: ...


class BackendLauncher(Protocol):
    def launch(
        self, request: HelperRequest, sandbox: SandboxPolicy
    ) -> Mapping[str, object]: ...


class SystemdCommandRunner:
    """Run only the compiled systemd transient-service command without output."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        pass_fds: tuple[int, ...],
        timeout_seconds: int,
    ) -> int:
        if not argv or argv[0] != "/usr/bin/systemd-run":
            raise HelperProtocolError("package helper command is not compiled")
        try:
            completed = subprocess.run(
                argv,
                executable="/usr/bin/systemd-run",
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd="/",
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                close_fds=True,
                pass_fds=pass_fds,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HelperProtocolError("package backend launch failed") from error
        return completed.returncode


class SystemdBackendLauncher:
    """Launch a digest-matched adapter in a fixed transient systemd sandbox."""

    def __init__(self, generations_root: Path, *, runner=None) -> None:
        root = Path(generations_root)
        if not root.is_absolute():
            raise HelperProtocolError("generation root is invalid")
        self._root = root
        self._runner = runner or SystemdCommandRunner()
        if not callable(getattr(self._runner, "run", None)):
            raise HelperProtocolError("systemd command boundary is invalid")

    def launch(
        self, request: HelperRequest, sandbox: SandboxPolicy
    ) -> Mapping[str, object]:
        generation_fd = -1
        executable_fd = -1
        try:
            generation_fd, executable_fd = _open_backend_content(self._root, request)
            plan = sandbox.plan(request.invocation, generation_fd, executable_fd)
            try:
                os.fchown(executable_fd, plan.uid, plan.gid)
                os.fchmod(executable_fd, 0o500)
            except OSError as error:
                raise HelperProtocolError(
                    "backend snapshot identity could not be applied"
                ) from error
            helper_pid = os.getpid()
            source_executable = f"/proc/{helper_pid}/fd/{executable_fd}"
            source_generation = f"/proc/{helper_pid}/fd/{generation_fd}"
            resources = request.invocation.resources
            argv = [
                "/usr/bin/systemd-run",
                "--quiet",
                "--wait",
                "--pipe",
                "--collect",
                "--service-type=exec",
                f"--unit=dgx-workload-{request.request_id}.service",
                f"--uid={plan.uid}",
                f"--gid={plan.gid}",
                "--property=NoNewPrivileges=yes",
                "--property=CapabilityBoundingSet=",
                "--property=AmbientCapabilities=",
                "--property=DevicePolicy=closed",
                "--property=PrivateMounts=yes",
                "--property=ProtectSystem=strict",
                "--property=ProtectHome=yes",
                "--property=RestrictSUIDSGID=yes",
                f"--property=MemoryMax={resources.memory_bytes}",
                f"--property=TasksMax={resources.pids_limit}",
                f"--property=CPUQuota={resources.cpu_millis / 10:.1f}%",
                f"--property=RuntimeMaxSec={resources.timeout_seconds}",
                "--property=TimeoutStopSec=10",
                f"--property=BindReadOnlyPaths={source_executable}:/run/dgx-forge/entrypoint",
                f"--property=BindReadOnlyPaths={source_generation}:/run/dgx-forge/generation",
                "--working-directory=/run/dgx-forge/generation",
            ]
            argv.append("--property=PrivateNetwork=yes")
            argv.extend(
                f"--property=DeviceAllow=/dev/{device} rw"
                for device in request.invocation.devices
            )
            argv.extend(
                ("--", "/run/dgx-forge/entrypoint", *request.invocation.arguments)
            )
            fixed_argv = tuple(argv)
            returncode = self._runner.run(
                fixed_argv,
                pass_fds=(generation_fd, executable_fd),
                timeout_seconds=resources.timeout_seconds,
            )
            if returncode != 0:
                raise HelperProtocolError("package backend launch failed")
            evidence = hashlib.sha256(
                canonical_helper_document(
                    {
                        "request_digest": request.digest,
                        "launch_digest": hashlib.sha256(
                            "\0".join(fixed_argv).encode("utf-8")
                        ).hexdigest(),
                    }
                )
            ).hexdigest()
            return {
                "status": "launched",
                "evidence_digest": evidence,
                "fence": request.fence,
            }
        finally:
            if executable_fd >= 0:
                os.close(executable_fd)
            if generation_fd >= 0:
                os.close(generation_fd)


def _open_backend_content(
    generations_root: Path, request: HelperRequest
) -> tuple[int, int]:
    root_fd = -1
    generation_fd = -1
    source_fd = -1
    snapshot_fd = -1
    directory_fd = -1
    try:
        root_fd = os.open(
            generations_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        release_fd = os.open(
            request.invocation.release_digest,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            generation_fd = os.open(
                request.invocation.generation,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=release_fd,
            )
        finally:
            os.close(release_fd)
        directory_fd = os.dup(generation_fd)
        parts = PurePosixPath(request.invocation.entrypoint).parts
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child
        source_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or not before.st_mode & 0o100
            or not 1 <= before.st_size <= 256 * 1024 * 1024
        ):
            raise HelperProtocolError("backend entrypoint is unsafe")
        snapshot_fd = os.memfd_create(
            "dgx-package-entrypoint", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        os.fchmod(snapshot_fd, 0o500)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot_fd, view)
                if written <= 0:
                    raise HelperProtocolError("backend snapshot is incomplete")
                view = view[written:]
        after = os.fstat(source_fd)
        exact_receipt = next(
            (
                receipt
                for receipt in request.receipts
                if receipt.object_digest == digest.hexdigest()
                and receipt.size == before.st_size
            ),
            None,
        )
        if exact_receipt is None or (
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
            raise HelperProtocolError("backend entrypoint has no signed receipt")
        fcntl.fcntl(
            snapshot_fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE,
        )
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        result = (generation_fd, snapshot_fd)
        generation_fd = snapshot_fd = -1
        return result
    except HelperProtocolError:
        raise
    except OSError as error:
        raise HelperProtocolError("backend content is unavailable") from error
    finally:
        for descriptor in (
            directory_fd,
            source_fd,
            generation_fd,
            snapshot_fd,
            root_fd,
        ):
            if descriptor >= 0:
                os.close(descriptor)


class Ed25519ReceiptVerifier:
    """Verify canonical object receipts with one installed public key."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise HelperProtocolError("receipt public key is invalid")
        self._public_key = public_key

    @classmethod
    def from_file(
        cls, path: Path, *, allow_unprivileged_test_file: bool = False
    ) -> Ed25519ReceiptVerifier:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > 4096
                or before.st_mode & 0o022
                or (
                    not allow_unprivileged_test_file
                    and before.st_uid not in {0, os.geteuid()}
                )
            ):
                raise HelperProtocolError("receipt public key file is unsafe")
            raw = os.read(descriptor, 4097)
            after = os.fstat(descriptor)
            if len(raw) > 4096 or (
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
                raise HelperProtocolError("receipt public key changed")
            public_key = serialization.load_pem_public_key(raw)
            if not isinstance(public_key, Ed25519PublicKey):
                raise HelperProtocolError("receipt public key is not Ed25519")
            return cls(public_key)
        except HelperProtocolError:
            raise
        except (OSError, ValueError) as error:
            raise HelperProtocolError("receipt public key is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def verify(self, receipt: SignedObjectReceipt) -> bool:
        if type(receipt) is not SignedObjectReceipt:
            return False
        try:
            signature = urlsafe_b64decode(receipt.signature + "==")
            self._public_key.verify(
                signature, canonical_helper_document(receipt.unsigned_mapping())
            )
            return True
        except (InvalidSignature, ValueError):
            return False


class SignedFenceAuthorizer:
    """Verify a control-signed operation grant and durably consume it once."""

    def __init__(
        self,
        public_key: Ed25519PublicKey,
        replay_database: Path,
        *,
        allow_unprivileged_test_files: bool = False,
        clock: Callable[[], datetime] | None = None,
        max_entries: int = MAX_REPLAY_ENTRIES,
    ) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise HelperProtocolError("fence public key is invalid")
        self._public_key = public_key
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or not 1 <= max_entries <= MAX_REPLAY_ENTRIES
        ):
            raise HelperProtocolError("helper replay capacity is invalid")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_entries = max_entries
        self._replay_database = _safe_replay_database(
            replay_database,
            allow_unprivileged_test_files=allow_unprivileged_test_files,
        )
        self._lock = threading.Lock()
        try:
            with sqlite3.connect(self._replay_database) as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS consumed_fences ("
                    "request_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, "
                    "attempt INTEGER NOT NULL, fence TEXT NOT NULL, "
                    "request_digest TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                    "UNIQUE(operation_id, attempt, fence))"
                )
        except sqlite3.DatabaseError as error:
            raise HelperProtocolError("helper replay database is invalid") from error

    @classmethod
    def from_file(
        cls,
        public_key_path: Path,
        replay_database: Path,
        *,
        allow_unprivileged_test_files: bool = False,
        clock: Callable[[], datetime] | None = None,
        max_entries: int = MAX_REPLAY_ENTRIES,
    ) -> SignedFenceAuthorizer:
        verifier = Ed25519ReceiptVerifier.from_file(
            public_key_path,
            allow_unprivileged_test_file=allow_unprivileged_test_files,
        )
        return cls(
            verifier._public_key,
            replay_database,
            allow_unprivileged_test_files=allow_unprivileged_test_files,
            clock=clock,
            max_entries=max_entries,
        )

    def authorize(self, request: HelperRequest, request_digest: str) -> bool:
        if type(request) is not HelperRequest or request_digest != request.digest:
            return False
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise HelperProtocolError("helper authority clock is invalid")
        now = now.astimezone(UTC)
        try:
            expires_at = datetime.strptime(
                request.expires_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=UTC)
        except ValueError:
            return False
        if expires_at <= now or expires_at > now.replace(microsecond=0) + timedelta(
            minutes=15
        ):
            return False
        try:
            signature = urlsafe_b64decode(request.authorization + "==")
            self._public_key.verify(
                signature, canonical_helper_document(request.unsigned_mapping())
            )
        except (InvalidSignature, ValueError):
            return False
        with self._lock:
            try:
                with sqlite3.connect(
                    self._replay_database, isolation_level=None
                ) as replay:
                    replay.execute("BEGIN IMMEDIATE")
                    replay.execute(
                        "DELETE FROM consumed_fences WHERE expires_at <= ?",
                        (int(now.timestamp()),),
                    )
                    count = replay.execute(
                        "SELECT COUNT(*) FROM consumed_fences"
                    ).fetchone()
                    if count is None or count[0] >= self._max_entries:
                        replay.execute("ROLLBACK")
                        raise HelperProtocolError(
                            "package helper replay capacity is exhausted"
                        )
                    cursor = replay.execute(
                        "INSERT OR IGNORE INTO consumed_fences "
                        "(request_id, operation_id, attempt, fence, request_digest, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            request.request_id,
                            request.operation_id,
                            request.attempt,
                            request.fence,
                            request_digest,
                            int(expires_at.timestamp()),
                        ),
                    )
                    replay.execute("COMMIT")
                    return cursor.rowcount == 1
            except sqlite3.DatabaseError as error:
                raise HelperProtocolError(
                    "package helper fence state is unavailable"
                ) from error


def _safe_replay_database(path: Path, *, allow_unprivileged_test_files: bool) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise HelperProtocolError("helper replay database path is invalid")
    expected_uid = os.geteuid() if allow_unprivileged_test_files else 0
    try:
        parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != expected_uid
            or parent.st_mode & 0o022
        ):
            raise HelperProtocolError("helper replay directory is unsafe")
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != expected_uid
                or metadata.st_mode & 0o177 != 0
            ):
                raise HelperProtocolError("helper replay database is unsafe")
        finally:
            os.close(descriptor)
    except HelperProtocolError:
        raise
    except OSError as error:
        raise HelperProtocolError("helper replay database is unavailable") from error
    return path


class PackageHelper:
    """Validate one already-authenticated package-engine launch request."""

    def __init__(
        self,
        *,
        agent_uid: int,
        sandbox: SandboxPolicy,
        receipt_verifier: ReceiptVerifier,
        fence_authorizer: FenceAuthorizer,
        launcher: BackendLauncher,
    ) -> None:
        if (
            not isinstance(agent_uid, int)
            or isinstance(agent_uid, bool)
            or not 1 <= agent_uid <= 2**31 - 1
            or agent_uid == sandbox.workload_uid
        ):
            raise HelperProtocolError("agent peer UID is invalid")
        for boundary, method, name in (
            (receipt_verifier, "verify", "receipt verifier"),
            (fence_authorizer, "authorize", "fence authorizer"),
            (launcher, "launch", "backend launcher"),
        ):
            if not callable(getattr(boundary, method, None)):
                raise HelperProtocolError(f"{name} is invalid")
        self._agent_uid = agent_uid
        self._sandbox = sandbox
        self._receipt_verifier = receipt_verifier
        self._fence_authorizer = fence_authorizer
        self._launcher = launcher
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def handle(self, peer_uid: int, raw: bytes) -> bytes:
        if type(peer_uid) is not int or peer_uid != self._agent_uid:
            raise HelperProtocolError("package helper peer is not the agent")
        request = HelperRequest.parse(raw)
        request_digest = hashlib.sha256(raw).hexdigest()
        key = (request.request_id, request.fence)
        with self._lock:
            if key in self._seen:
                raise HelperProtocolError("package helper request replay was rejected")
            if len(self._seen) >= MAX_REPLAY_ENTRIES:
                raise HelperProtocolError("package helper replay state is full")
            if not self._fence_authorizer.authorize(request, request_digest):
                raise HelperProtocolError("package helper operation fence is stale")
            for receipt in request.receipts:
                if not self._receipt_verifier.verify(receipt):
                    raise HelperProtocolError(
                        "package helper object receipt is invalid"
                    )
            # Consume before the side effect. A failed launch remains ambiguous and may
            # only be retried under a new authorized request/fence.
            self._seen.add(key)
        try:
            outcome = self._launcher.launch(request, self._sandbox)
        except HelperProtocolError:
            raise
        except Exception as error:
            raise HelperProtocolError("package helper backend launch failed") from error
        if not isinstance(outcome, Mapping) or set(outcome) != {
            "status",
            "evidence_digest",
            "fence",
        }:
            raise HelperProtocolError("package helper result fields are invalid")
        if outcome["fence"] != request.fence:
            raise HelperProtocolError("package helper result fence does not match")
        try:
            response = HelperResponse(
                1,
                request.request_id,
                outcome["status"],
                outcome["evidence_digest"],
                outcome["fence"],
            )
        except (TypeError, ValueError) as error:
            raise HelperProtocolError("package helper result is invalid") from error
        return response.to_bytes()


def unix_peer_uid(connection: socket.socket) -> int:
    """Return the kernel-authenticated UID for one Unix stream connection."""
    if connection.family != socket.AF_UNIX:
        raise HelperProtocolError("package helper requires a Unix socket")
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as error:
        raise HelperProtocolError(
            "package helper peer credentials are unavailable"
        ) from error
    if uid < 0:
        raise HelperProtocolError("package helper peer credentials are invalid")
    return uid


def serve_connection(
    helper: PackageHelper,
    connection: socket.socket,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Serve one length-framed connection within an absolute socket deadline."""
    peer_uid = unix_peer_uid(connection)
    request = receive_helper_message(connection, timeout_seconds=timeout_seconds)
    response = helper.handle(peer_uid, request)
    previous = connection.gettimeout()
    connection.settimeout(timeout_seconds)
    try:
        connection.sendall(frame_helper_message(response))
    except OSError as error:
        raise HelperProtocolError("package helper response deadline elapsed") from error
    finally:
        connection.settimeout(previous)


def serve_listener(
    helper: PackageHelper,
    listener: socket.socket,
    *,
    connection_timeout_seconds: float = 5.0,
) -> None:
    """Serve a persistent systemd-owned Unix listener, isolating bad peers."""
    if listener.family != socket.AF_UNIX or listener.type & socket.SOCK_STREAM == 0:
        raise HelperProtocolError("package helper listener is invalid")
    while True:
        connection, _ = listener.accept()
        try:
            serve_connection(
                helper,
                connection,
                timeout_seconds=connection_timeout_seconds,
            )
        except HelperProtocolError:
            # A malformed or unauthorized connection must not terminate the
            # persistent authority process or expose diagnostic details.
            pass
        finally:
            connection.close()


def main(argv: list[str] | None = None) -> int:
    """Run the helper only when launched by systemd socket activation."""
    import argparse

    parser = argparse.ArgumentParser(prog="dgx-package-helper")
    parser.add_argument("--listen-fd", type=int, default=3)
    args = parser.parse_args(argv)
    if (
        os.environ.get("LISTEN_PID") != str(os.getpid())
        or os.environ.get("LISTEN_FDS") != "1"
    ):
        raise HelperProtocolError("systemd socket activation is required")
    if args.listen_fd != 3:
        raise HelperProtocolError("listener FD is invalid")
    if os.geteuid() != 0:
        raise HelperProtocolError("package helper must run as root")
    try:
        agent_uid = pwd.getpwnam("dgx-agent").pw_uid
        workload = pwd.getpwnam("dgx-workload")
    except KeyError as error:
        raise HelperProtocolError(
            "package helper identities are not installed"
        ) from error
    sandbox = SandboxPolicy(
        workload_uid=workload.pw_uid,
        workload_gid=workload.pw_gid,
        allowed_devices=(
            "nvidiactl",
            "nvidia-uvm",
            "nvidia-uvm-tools",
            "nvidia-modeset",
            *(f"nvidia{index}" for index in range(16)),
        ),
    )
    helper = PackageHelper(
        agent_uid=agent_uid,
        sandbox=sandbox,
        receipt_verifier=Ed25519ReceiptVerifier.from_file(
            Path("/etc/dgx-forge-agent/package-receipt-public.pem")
        ),
        fence_authorizer=SignedFenceAuthorizer.from_file(
            Path("/etc/dgx-forge-agent/package-fence-public.pem"),
            Path("/var/lib/dgx-forge-package-helper/replay.sqlite3"),
        ),
        launcher=SystemdBackendLauncher(
            Path("/var/lib/dgx-forge-agent/packages/generations")
        ),
    )
    listener = socket.socket(fileno=args.listen_fd)
    try:
        serve_listener(helper, listener)
    finally:
        listener.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
