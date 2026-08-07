"""Networkless workload-TUF publication signer boundary.

The control API owns previews and Git policy, but never receives online
workload-TUF private keys.  This module is the small Unix-socket protocol used
by the NAS workload signer container.  It accepts only an exact release lock,
eligible Git commit, and bounded evidence document, then delegates signing to
the workload-only :class:`WorkloadTrustPublisher`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import stat
import struct
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from securesystemslib.signer import CryptoSigner, SSlibKey

from .workload_trust import (
    TrustedWorkloadTarget,
    WorkloadOnlineSigners,
    WorkloadTrustError,
)

MAX_WORKLOAD_SIGNER_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_WORKLOAD_LOCK_BYTES = 1024 * 1024
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class WorkloadSignerProtocolError(RuntimeError):
    """A workload signer request or response crossed its closed ABI."""


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise WorkloadSignerProtocolError("workload signer message is not canonical") from error


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkloadSignerProtocolError("workload signer message has duplicate fields")
        result[key] = value
    return result


def _document(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_WORKLOAD_SIGNER_MESSAGE_BYTES or not raw.endswith(b"\n"):
        raise WorkloadSignerProtocolError("workload signer message bounds are invalid")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise WorkloadSignerProtocolError("workload signer message is invalid JSON") from error
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise WorkloadSignerProtocolError("workload signer message is not canonical")
    return value


def _read_message(connection: socket.socket, *, deadline: float | None = None) -> bytes:
    content = bytearray()
    while len(content) <= MAX_WORKLOAD_SIGNER_MESSAGE_BYTES:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkloadSignerProtocolError("workload signer message timeout")
            connection.settimeout(remaining)
        try:
            chunk = connection.recv(
                min(16 * 1024, MAX_WORKLOAD_SIGNER_MESSAGE_BYTES + 1 - len(content))
            )
        except (OSError, TimeoutError) as error:
            raise WorkloadSignerProtocolError("workload signer peer disconnected") from error
        if not chunk:
            break
        content.extend(chunk)
    if len(content) > MAX_WORKLOAD_SIGNER_MESSAGE_BYTES:
        raise WorkloadSignerProtocolError("workload signer message is too large")
    return bytes(content)


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise WorkloadSignerProtocolError(f"{label} is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise WorkloadSignerProtocolError(f"{label} is invalid") from error
    if parsed.version != 4 or str(parsed) != value:
        raise WorkloadSignerProtocolError(f"{label} is invalid")
    return value


def _validate_request(value: Mapping[str, object]) -> tuple[str, bytes, str, dict[str, object]]:
    if set(value) != {"schema_version", "intent_id", "action", "lock", "git_commit", "evidence"}:
        raise WorkloadSignerProtocolError("workload signer request fields are invalid")
    if value.get("schema_version") != 1 or value.get("action") != "workload.publish":
        raise WorkloadSignerProtocolError("workload signer request identity is invalid")
    intent_id = _uuid(value.get("intent_id"), "workload signer intent ID")
    encoded = value.get("lock")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_WORKLOAD_LOCK_BYTES + 2) // 3) * 4:
        raise WorkloadSignerProtocolError("workload signer lock is invalid")
    try:
        lock = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise WorkloadSignerProtocolError("workload signer lock is invalid") from error
    if not 0 < len(lock) <= MAX_WORKLOAD_LOCK_BYTES:
        raise WorkloadSignerProtocolError("workload signer lock is invalid")
    commit = value.get("git_commit")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise WorkloadSignerProtocolError("workload signer Git commit is invalid")
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping) or not all(isinstance(key, str) for key in evidence):
        raise WorkloadSignerProtocolError("workload signer evidence is invalid")
    try:
        evidence_copy = json.loads(json.dumps(dict(evidence), sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError, RecursionError) as error:
        raise WorkloadSignerProtocolError("workload signer evidence is invalid") from error
    if not isinstance(evidence_copy, dict) or len(_canonical(evidence_copy)) > 64 * 1024:
        raise WorkloadSignerProtocolError("workload signer evidence is invalid")
    return intent_id, lock, commit, evidence_copy


def _target_document(target: object) -> dict[str, object]:
    digest = target.get("digest") if isinstance(target, Mapping) else getattr(target, "digest", None)
    length = target.get("length") if isinstance(target, Mapping) else getattr(target, "length", None)
    commit = target.get("git_commit") if isinstance(target, Mapping) else getattr(target, "git_commit", None)
    version = target.get("tuf_snapshot_version") if isinstance(target, Mapping) else getattr(target, "tuf_snapshot_version", None)
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise WorkloadSignerProtocolError("workload signer target digest is invalid")
    if isinstance(length, bool) or not isinstance(length, int) or length < 1 or length > MAX_WORKLOAD_LOCK_BYTES:
        raise WorkloadSignerProtocolError("workload signer target length is invalid")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise WorkloadSignerProtocolError("workload signer target commit is invalid")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise WorkloadSignerProtocolError("workload signer target version is invalid")
    return {
        "digest": digest,
        "length": length,
        "git_commit": commit,
        "tuf_snapshot_version": version,
    }


class WorkloadPublicationSignerPolicy:
    """Closed publish-only policy around a workload TUF publisher."""

    def __init__(self, publisher: Any) -> None:
        if not callable(getattr(publisher, "publish", None)):
            raise TypeError("workload TUF publisher is invalid")
        self._publisher = publisher

    def publish(self, lock: bytes, commit: str, evidence: Mapping[str, object]) -> dict[str, object]:
        try:
            target = self._publisher.publish(lock, commit, evidence)
        except (WorkloadTrustError, ValueError, TypeError) as error:
            raise WorkloadSignerProtocolError("workload publication was rejected") from error
        document = _target_document(target)
        if document["digest"] != hashlib.sha256(lock).hexdigest() or document["length"] != len(lock):
            raise WorkloadSignerProtocolError("workload publication target digest changed")
        return document


class WorkloadSignerConnectionHandler:
    def __init__(self, policy: WorkloadPublicationSignerPolicy | Any, *, allowed_peer_uid: int, request_timeout_seconds: float = 5.0) -> None:
        if not callable(getattr(policy, "publish", None)):
            raise TypeError("workload signer policy is invalid")
        if isinstance(allowed_peer_uid, bool) or not isinstance(allowed_peer_uid, int) or allowed_peer_uid < 0:
            raise ValueError("workload signer peer UID is invalid")
        if not 0 < request_timeout_seconds <= 30:
            raise ValueError("workload signer request timeout is invalid")
        self._policy = policy
        self._allowed_peer_uid = allowed_peer_uid
        self._timeout = float(request_timeout_seconds)

    def handle(self, connection: socket.socket) -> None:
        try:
            _pid, uid, _gid = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            )
        except (AttributeError, OSError, struct.error) as error:
            raise WorkloadSignerProtocolError("workload signer peer identity is unavailable") from error
        if uid != self._allowed_peer_uid:
            raise WorkloadSignerProtocolError("workload signer peer UID is not authorized")
        raw = _read_message(connection, deadline=time.monotonic() + self._timeout)
        request = _document(raw)
        intent_id, lock, commit, evidence = _validate_request(request)
        try:
            target = self._policy.publish(lock, commit, evidence)
        except WorkloadSignerProtocolError:
            raise
        except (TypeError, ValueError, WorkloadTrustError) as error:
            raise WorkloadSignerProtocolError("workload publication was rejected") from error
        response = {
            "schema_version": 1,
            "intent_id": intent_id,
            "request_digest": hashlib.sha256(raw).hexdigest(),
            "target": _target_document(target),
        }
        encoded = _canonical(response)
        if len(encoded) > MAX_WORKLOAD_SIGNER_MESSAGE_BYTES:
            raise WorkloadSignerProtocolError("workload signer response is too large")
        try:
            connection.settimeout(self._timeout)
            connection.sendall(encoded)
        except (OSError, TimeoutError) as error:
            raise WorkloadSignerProtocolError("workload signer response failed") from error


class UnixWorkloadSignerClient:
    """Client implementing the ``PackagePublicationService`` publisher ABI."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 5.0) -> None:
        self._socket_path = Path(socket_path)
        if not self._socket_path.is_absolute():
            raise ValueError("workload signer socket path must be absolute")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("workload signer timeout is invalid")
        self._timeout = float(timeout_seconds)

    def publish(self, lock: bytes, commit: str, evidence: Mapping[str, object]) -> TrustedWorkloadTarget:
        if not isinstance(lock, bytes) or not 0 < len(lock) <= MAX_WORKLOAD_LOCK_BYTES:
            raise ValueError("workload release lock is invalid")
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise ValueError("workload Git commit is invalid")
        if not isinstance(evidence, Mapping):
            raise TypeError("workload evidence is invalid")
        request = {
            "schema_version": 1,
            "intent_id": str(uuid.uuid4()),
            "action": "workload.publish",
            "lock": base64.b64encode(lock).decode("ascii"),
            "git_commit": commit,
            "evidence": dict(evidence),
        }
        encoded = _canonical(request)
        if len(encoded) > MAX_WORKLOAD_SIGNER_MESSAGE_BYTES:
            raise ValueError("workload signer request is too large")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout)
        try:
            connection.connect(str(self._socket_path))
            connection.sendall(encoded)
            connection.shutdown(socket.SHUT_WR)
            response = _document(_read_message(connection))
        except WorkloadSignerProtocolError:
            raise
        except (OSError, TimeoutError) as error:
            raise WorkloadSignerProtocolError("workload signer service is unavailable") from error
        finally:
            connection.close()
        return self._decode_response(response, request, encoded)

    @staticmethod
    def _decode_response(response: Mapping[str, object], request: Mapping[str, object], encoded: bytes | None = None) -> TrustedWorkloadTarget:
        request_bytes = encoded or _canonical(request)
        if set(response) != {"schema_version", "intent_id", "request_digest", "target"}:
            raise WorkloadSignerProtocolError("workload signer response fields are invalid")
        if (
            response.get("schema_version") != 1
            or response.get("intent_id") != request.get("intent_id")
            or response.get("request_digest") != hashlib.sha256(request_bytes).hexdigest()
        ):
            raise WorkloadSignerProtocolError("workload signer response binding is invalid")
        target = _target_document(response.get("target"))
        return TrustedWorkloadTarget(
            digest=target["digest"],
            length=target["length"],
            git_commit=target["git_commit"],
            tuf_snapshot_version=target["tuf_snapshot_version"],
        )


def serve_forever(socket_path: Path, handler: WorkloadSignerConnectionHandler) -> None:
    path = Path(socket_path)
    if not path.is_absolute():
        raise ValueError("workload signer socket path must be absolute")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(info.st_mode):
            raise WorkloadSignerProtocolError("workload signer socket path is occupied")
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o660)
        listener.listen(16)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    handler.handle(connection)
                except WorkloadSignerProtocolError:
                    continue
    finally:
        listener.close()


def _load_signer(path: Path) -> CryptoSigner:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_size > 16 * 1024
            or stat.S_IMODE(metadata.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise WorkloadSignerProtocolError("workload signer key is unsafe")
        raw = bytearray()
        while len(raw) <= 16 * 1024:
            chunk = os.read(descriptor, min(4096, 16 * 1024 + 1 - len(raw)))
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
        if not raw or len(raw) > 16 * 1024 or identity(metadata) != identity(after):
            raise WorkloadSignerProtocolError("workload signer key is unsafe")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        private = serialization.load_pem_private_key(bytes(raw), password=None)
        if not isinstance(private, ed25519.Ed25519PrivateKey):
            raise WorkloadSignerProtocolError("workload signer key is not Ed25519")
        return CryptoSigner(private, SSlibKey.from_crypto(private.public_key()))
    except WorkloadSignerProtocolError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise WorkloadSignerProtocolError("workload signer key is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main() -> None:
    """Run the isolated NAS workload-TUF signer from deployment settings."""
    from cluster_profiles.workload_packages import PackageFamily

    from .repository import RepositoryService
    from .workload_trust import WorkloadTrustPublisher

    def absolute(name: str, default: str) -> Path:
        value = Path(os.environ.get(name, default))
        if not value.is_absolute():
            raise RuntimeError(f"{name} must be absolute")
        return value

    def required_path(name: str) -> Path:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"{name} is required")
        path = Path(value)
        if not path.is_absolute():
            raise RuntimeError(f"{name} must be absolute")
        return path

    metadata_root = absolute("VONK_WORKLOAD_TUF_METADATA_ROOT", "/workload-tuf/metadata")
    target_root = absolute("VONK_WORKLOAD_TUF_TARGET_ROOT", "/workload-tuf/targets")
    repository = RepositoryService(
        absolute("VONK_WORKLOAD_REPOSITORY_PATH", "/repository")
    )
    branch = os.environ.get("VONK_DEPLOYMENT_BRANCH", "deploy")
    if not branch or any(part in {"", ".", ".."} for part in branch.split("/")):
        raise RuntimeError("VONK_DEPLOYMENT_BRANCH is invalid")

    def commit_eligible(commit: str) -> bool:
        try:
            return repository.head(branch) == commit
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def policy_authorized(family_id: str, evidence: Mapping[str, object]) -> bool:
        try:
            commit = repository.head(branch)
            document = repository.read_document(
                commit, f"config/package-families/{family_id}.toml"
            )
            if not isinstance(document.parsed, Mapping):
                return False
            family = PackageFamily.load(document.parsed)
            if family.family_id != family_id:
                return False
            required = family.policy.get("required_evidence", ())
            if not isinstance(required, (list, tuple)):
                return False
            for kind in required:
                if not isinstance(kind, str) or not kind:
                    return False
                # The policy names evidence classes; the concrete digest
                # fields are supplied by the signed family definition. Keep
                # this boundary generic so a new runtime does not require a
                # Vonk Forge release.
                fields = {
                    key
                    for key in evidence
                    if key == kind
                    or key == f"{kind}_digest"
                    or key.startswith(f"{kind}.")
                }
                if not fields or not any(
                    isinstance(evidence[field], str) and evidence[field]
                    for field in fields
                ):
                    return False
            return True
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def evidence_verified(digest: str, evidence: Mapping[str, object]) -> bool:
        if evidence.get("lock_digest") != digest:
            return False
        return all(
            key in {"lock_digest", "schema_version"}
            or (isinstance(value, str) and _DIGEST.fullmatch(value) is not None)
            for key, value in evidence.items()
        )

    publisher = WorkloadTrustPublisher(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=WorkloadOnlineSigners(
            releases=_load_signer(required_path("VONK_WORKLOAD_RELEASES_KEY_FILE")),
            snapshot=_load_signer(required_path("VONK_WORKLOAD_SNAPSHOT_KEY_FILE")),
            timestamp=_load_signer(required_path("VONK_WORKLOAD_TIMESTAMP_KEY_FILE")),
        ),
        commit_eligible=commit_eligible,
        policy_authorized=policy_authorized,
        evidence_verified=evidence_verified,
        clock=lambda: datetime.now(UTC),
    )
    peer_uid_raw = os.environ.get("VONK_WORKLOAD_SIGNER_PEER_UID", "10001")
    try:
        peer_uid = int(peer_uid_raw)
    except ValueError as error:
        raise RuntimeError("VONK_WORKLOAD_SIGNER_PEER_UID is invalid") from error
    serve_forever(
        absolute("VONK_WORKLOAD_SIGNER_SOCKET", "/run/vonk-workload-signer/signer.sock"),
        WorkloadSignerConnectionHandler(
            WorkloadPublicationSignerPolicy(publisher),
            allowed_peer_uid=peer_uid,
        ),
    )


__all__ = [
    "UnixWorkloadSignerClient",
    "WorkloadPublicationSignerPolicy",
    "WorkloadSignerConnectionHandler",
    "WorkloadSignerProtocolError",
    "serve_forever",
]
