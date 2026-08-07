"""Issue bounded API-side grants for the isolated update signer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

MAX_GRANT_BYTES = 64 * 1024
MAX_GRANT_NODES = 1024
MAX_GRANT_SECONDS = 3600
MAX_PRIVATE_KEY_BYTES = 16 * 1024

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_RELEASE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AdminActionGrantError(RuntimeError):
    """An admin grant input or its private signing key is unsafe."""


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
        raise AdminActionGrantError(
            "admin action grant is not canonical JSON"
        ) from error


def _private_key_snapshot(path: Path) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size > MAX_PRIVATE_KEY_BYTES
            or mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise AdminActionGrantError("admin grant private key is unsafe")
        content = bytearray()
        while len(content) <= MAX_PRIVATE_KEY_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, MAX_PRIVATE_KEY_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if len(content) > MAX_PRIVATE_KEY_BYTES or identity(before) != identity(after):
            raise AdminActionGrantError(
                "admin grant private key changed while being read"
            )
        return bytes(content)
    except AdminActionGrantError:
        raise
    except OSError as error:
        raise AdminActionGrantError("admin grant private key is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _uuid4(value: object, name: str, *, require_string: bool = True) -> str:
    if require_string and not isinstance(value, str):
        raise AdminActionGrantError(f"admin grant {name} is invalid")
    rendered = str(value)
    try:
        parsed = uuid.UUID(rendered)
    except (AttributeError, TypeError, ValueError) as error:
        raise AdminActionGrantError(f"admin grant {name} is invalid") from error
    if parsed.version != 4 or str(parsed) != rendered:
        raise AdminActionGrantError(f"admin grant {name} is invalid")
    return rendered


class AdminActionGrantIssuer:
    """Sign one short-lived grant over an exact rollout and node set."""

    def __init__(
        self,
        private_key: ed25519.Ed25519PrivateKey,
        *,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], object] | None = None,
    ) -> None:
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError("admin grant private key must be Ed25519")
        if clock is not None and not callable(clock):
            raise TypeError("admin grant clock is invalid")
        if nonce_factory is not None and not callable(nonce_factory):
            raise TypeError("admin grant nonce factory is invalid")
        self._private_key = private_key
        self._clock = clock or (lambda: datetime.now(UTC))
        self._nonce_factory = nonce_factory or uuid.uuid4
        self._public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.key_id = hashlib.sha256(self._public_key).hexdigest()

    @classmethod
    def from_private_key_file(
        cls,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], object] | None = None,
    ) -> AdminActionGrantIssuer:
        raw = _private_key_snapshot(Path(path))
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError) as error:
            raise AdminActionGrantError("admin grant private key is invalid") from error
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise AdminActionGrantError("admin grant private key must be Ed25519")
        return cls(key, clock=clock, nonce_factory=nonce_factory)

    def public_key_document(self) -> dict[str, object]:
        return {
            "algorithm": "ed25519",
            "key_id": self.key_id,
            "public_key": self._public_key.hex(),
            "schema_version": 1,
        }

    def public_key_bytes(self) -> bytes:
        return _canonical(self.public_key_document())

    def issue(
        self,
        *,
        action: object,
        rollout_id: object,
        parent_job_id: object,
        node_ids: object,
        target_release_digest: object,
        expires_at: object,
    ) -> dict[str, object]:
        if not isinstance(action, str) or action not in {
            "agent.update",
            "agent.rollback",
        }:
            raise AdminActionGrantError("admin grant action is invalid")
        rollout = _uuid4(rollout_id, "rollout ID")
        parent = _uuid4(parent_job_id, "parent job ID")
        nodes = self._nodes(node_ids)
        target = self._target(action, target_release_digest)
        expiry = self._expiry(expires_at)
        try:
            nonce_value = self._nonce_factory()
        except Exception as error:
            raise AdminActionGrantError("admin grant nonce is unavailable") from error
        nonce = _uuid4(nonce_value, "nonce", require_string=False)
        claims = {
            "action": action,
            "expires_at": expiry,
            "nonce": nonce,
            "node_ids": nodes,
            "parent_job_id": parent,
            "rollout_id": rollout,
            "schema_version": 1,
            "target_release_digest": target,
        }
        encoded = _canonical(claims)
        if len(encoded) > MAX_GRANT_BYTES:
            raise AdminActionGrantError("admin action grant is too large")
        envelope = {
            "claims": claims,
            "signature": {
                "algorithm": "ed25519",
                "key_id": self.key_id,
                "value": self._private_key.sign(encoded).hex(),
            },
        }
        if len(_canonical(envelope)) > MAX_GRANT_BYTES:
            raise AdminActionGrantError("admin action grant is too large")
        return envelope

    @staticmethod
    def _nodes(value: object) -> list[str]:
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray))
            or not 1 <= len(value) <= MAX_GRANT_NODES
            or any(
                not isinstance(node, str) or _NODE_ID.fullmatch(node) is None
                for node in value
            )
            or len(set(value)) != len(value)
        ):
            raise AdminActionGrantError("admin grant node IDs are invalid")
        return sorted(value)

    @staticmethod
    def _target(action: str, value: object) -> str | None:
        if action == "agent.update":
            if not isinstance(value, str) or _RELEASE_DIGEST.fullmatch(value) is None:
                raise AdminActionGrantError("admin grant update target is invalid")
            return value
        if value is not None:
            raise AdminActionGrantError("admin grant rollback target must be null")
        return None

    def _expiry(self, value: object) -> int:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise AdminActionGrantError("admin grant clock must be timezone-aware")
        now_epoch = int(now.astimezone(UTC).timestamp())
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= now_epoch
            or value > now_epoch + MAX_GRANT_SECONDS
        ):
            raise AdminActionGrantError("admin grant expiry is invalid")
        return value
