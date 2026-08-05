"""Signed inactive-slot acquisition and supervisor activation requests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import struct
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PREFIXED_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_REFERENCE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9._/-]*@sha256:([0-9a-f]{64})\Z")
_MACHINE = {"linux-x86_64": 62, "linux-arm64": 183}
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class AgentUpdateError(RuntimeError):
    """An agent update cannot proceed safely."""


@dataclass(frozen=True)
class AgentArtifact:
    architecture: str
    reference: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        if self.architecture not in _MACHINE:
            raise AgentUpdateError("agent artifact architecture is invalid")
        match = _REFERENCE.fullmatch(self.reference)
        if match is None or match.group(1) != self.sha256:
            raise AgentUpdateError("agent artifact reference digest is invalid")
        if _DIGEST.fullmatch(self.sha256) is None:
            raise AgentUpdateError("agent artifact digest is invalid")
        if isinstance(self.size, bool) or not 64 <= self.size <= _MAX_ARTIFACT_BYTES:
            raise AgentUpdateError("agent artifact size is invalid")


@dataclass(frozen=True)
class AgentReleaseIdentity:
    platform_version: str
    build_digest: str
    protocol_minimum: int
    protocol_maximum: int

    def __post_init__(self) -> None:
        if _SEMVER.fullmatch(self.platform_version) is None:
            raise AgentUpdateError("platform version is invalid")
        if _PREFIXED_DIGEST.fullmatch(self.build_digest) is None:
            raise AgentUpdateError("platform build digest is invalid")
        if (
            isinstance(self.protocol_minimum, bool)
            or isinstance(self.protocol_maximum, bool)
            or not isinstance(self.protocol_minimum, int)
            or not isinstance(self.protocol_maximum, int)
            or not 1 <= self.protocol_minimum <= self.protocol_maximum <= 65535
        ):
            raise AgentUpdateError("agent protocol range is invalid")


@dataclass(frozen=True)
class SupervisorSlotState:
    active_slot: str
    previous_slot: str | None
    status: str
    slot_sha256: Mapping[str, str | None]

    def __post_init__(self) -> None:
        if self.active_slot not in {"A", "B"}:
            raise AgentUpdateError("supervisor active slot is invalid")
        if self.previous_slot is not None and self.previous_slot not in {"A", "B"}:
            raise AgentUpdateError("supervisor previous slot is invalid")
        if self.status not in {"stable", "pending"}:
            raise AgentUpdateError("supervisor status is invalid")
        if set(self.slot_sha256) != {"A", "B"}:
            raise AgentUpdateError("supervisor slot digests are invalid")
        for digest in self.slot_sha256.values():
            if digest is not None and _DIGEST.fullmatch(digest) is None:
                raise AgentUpdateError("supervisor slot digest is invalid")


@dataclass(frozen=True)
class UpdatePlan:
    artifact: AgentArtifact
    release: AgentReleaseIdentity
    previous_slot: str
    target_slot: str
    plan_digest: str


@dataclass(frozen=True)
class PendingActivation:
    previous_slot: str
    target_slot: str
    artifact_sha256: str
    platform_version: str
    build_digest: str
    status: str = "pending-activation"


class UpdateTrustBoundary(Protocol):
    def authorize(
        self, artifact: AgentArtifact, release: AgentReleaseIdentity
    ) -> None: ...


class UpdateTransportBoundary(Protocol):
    def fetch(self, artifact: AgentArtifact, destination: Path) -> None: ...


class SupervisorBoundary(Protocol):
    def inspect(self) -> SupervisorSlotState: ...

    def notify(self, request_path: Path) -> None: ...


class AgentUpdater:
    def __init__(
        self,
        *,
        architecture: str,
        protocol_version: int,
        staging_root: Path,
        runtime_root: Path,
        trust: UpdateTrustBoundary,
        transport: UpdateTransportBoundary,
        supervisor: SupervisorBoundary,
        available_bytes: Callable[[], int],
    ) -> None:
        if architecture not in _MACHINE:
            raise AgentUpdateError("local architecture is unsupported")
        if isinstance(protocol_version, bool) or not 1 <= protocol_version <= 65535:
            raise AgentUpdateError("local protocol version is invalid")
        self._architecture = architecture
        self._protocol_version = protocol_version
        self._staging_root = Path(staging_root)
        self._runtime_root = Path(runtime_root)
        self._trust = trust
        self._transport = transport
        self._supervisor = supervisor
        self._available_bytes = available_bytes

    def plan(
        self, artifact: AgentArtifact, release: AgentReleaseIdentity
    ) -> UpdatePlan:
        if artifact.architecture != self._architecture:
            raise AgentUpdateError("agent artifact architecture is incompatible")
        if not release.protocol_minimum <= self._protocol_version <= release.protocol_maximum:
            raise AgentUpdateError("agent protocol is incompatible")
        state = self._supervisor.inspect()
        if state.status != "stable":
            raise AgentUpdateError("supervisor must be stable before update")
        if state.slot_sha256[state.active_slot] is None:
            raise AgentUpdateError("active supervisor slot is unavailable")
        available = self._available_bytes()
        if isinstance(available, bool) or not isinstance(available, int) or available < artifact.size * 2:
            raise AgentUpdateError("insufficient disk space for agent update")
        self._trust.authorize(artifact, release)
        target = "B" if state.active_slot == "A" else "A"
        content = {
            "artifact": asdict(artifact),
            "previous_slot": state.active_slot,
            "release": asdict(release),
            "target_slot": target,
        }
        digest = hashlib.sha256(_canonical(content)).hexdigest()
        return UpdatePlan(artifact, release, state.active_slot, target, f"sha256:{digest}")

    def apply(self, plan: UpdatePlan) -> PendingActivation:
        if plan != self.plan(plan.artifact, plan.release):
            raise AgentUpdateError("agent update plan is stale")
        _secure_directory(self._staging_root)
        _secure_directory(self._runtime_root)
        final = self._staging_root / f"{plan.artifact.sha256}.agent"
        temporary = self._staging_root / f".{plan.artifact.sha256}.{secrets.token_hex(8)}.partial"
        try:
            if not final.exists():
                self._transport.fetch(plan.artifact, temporary)
                _verify_artifact(temporary, plan.artifact, self._architecture)
                os.chmod(temporary, 0o500)
                descriptor = os.open(temporary, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                try:
                    os.link(temporary, final, follow_symlinks=False)
                except FileExistsError:
                    _verify_artifact(final, plan.artifact, self._architecture)
                _fsync_directory(self._staging_root)
            else:
                _verify_artifact(final, plan.artifact, self._architecture)
            request = {
                "build_digest": plan.release.build_digest,
                "platform_version": plan.release.platform_version,
                "previous_slot": plan.previous_slot,
                "schema_version": 1,
                "sha256": plan.artifact.sha256,
                "size": plan.artifact.size,
                "target_slot": plan.target_slot,
            }
            request_path = self._runtime_root / "activation-request.json"
            _write_atomic(request_path, _canonical(request))
            self._supervisor.notify(request_path)
            return PendingActivation(
                previous_slot=plan.previous_slot,
                target_slot=plan.target_slot,
                artifact_sha256=plan.artifact.sha256,
                platform_version=plan.release.platform_version,
                build_digest=plan.release.build_digest,
            )
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _verify_artifact(path: Path, artifact: AgentArtifact, architecture: str) -> None:
    try:
        if path.is_symlink():
            raise AgentUpdateError("agent artifact staging path is unsafe")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != artifact.size:
            raise AgentUpdateError("agent artifact size or type is invalid")
        content = path.read_bytes()
    except OSError as error:
        raise AgentUpdateError("agent artifact is unavailable") from error
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise AgentUpdateError("agent artifact digest is invalid")
    if len(content) < 20 or content[:7] != b"\x7fELF\x02\x01\x01":
        raise AgentUpdateError("agent artifact is not a supported ELF")
    elf_type, machine = struct.unpack_from("<HH", content, 16)
    if elf_type not in {2, 3} or machine != _MACHINE[architecture]:
        raise AgentUpdateError("agent artifact ELF architecture is incompatible")


def _secure_directory(path: Path) -> None:
    if not path.is_absolute():
        raise AgentUpdateError("agent update path must be absolute")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise AgentUpdateError("agent update directory is unsafe") from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise AgentUpdateError("agent update directory is unsafe")
    os.chmod(path, 0o700)


def _write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.new")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
