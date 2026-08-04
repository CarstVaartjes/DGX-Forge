"""Crash-recovering lifecycle for the outbound Spark agent."""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import signal
import stat
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from dgx_agent_protocol import AgentClaim, AgentResult

from .client import (
    AgentClient,
    AgentTransportError,
    CredentialProvider,
    CredentialStore,
    EnrollmentPending,
    IssuedCredential,
)
from .config import DEFAULT_CONFIG_PATH, AgentConfig
from .nvidia_tools import InstalledPolicy
from .oci import ORASClient, ORASPolicy
from .operations import OperationContext, OperationRegistry
from .probe import PinnedNodeProbe
from .readiness import ReadinessReporter
from .releases import ReleaseInstaller
from .runtime_policy import RuntimePolicy
from .state import AgentStateStore
from .update_trust import BoundedHTTPSFetcher, TUFReleaseTrust
from .workloads import WorkloadOperations


class AgentControl(Protocol):
    def claim(self) -> AgentClaim | None: ...

    def result(self, result: AgentResult) -> None: ...

    def renew(self, csr: bytes) -> IssuedCredential: ...

    def activate(self, generation: int, credentials: CredentialProvider) -> None: ...


class Interrupt(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


class Agent:
    def __init__(
        self,
        client: AgentControl,
        registry: OperationRegistry,
        context: OperationContext,
        *,
        backoff_min_seconds: float = 1,
        backoff_max_seconds: float = 60,
        jitter: Callable[[float], float] | None = None,
        credentials: CredentialStore | None = None,
        on_authenticated_exchange: Callable[[], object] | None = None,
    ) -> None:
        if backoff_min_seconds <= 0 or backoff_max_seconds < backoff_min_seconds:
            raise ValueError("backoff bounds are invalid")
        self._client = client
        self._registry = registry
        self._context = context
        self._backoff_min = float(backoff_min_seconds)
        self._backoff_max = float(backoff_max_seconds)
        self._jitter = jitter or (lambda upper: random.uniform(0, upper))
        self._credentials = credentials
        self._on_authenticated_exchange = on_authenticated_exchange or (lambda: None)
        self._authenticated_exchange_reported = False

    def _report_authenticated_exchange(self) -> None:
        if self._authenticated_exchange_reported:
            return
        self._on_authenticated_exchange()
        self._authenticated_exchange_reported = True

    def run_once(self) -> None:
        pending = self._context.state.recover_pending()
        if pending is not None:
            assert pending.result is not None
            self._submit(pending.result)
            return
        active = self._context.state.recover_active()
        if active is not None:
            execution = self._registry.execute(active.claim, self._context)
            self._submit(execution.result)
            return
        if self._credentials is not None:
            self._rotate_credentials()
        claim = self._client.claim()
        if claim is None:
            self._report_authenticated_exchange()
            return
        self._report_authenticated_exchange()
        execution = self._registry.execute(claim, self._context)
        self._submit(execution.result)

    def run_forever(self, stop: Interrupt) -> None:
        backoff = self._backoff_min
        while not stop.is_set():
            try:
                self.run_once()
            except AgentTransportError:
                delay = max(0.0, min(backoff, float(self._jitter(backoff))))
                if stop.wait(delay):
                    return
                backoff = min(self._backoff_max, backoff * 2)
            else:
                backoff = self._backoff_min

    def _submit(self, result: AgentResult) -> None:
        self._client.result(result)
        self._report_authenticated_exchange()
        self._context.state.acknowledge(result)

    def _rotate_credentials(self) -> None:
        assert self._credentials is not None
        staged = self._credentials.staged_provider()
        if staged is not None:
            generation = self._credentials.staged_generation
            assert generation is not None
            self._client.activate(generation, staged)
            self._credentials.publish_active(generation)
            self._report_authenticated_exchange()
            return
        pending = self._credentials.pending_rotation()
        if pending is None:
            if not self._credentials.renewal_due(datetime.now(UTC)):
                return
            pending = self._credentials.prepare_rotation(self._context.node_id)
        elif pending.purpose != "rotation":
            raise RuntimeError("enrollment credential request was not recovered")
        issued = self._client.renew(pending.csr_pem)
        self._credentials.stage(issued)
        staged = self._credentials.staged_provider()
        if staged is None:
            raise RuntimeError("staged credential was not published")
        self._client.activate(issued.generation, staged)
        self._credentials.publish_active(issued.generation)
        self._report_authenticated_exchange()


def build_agent(
    config: AgentConfig,
    *,
    credentials: CredentialStore | None = None,
    readiness: ReadinessReporter | None = None,
) -> Agent:
    credentials = credentials or CredentialStore(
        config.state_root,
        config.ca_path,
        config.certificate_path,
        config.private_key_path,
    )
    client = AgentClient(
        config.control_origin,
        config.node_id,
        credentials,
        long_poll_seconds=min(60, config.poll_max_seconds),
        lease_seconds=max(30, min(300, config.poll_max_seconds * 2)),
    )
    state = AgentStateStore(config.state_root)
    policy = InstalledPolicy.load(config.installed_policy_path)
    runtime = RuntimePolicy.load(config.runtime_policy_path)
    runtime.verify_installed()
    fetcher = BoundedHTTPSFetcher(
        config.control_origin,
        credential_provider=credentials,
    )
    trust = TUFReleaseTrust(
        runtime.tuf.metadata_root,
        runtime.tuf.target_root,
        f"{config.control_origin}/agent/v1/tuf/metadata/",
        f"{config.control_origin}/agent/v1/tuf/targets/",
        runtime.read_bootstrap_root(),
        fetcher,
        runtime.registry_origin,
        runtime.repository,
        runtime.architecture,
    )
    oras = ORASClient(
        ORASPolicy(
            runtime.registry_origin,
            runtime.repository,
            runtime.oras.executable,
            runtime.oras.sha256,
            runtime.oras.version,
            runtime.oras.auth_path,
            config.ca_path,
            config.certificate_path,
            config.private_key_path,
            allow_unprivileged_test_files=runtime.allow_unprivileged_test_files,
            credential_provider=credentials,
        )
    )
    releases = ReleaseInstaller(
        trust,
        oras,
        runtime.release_root,
        runtime.staging_root,
    )
    workloads = WorkloadOperations(runtime.release_root, trust)
    context = OperationContext(
        node_id=config.node_id,
        state=state,
        probe=PinnedNodeProbe(policy),
        releases=releases,
        workloads=workloads,
    )
    return Agent(
        client,
        OperationRegistry(),
        context,
        backoff_min_seconds=config.poll_min_seconds,
        backoff_max_seconds=config.poll_max_seconds,
        credentials=credentials,
        on_authenticated_exchange=(
            readiness or ReadinessReporter.from_environment()
        ).report,
    )


class EnrollmentControl(Protocol):
    def enroll(
        self,
        enrollment_origin: str,
        grant_token: str,
        csr: bytes,
        evidence: Mapping[str, object],
    ) -> EnrollmentPending | IssuedCredential: ...


def ensure_initial_enrollment(
    config: AgentConfig,
    credentials: CredentialStore,
    client: EnrollmentControl,
    evidence: Mapping[str, object],
) -> bool:
    """Attempt one idempotent initial enrollment pickup."""
    if credentials.has_active_credentials:
        return True
    pending = credentials.prepare_enrollment(config.node_id)
    request = x509.load_pem_x509_csr(pending.csr_pem)
    public = request.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    exact_evidence = dict(evidence)
    exact_evidence["node_id"] = config.node_id
    exact_evidence["csr_public_key_fingerprint"] = hashlib.sha256(public).hexdigest()
    if set(exact_evidence) != {
        "agent_digest",
        "boot_id",
        "csr_public_key_fingerprint",
        "hardware_fingerprint",
        "host_key_fingerprint",
        "node_id",
    }:
        raise RuntimeError("initial enrollment evidence fields are invalid")
    directory, token, identity = _open_enrollment_token(config.enrollment_token_path)
    try:
        response = client.enroll(
            config.enrollment_origin,
            token,
            pending.csr_pem,
            exact_evidence,
        )
        if isinstance(response, EnrollmentPending):
            return False
        credentials.install_initial(response)
        current = os.stat(
            config.enrollment_token_path.name,
            dir_fd=directory,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("enrollment token changed before consumption")
        os.unlink(config.enrollment_token_path.name, dir_fd=directory)
        os.fsync(directory)
        return True
    finally:
        os.close(directory)


def remove_consumed_enrollment_token(
    config: AgentConfig, credentials: CredentialStore
) -> bool:
    """Finish token consumption after a crash beyond active publication."""
    if not credentials.has_published_credentials:
        return False
    try:
        directory, _token, identity = _open_enrollment_token(
            config.enrollment_token_path
        )
    except FileNotFoundError:
        return False
    try:
        current = os.stat(
            config.enrollment_token_path.name,
            dir_fd=directory,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("enrollment token changed before recovery consumption")
        os.unlink(config.enrollment_token_path.name, dir_fd=directory)
        os.fsync(directory)
        return True
    finally:
        os.close(directory)


def _open_enrollment_token(path: Path) -> tuple[int, str, tuple[int, int]]:
    if not path.is_absolute() or len(path.parts) < 2:
        raise RuntimeError("enrollment token path is invalid")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = -1
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
            metadata = os.fstat(directory)
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid not in {0, os.geteuid()} or (
                mode & 0o022 and not mode & stat.S_ISVTX
            ):
                raise RuntimeError("enrollment token ancestry is unsafe")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 128
        ):
            raise RuntimeError("enrollment token is unsafe")
        raw = os.read(descriptor, 129)
        match = re.fullmatch(rb"([A-Za-z0-9_-]{43})\n?", raw)
        if match is None:
            raise RuntimeError("enrollment token is invalid")
        return (
            directory,
            match.group(1).decode("ascii"),
            (metadata.st_dev, metadata.st_ino),
        )
    except Exception:
        os.close(directory)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bounded_text(path: Path, fallback: str) -> str:
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = -1
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
            metadata = os.fstat(directory)
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid != 0 or (mode & 0o022 and not mode & stat.S_ISVTX):
                return fallback
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > 512
        ):
            return fallback
        raw = os.read(descriptor, 513)
    except OSError:
        return fallback
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)
    if not 0 < len(raw) <= 512:
        return fallback
    value = raw.strip()
    try:
        return value.decode("ascii", "strict") if value else fallback
    except UnicodeDecodeError:
        return fallback


def _enrollment_evidence() -> dict[str, object]:
    agent_digest = os.environ.get("DGX_AGENT_SUPERVISOR_SHA256", "")
    if re.fullmatch(r"[0-9a-f]{64}", agent_digest) is None:
        raise RuntimeError("supervised agent digest is unavailable")
    machine = _bounded_text(Path("/etc/machine-id"), "unavailable")
    host = _bounded_text(Path("/etc/ssh/ssh_host_ed25519_key.pub"), "unavailable")
    return {
        "agent_digest": agent_digest,
        "boot_id": _bounded_text(
            Path("/proc/sys/kernel/random/boot_id"), "unavailable"
        ),
        "csr_public_key_fingerprint": "0" * 64,
        "hardware_fingerprint": hashlib.sha256(machine.encode("ascii")).hexdigest(),
        "host_key_fingerprint": hashlib.sha256(host.encode("ascii")).hexdigest(),
        "node_id": "pending",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DGX Forge outbound Spark agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="absolute path to the restrictive agent configuration",
    )
    arguments = parser.parse_args(argv)
    config = AgentConfig.load(arguments.config)
    stop = threading.Event()

    def terminate(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    readiness = ReadinessReporter.from_environment()
    credentials = CredentialStore(
        config.state_root,
        config.ca_path,
        config.certificate_path,
        config.private_key_path,
    )
    enrollment_client = AgentClient(
        config.control_origin,
        config.node_id,
        credentials,
        long_poll_seconds=min(60, config.poll_max_seconds),
        lease_seconds=max(30, min(300, config.poll_max_seconds * 2)),
    )
    while not credentials.has_active_credentials and not stop.is_set():
        try:
            enrolled = ensure_initial_enrollment(
                config,
                credentials,
                enrollment_client,
                _enrollment_evidence(),
            )
        except AgentTransportError:
            enrolled = False
        if enrolled:
            break
        stop.wait(config.poll_min_seconds)
    if stop.is_set():
        return 0
    credentials.recover_initial_enrollment(config.node_id)
    remove_consumed_enrollment_token(config, credentials)
    build_agent(config, credentials=credentials, readiness=readiness).run_forever(stop)
    return 0
