"""Small signed-token authentication core for CLI and browser sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from starlette.responses import Response

_ROLES = frozenset({"viewer", "operator", "administrator"})
_AGENT_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_AGENT_IDENTITY_SCOPE_KEY = "dgx.agent_identity"
_AGENT_SOURCE_SCOPE_KEY = "dgx.agent_source"

MUTATION_ROLES = {
    ("POST", "/api/v1/jobs"): frozenset({"operator", "administrator"}),
    ("POST", "/api/v1/proposals"): frozenset({"operator", "administrator"}),
    ("POST", "/api/v1/changes"): frozenset({"administrator"}),
    ("POST", "/api/v1/reconciliations/plan"): frozenset({"operator", "administrator"}),
    ("POST", "/api/v1/reconciliations"): frozenset({"operator", "administrator"}),
    ("POST", "/api/v1/agents/enrollments/grants"): frozenset({"administrator"}),
    ("POST", "/api/v1/agents/enrollments/{enrollment_id}/approve"): frozenset({"administrator"}),
    ("POST", "/api/v1/agents/enrollments/{enrollment_id}/reject"): frozenset({"administrator"}),
    ("POST", "/api/v1/agents/nodes/{node_id}/revoke"): frozenset({"administrator"}),
}


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    subject: str
    role: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or self.role not in _ROLES:
            raise AuthError("invalid authenticated actor")


@dataclass(frozen=True)
class AgentIdentity:
    """An identity attested by the private TLS-terminating proxy."""

    node_id: str
    certificate_serial: str
    certificate_fingerprint: str
    verified: bool

    def __post_init__(self) -> None:
        if (
            _AGENT_NODE_ID.fullmatch(self.node_id) is None
            or not self.certificate_serial.strip()
            or not self.certificate_fingerprint.strip()
            or self.verified is not True
        ):
            raise AuthError("invalid verified agent identity")


def agent_identity_from_scope(scope: dict[str, Any]) -> AgentIdentity | None:
    """Return only a typed, verification-marked proxy identity from a scope."""
    identity = scope.get(_AGENT_IDENTITY_SCOPE_KEY)
    if not isinstance(identity, AgentIdentity) or identity.verified is not True:
        return None
    return identity


def agent_source_from_scope(scope: dict[str, Any]) -> str | None:
    """Return the proxy-observed peer address from an authenticated scope."""
    if agent_identity_from_scope(scope) is None:
        return None
    source = scope.get(_AGENT_SOURCE_SCOPE_KEY)
    return source if isinstance(source, str) and source else None


class TrustedProxyAgentIdentityMiddleware:
    """Convert forwarded mTLS metadata from configured private peers only.

    It deliberately removes every incoming ``X-DGX-Agent-*`` header before
    invoking the application.  Consequently downstream code can only consume
    the typed ASGI scope value, never a client supplied header.
    """

    def __init__(
        self,
        app: Any,
        *,
        trusted_proxy_auth: bytes = b"",
        agent_identity_validator: Callable[[AgentIdentity], bool] | None = None,
        activation_identity_validator: Callable[[AgentIdentity], bool] | None = None,
    ) -> None:
        self.app = app
        self._trusted_proxy_auth = trusted_proxy_auth
        self._agent_identity_validator = agent_identity_validator
        self._activation_identity_validator = activation_identity_validator

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers", ())
        forwarded: dict[str, str] = {}
        duplicate_forwarded_headers = False
        for key, value in raw_headers:
            if not key.lower().startswith(b"x-dgx-agent-"):
                continue
            name = key.decode("latin-1").lower()
            if name in forwarded:
                duplicate_forwarded_headers = True
            forwarded[name] = value.decode("latin-1")
        sanitized = tuple(
            (key, value) for key, value in raw_headers
            if not key.lower().startswith(b"x-dgx-agent-")
        )
        safe_scope = dict(scope)
        safe_scope.pop(_AGENT_IDENTITY_SCOPE_KEY, None)
        safe_scope.pop(_AGENT_SOURCE_SCOPE_KEY, None)
        safe_scope["headers"] = sanitized
        supplied_proxy_auth = forwarded.get("x-dgx-agent-proxy-auth", "").encode()
        if self._trusted_proxy_auth and hmac.compare_digest(supplied_proxy_auth, self._trusted_proxy_auth) and not duplicate_forwarded_headers:
            try:
                safe_scope[_AGENT_IDENTITY_SCOPE_KEY] = AgentIdentity(
                    node_id=forwarded["x-dgx-agent-node"],
                    certificate_serial=forwarded["x-dgx-agent-serial"],
                    certificate_fingerprint=forwarded["x-dgx-agent-fingerprint"],
                    verified=forwarded["x-dgx-agent-verified"] == "1",
                )
                source = forwarded.get("x-dgx-agent-source")
                if source:
                    safe_scope[_AGENT_SOURCE_SCOPE_KEY] = source
            except (AuthError, KeyError):
                pass
        path = safe_scope.get("path")
        validator = (
            self._activation_identity_validator
            if path == "/agent/v1/renew/activate"
            else self._agent_identity_validator
        )
        if (
            isinstance(path, str)
            and path.startswith("/agent/v1/")
            and path != "/agent/v1/enroll"
            and (
                agent_identity_from_scope(safe_scope) is None
                or validator is None
                or not validator(agent_identity_from_scope(safe_scope))
            )
        ):
            await Response(status_code=401)(safe_scope, receive, send)
            return
        await self.app(safe_scope, receive, send)


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
