"""Ed25519 authority for root-verifiable GPU node agent activation receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from tuf.api.exceptions import DownloadHTTPError
from tuf.ngclient import FetcherInterface

from cluster_profiles.platform_release import PlatformRelease, PlatformReleaseError
from cluster_profiles.update_trust import UpdateTrust, UpdateTrustError

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PREFIXED_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_MAX_KEY_BYTES = 16 * 1024
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_MAX_TARGET_BYTES = 16 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_MAX_RECEIPT_SECONDS = 600
_TUF_METADATA_NAME = re.compile(
    r"(?:[1-9][0-9]*\.root|root|timestamp|"
    r"(?:[1-9][0-9]*\.)?snapshot|(?:[1-9][0-9]*\.)?targets)\.json\Z"
)
_VERSIONED_TARGET_NAME = re.compile(
    r"platform/releases/"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)/"
    r"(?P<sha256>[0-9a-f]{64})\.json\Z"
)
_VERSIONED_TARGET_DOWNLOAD_NAME = re.compile(
    r"platform/releases/"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)/"
    r"(?:(?P<prefix>[0-9a-f]{64})\.)?(?P<sha256>[0-9a-f]{64})\.json\Z"
)


class UpdateAuthorizationError(RuntimeError):
    """An activation receipt cannot be issued from the authorized inputs."""


class _PublishedInputMissing(UpdateAuthorizationError):
    pass


class VerifiedReleaseSource(Protocol):
    def refresh(self, target_name: str) -> tuple[bytes, int]: ...


@dataclass(frozen=True)
class PreparedUpdateAuthorization:
    payload_digest: str
    release: PlatformRelease
    target_name: str
    target_sha256: str
    targets_version: int


class _PublishedTufFetcher(FetcherInterface):
    def __init__(self, metadata_root: Path, target_root: Path) -> None:
        self._metadata_root = Path(metadata_root)
        self._target_root = Path(target_root)

    def _fetch(self, url: str):
        parsed = urlsplit(url)
        metadata = "/metadata/" in parsed.path
        if metadata:
            name = parsed.path.rsplit("/", 1)[-1]
            if _TUF_METADATA_NAME.fullmatch(name) is None:
                raise UpdateAuthorizationError("platform TUF fetch name is invalid")
        else:
            marker = "/platform/targets/"
            if marker not in parsed.path:
                raise UpdateAuthorizationError("platform TUF fetch name is invalid")
            name = parsed.path.split(marker, 1)[1]
            match = _VERSIONED_TARGET_DOWNLOAD_NAME.fullmatch(name)
            if match is None or (
                match.group("prefix") is not None
                and match.group("prefix") != match.group("sha256")
            ):
                raise UpdateAuthorizationError("platform TUF fetch name is invalid")
        root = self._metadata_root if metadata else self._target_root
        maximum = _MAX_METADATA_BYTES if metadata else _MAX_TARGET_BYTES
        try:
            yield _snapshot_relative(
                root,
                name,
                "published platform TUF input",
                maximum,
            )
        except _PublishedInputMissing as error:
            if re.fullmatch(r"[1-9][0-9]*\.root\.json", name):
                raise DownloadHTTPError("no newer root", 404) from error
            raise


class PublishedTUFReleaseSource:
    """Verify the NAS publication through a distinct persistent python-tuf cache."""

    def __init__(
        self,
        *,
        publication_metadata_root: Path,
        publication_target_root: Path,
        verified_metadata_root: Path,
        verified_target_root: Path,
        bootstrap_root: bytes,
    ) -> None:
        self._verified_metadata_root = Path(verified_metadata_root)
        self._trust = UpdateTrust(
            metadata_root=verified_metadata_root,
            target_root=verified_target_root,
            metadata_base_url="https://control.invalid/platform/metadata/",
            target_base_url="https://control.invalid/platform/targets/",
            bootstrap_root=bootstrap_root,
            fetcher=_PublishedTufFetcher(
                publication_metadata_root, publication_target_root
            ),
        )

    def refresh(self, target_name: str) -> tuple[bytes, int]:
        match = (
            _VERSIONED_TARGET_NAME.fullmatch(target_name)
            if isinstance(target_name, str)
            else None
        )
        if match is None:
            raise UpdateAuthorizationError("platform TUF target name is invalid")
        try:
            target, version = self._trust.refresh_and_trusted_target(target_name)
        except (OSError, TypeError, ValueError, UpdateTrustError) as error:
            raise UpdateAuthorizationError(
                "platform TUF authorization failed"
            ) from error
        _targets_version(version)
        if match.group("sha256") != target.sha256:
            raise UpdateAuthorizationError("platform TUF target name digest is invalid")
        return target.data, version


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateAuthorizationError(
                "update authority JSON contains duplicate fields"
            )
        result[key] = value
    return result


def _document(raw: bytes, name: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except UpdateAuthorizationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise UpdateAuthorizationError(f"{name} is invalid") from error
    if not isinstance(value, dict):
        raise UpdateAuthorizationError(f"{name} is invalid")
    return value


def _snapshot_descriptor(
    descriptor: int,
    name: str,
    maximum: int,
    *,
    private: bool = False,
) -> bytes:
    before = os.fstat(descriptor)
    mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum
        or mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (private and mode & (stat.S_IRWXG | stat.S_IRWXO))
    ):
        raise UpdateAuthorizationError(f"{name} is unsafe")
    raw = bytearray()
    while len(raw) <= maximum:
        chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
    after = os.fstat(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if len(raw) > maximum or identity(before) != identity(after):
        raise UpdateAuthorizationError(f"{name} changed while being read")
    return bytes(raw)


def _snapshot(path: Path, name: str, maximum: int, *, private: bool = False) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except FileNotFoundError as error:
        raise _PublishedInputMissing(f"{name} is unavailable") from error
    except OSError as error:
        raise UpdateAuthorizationError(f"{name} is unavailable") from error
    try:
        return _snapshot_descriptor(descriptor, name, maximum, private=private)
    except OSError as error:
        raise UpdateAuthorizationError(f"{name} cannot be read safely") from error
    finally:
        os.close(descriptor)


def _snapshot_relative(root: Path, relative: str, name: str, maximum: int) -> bytes:
    components = relative.split("/")
    if (
        not root.is_absolute()
        or not components
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise UpdateAuthorizationError(f"{name} is unavailable")
    descriptor = -1
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        for component in components[:-1]:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise UpdateAuthorizationError(f"{name} directory is unsafe")
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise UpdateAuthorizationError(f"{name} directory is unsafe")
        file_descriptor = os.open(
            components[-1],
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=descriptor,
        )
        try:
            return _snapshot_descriptor(file_descriptor, name, maximum)
        finally:
            os.close(file_descriptor)
    except FileNotFoundError as error:
        raise _PublishedInputMissing(f"{name} is unavailable") from error
    except UpdateAuthorizationError:
        raise
    except OSError as error:
        raise UpdateAuthorizationError(f"{name} is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def snapshot_public_trust_root(path: Path) -> bytes:
    """Take a bounded, identity-stable snapshot of the bootstrap trust root."""
    return _snapshot(Path(path), "platform TUF bootstrap root", 256 * 1024)


def _uuid4(value: object, name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise UpdateAuthorizationError(f"{name} is invalid") from error
    if parsed.version != 4 or str(parsed) != value:
        raise UpdateAuthorizationError(f"{name} is invalid")
    return str(parsed)


def _targets_version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise UpdateAuthorizationError("platform TUF targets version is invalid")
    return value


class UpdateAuthorizationAuthority:
    """Issue short-lived receipts bound to one exact operation attempt."""

    def __init__(
        self,
        private_key: ed25519.Ed25519PrivateKey,
        *,
        release_source: VerifiedReleaseSource,
    ) -> None:
        self._private_key = private_key
        self._release_source = release_source
        public = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self._public_key = public
        self.key_id = hashlib.sha256(public).hexdigest()

    @classmethod
    def from_private_key_file(
        cls,
        path: Path,
        *,
        release_source: VerifiedReleaseSource,
    ) -> UpdateAuthorizationAuthority:
        raw = _snapshot(
            Path(path), "update authority private key", _MAX_KEY_BYTES, private=True
        )
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError) as error:
            raise UpdateAuthorizationError(
                "update authority private key is invalid"
            ) from error
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise UpdateAuthorizationError(
                "update authority private key must be Ed25519"
            )
        return cls(key, release_source=release_source)

    def public_authority_document(self) -> dict[str, object]:
        return {
            "algorithm": "ed25519",
            "key_id": self.key_id,
            "public_key": self._public_key.hex(),
            "schema_version": 1,
        }

    def public_authority_bytes(self) -> bytes:
        return _canonical(self.public_authority_document())

    def refresh_and_validate(
        self, payload: Mapping[str, object], *, target_name: str
    ) -> PreparedUpdateAuthorization:
        """Refresh the trusted release input before any operation lock is held."""
        target, version = self._release_source.refresh(target_name)
        _targets_version(version)
        target_sha256 = hashlib.sha256(target).hexdigest()
        try:
            release = PlatformRelease.from_bytes(target)
            release.validate_target_identity(target_name, target_sha256)
        except PlatformReleaseError as error:
            raise UpdateAuthorizationError(
                "platform release target identity is invalid"
            ) from error
        self._validate_release_payload(payload, release)
        return PreparedUpdateAuthorization(
            payload_digest=hashlib.sha256(_canonical(payload)).hexdigest(),
            release=release,
            target_name=target_name,
            target_sha256=target_sha256,
            targets_version=version,
        )

    def authorize(
        self,
        payload: Mapping[str, object],
        *,
        operation_id: str,
        fence: str,
        expires_at: int,
        previous_slot: str,
        previous_sha256: str,
        previous_generation: int,
        node_id: str,
        attempt: int,
        claim_deadline: int,
        prepared: PreparedUpdateAuthorization,
        now: datetime,
    ) -> dict[str, object]:
        artifact, release = self._update_payload(payload)
        operation_id = _uuid4(operation_id, "update operation ID")
        fence = _uuid4(fence, "update operation fence")
        if now.tzinfo is None:
            raise UpdateAuthorizationError("update authorization clock is naive")
        issued_at = int(now.astimezone(UTC).timestamp())
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or not issued_at < expires_at <= issued_at + _MAX_RECEIPT_SECONDS
        ):
            raise UpdateAuthorizationError("update authorization expiry is invalid")
        if (
            previous_slot not in {"A", "B"}
            or _DIGEST.fullmatch(previous_sha256) is None
        ):
            raise UpdateAuthorizationError("update source slot identity is invalid")
        if type(prepared) is not PreparedUpdateAuthorization:
            raise UpdateAuthorizationError(
                "platform release authorization is not prepared"
            )
        try:
            prepared.release.validate_target_identity(
                prepared.target_name, prepared.target_sha256
            )
            _targets_version(prepared.targets_version)
        except (PlatformReleaseError, UpdateAuthorizationError) as error:
            raise UpdateAuthorizationError(
                "platform release authorization is not prepared"
            ) from error
        if prepared.payload_digest != hashlib.sha256(_canonical(payload)).hexdigest():
            raise UpdateAuthorizationError(
                "platform release authorization is not prepared"
            )
        self._validate_release_payload(payload, prepared.release)
        if (
            not isinstance(node_id, str)
            or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt != 1
            or isinstance(previous_generation, bool)
            or not isinstance(previous_generation, int)
            or not 1 <= previous_generation <= 999_999_999
            or claim_deadline != expires_at
        ):
            raise UpdateAuthorizationError("update claim binding is invalid")
        receipt = {
            "architecture": artifact["architecture"],
            "attempt": attempt,
            "build_digest": release["build_digest"],
            "claim_deadline": claim_deadline,
            "expires_at": expires_at,
            "fence": fence,
            "node_id": node_id,
            "oci_manifest_digest": artifact["oci_manifest_digest"],
            "operation_id": operation_id,
            "payload_name": artifact["payload_name"],
            "platform_target_name": prepared.target_name,
            "platform_target_sha256": prepared.target_sha256,
            "platform_version": release["platform_version"],
            "previous_sha256": previous_sha256,
            "previous_generation": previous_generation,
            "previous_slot": previous_slot,
            "sha256": artifact["payload_sha256"],
            "size": artifact["payload_size"],
            "target_slot": "B" if previous_slot == "A" else "A",
            "tuf_targets_version": prepared.targets_version,
        }
        signature = self._private_key.sign(_canonical(receipt))
        return {
            "artifact": artifact,
            "receipt": receipt,
            "release": release,
            "signature": {
                "algorithm": "ed25519",
                "key_id": self.key_id,
                "value": signature.hex(),
            },
        }

    def authorize_rollback(
        self,
        *,
        operation_id: str,
        fence: str,
        expires_at: int,
        current_slot: str,
        current_sha256: str,
        current_generation: int,
        node_id: str,
        attempt: int,
        claim_deadline: int,
        now: datetime,
    ) -> dict[str, object]:
        """Authorize one operator-requested rollback against observed GPU node state."""
        operation_id = _uuid4(operation_id, "rollback operation ID")
        fence = _uuid4(fence, "rollback operation fence")
        if now.tzinfo is None:
            raise UpdateAuthorizationError("rollback authorization clock is naive")
        issued_at = int(now.astimezone(UTC).timestamp())
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or not issued_at < expires_at <= issued_at + _MAX_RECEIPT_SECONDS
            or claim_deadline != expires_at
            or current_slot not in {"A", "B"}
            or not isinstance(current_sha256, str)
            or _DIGEST.fullmatch(current_sha256) is None
            or isinstance(current_generation, bool)
            or not isinstance(current_generation, int)
            or not 1 <= current_generation <= 999_999_999
            or not isinstance(node_id, str)
            or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
            or isinstance(attempt, bool)
            or attempt != 1
        ):
            raise UpdateAuthorizationError("rollback claim binding is invalid")
        receipt = {
            "action": "operator-rollback",
            "attempt": attempt,
            "claim_deadline": claim_deadline,
            "current_generation": current_generation,
            "current_sha256": current_sha256,
            "current_slot": current_slot,
            "expires_at": expires_at,
            "fence": fence,
            "node_id": node_id,
            "operation_id": operation_id,
        }
        signature = self._private_key.sign(_canonical(receipt))
        return {
            "receipt": receipt,
            "signature": {
                "algorithm": "ed25519",
                "key_id": self.key_id,
                "value": signature.hex(),
            },
        }

    @staticmethod
    def _update_payload(
        payload: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        if not isinstance(payload, Mapping) or set(payload) != {"artifact", "release"}:
            raise UpdateAuthorizationError("unsigned agent update payload is invalid")
        artifact = payload["artifact"]
        release = payload["release"]
        if not isinstance(artifact, Mapping) or set(artifact) != {
            "architecture",
            "oci_manifest_digest",
            "payload_name",
            "payload_sha256",
            "payload_size",
        }:
            raise UpdateAuthorizationError("agent update artifact is invalid")
        if not isinstance(release, Mapping) or set(release) != {
            "build_digest",
            "platform_version",
            "protocol_maximum",
            "protocol_minimum",
        }:
            raise UpdateAuthorizationError("agent update release is invalid")
        artifact = dict(artifact)
        release = dict(release)
        size = artifact["payload_size"]
        protocol_minimum = release["protocol_minimum"]
        protocol_maximum = release["protocol_maximum"]
        if (
            artifact["architecture"] not in {"linux-arm64", "linux-x86_64"}
            or not isinstance(artifact["oci_manifest_digest"], str)
            or _PREFIXED_DIGEST.fullmatch(artifact["oci_manifest_digest"]) is None
            or not isinstance(artifact["payload_name"], str)
            or _TOKEN.fullmatch(artifact["payload_name"]) is None
            or not isinstance(artifact["payload_sha256"], str)
            or _DIGEST.fullmatch(artifact["payload_sha256"]) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 64 <= size <= _MAX_ARTIFACT_BYTES
            or not isinstance(release["build_digest"], str)
            or _PREFIXED_DIGEST.fullmatch(release["build_digest"]) is None
            or not isinstance(release["platform_version"], str)
            or _SEMVER.fullmatch(release["platform_version"]) is None
            or isinstance(protocol_minimum, bool)
            or isinstance(protocol_maximum, bool)
            or not isinstance(protocol_minimum, int)
            or not isinstance(protocol_maximum, int)
            or not 1 <= protocol_minimum <= protocol_maximum <= 65535
        ):
            raise UpdateAuthorizationError("agent update payload values are invalid")
        return artifact, release

    @staticmethod
    def _validate_release_payload(
        payload: Mapping[str, object], release: PlatformRelease
    ) -> None:
        artifact, identity = UpdateAuthorizationAuthority._update_payload(payload)
        try:
            published = release.agent_for(str(artifact["architecture"]))
        except PlatformReleaseError as error:
            raise UpdateAuthorizationError(
                "agent architecture is not TUF-published"
            ) from error
        protocol = published.protocol
        reference_digest = published.artifact.reference.rsplit("@", 1)[-1]
        if (
            release.platform_version != identity["platform_version"]
            or release.build_digest != identity["build_digest"]
            or published.payload_name != artifact["payload_name"]
            or published.payload_sha256 != artifact["payload_sha256"]
            or published.payload_size != artifact["payload_size"]
            or reference_digest != artifact["oci_manifest_digest"]
            or protocol is None
            or protocol.minimum != identity["protocol_minimum"]
            or protocol.maximum != identity["protocol_maximum"]
        ):
            raise UpdateAuthorizationError(
                "agent update payload disagrees with TUF platform release"
            )


def export_public_authority(private_key_file: Path) -> bytes:
    """Derive the canonical GPU node trust document without exporting private material."""
    raw = _snapshot(
        Path(private_key_file),
        "update authority private key",
        _MAX_KEY_BYTES,
        private=True,
    )
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError) as error:
        raise UpdateAuthorizationError(
            "update authority private key is invalid"
        ) from error
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise UpdateAuthorizationError("update authority private key must be Ed25519")
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return _canonical(
        {
            "algorithm": "ed25519",
            "key_id": hashlib.sha256(public).hexdigest(),
            "public_key": public.hex(),
            "schema_version": 1,
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="export the public GPU node update authority from an Ed25519 key"
    )
    parser.add_argument("--private-key-file", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        document = export_public_authority(arguments.private_key_file)
    except UpdateAuthorizationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
