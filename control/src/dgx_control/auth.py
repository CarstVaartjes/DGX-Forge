"""Small signed-token authentication core for CLI and browser sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

_ROLES = frozenset({"viewer", "operator", "administrator"})


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    subject: str
    role: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or self.role not in _ROLES:
            raise AuthError("invalid authenticated actor")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class TokenCodec:
    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("token signing key must be at least 32 bytes")
        self._key = signing_key

    def issue(self, actor: Actor, *, ttl_seconds: int, now: int) -> str:
        if ttl_seconds <= 0:
            raise ValueError("token lifetime must be positive")
        payload = json.dumps(
            {"sub": actor.subject, "role": actor.role, "exp": now + ttl_seconds},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        body = _encode(payload)
        signature = _encode(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify(self, token: str, *, now: int) -> Actor:
        try:
            body, signature = token.split(".", 1)
            expected = hmac.new(self._key, body.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_decode(signature), expected):
                raise AuthError("token signature is invalid")
            payload = json.loads(_decode(body))
            if not isinstance(payload, dict):
                raise AuthError("token payload is invalid")
            if not isinstance(payload.get("exp"), int) or payload["exp"] < now:
                raise AuthError("token is expired")
            return Actor(str(payload["sub"]), str(payload["role"]))
        except AuthError:
            raise
        except Exception as error:
            raise AuthError("token is malformed") from error
