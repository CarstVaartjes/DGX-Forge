"""Controller-only signer for the narrow Spark host-maintenance helper."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ed25519
from dgx_agent_protocol import AgentProtocolError
from dgx_agent_protocol.host_helper import (
    HOST_HELPER_AUTHORITY,
    MAX_HOST_HELPER_GRANT_SECONDS,
    HostHelperGrantClaims,
    HostHelperOperation,
    HostHelperSignature,
    SignedHostHelperGrant,
    host_helper_grant_signing_bytes,
)

from .package_helper_authority import _load_private_key


class HostHelperAuthorityError(RuntimeError):
    """The host-helper grant could not be issued safely."""


class HostHelperGrantIssuer:
    """Sign one short-lived, exact host operation for one Spark node."""

    def __init__(
        self,
        private_key: ed25519.Ed25519PrivateKey,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], object] | None = None,
    ) -> None:
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError("host helper authority key must be Ed25519")
        if clock is not None and not callable(clock):
            raise TypeError("host helper authority clock is invalid")
        if request_id_factory is not None and not callable(request_id_factory):
            raise TypeError("host helper request ID factory is invalid")
        self._private_key = private_key
        self._clock = clock or (lambda: datetime.now(UTC))
        self._request_id_factory = request_id_factory or uuid4
        self.public_key = private_key.public_key()
        self.public_key_bytes = self.public_key.public_bytes_raw()
        self.key_id = hashlib.sha256(self.public_key_bytes).hexdigest()

    @classmethod
    def from_private_key_file(
        cls,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], object] | None = None,
    ) -> HostHelperGrantIssuer:
        return cls(
            _load_private_key(Path(path)),
            clock=clock,
            request_id_factory=request_id_factory,
        )

    def public_key_document(self) -> dict[str, object]:
        return {
            "algorithm": "ed25519",
            "authority": HOST_HELPER_AUTHORITY,
            "key_id": self.key_id,
            "public_key": self.public_key_bytes.hex(),
            "schema_version": 1,
            "usage": "host-maintenance-grant",
        }

    def issue_grant(
        self,
        *,
        node_id: object,
        operation: object,
        expires_in_seconds: object,
        request_id: object | None = None,
    ) -> SignedHostHelperGrant:
        if type(operation) is not HostHelperOperation:
            raise HostHelperAuthorityError("host helper operation is invalid")
        if (
            not isinstance(expires_in_seconds, int)
            or isinstance(expires_in_seconds, bool)
            or not 1 <= expires_in_seconds <= MAX_HOST_HELPER_GRANT_SECONDS
        ):
            raise HostHelperAuthorityError("host helper grant expiry is invalid")
        now = self._now()
        try:
            claims = HostHelperGrantClaims(
                schema_version=1,
                authority=HOST_HELPER_AUTHORITY,
                request_id=str(
                    self._request_id_factory() if request_id is None else request_id
                ),
                node_id=node_id,
                issued_at=now,
                expires_at=now + expires_in_seconds,
                operation=operation,
            )
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise HostHelperAuthorityError(
                "host helper grant binding is invalid"
            ) from error
        return SignedHostHelperGrant(
            schema_version=1,
            claims=claims,
            signature=HostHelperSignature(
                algorithm="ed25519",
                key_id=self.key_id,
                value=self._private_key.sign(
                    host_helper_grant_signing_bytes(claims)
                ).hex(),
            ),
        )

    def _now(self) -> int:
        try:
            now = self._clock()
        except Exception as error:
            raise HostHelperAuthorityError(
                "host helper authority clock is unavailable"
            ) from error
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise HostHelperAuthorityError(
                "host helper authority clock must be timezone-aware"
            )
        return int(now.astimezone(UTC).timestamp())
