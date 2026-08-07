from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_agent_protocol import (
    HostHelperOperation,
    HostOperationKind,
    host_helper_grant_signing_bytes,
)
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostHelperGrantIssuer,
)

NOW = datetime(2036, 7, 1, 12, 0, tzinfo=UTC)
REQUEST_ID = "10000000-0000-4000-8000-000000000001"


def issuer() -> HostHelperGrantIssuer:
    return HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: NOW,
        request_id_factory=lambda: REQUEST_ID,
    )


def test_controller_issues_exact_short_lived_host_grant() -> None:
    authority = issuer()
    grant = authority.issue_grant(
        node_id="spk_" + "1" * 32,
        operation=HostHelperOperation(
            HostOperationKind.RESTART_VONK_UNIT, {"unit": "agent"}
        ),
        expires_in_seconds=90,
    )

    assert grant.claims.request_id == REQUEST_ID
    assert grant.claims.expires_at - grant.claims.issued_at == 90
    authority.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    assert authority.public_key_document()["usage"] == "host-maintenance-grant"


@pytest.mark.parametrize("seconds", (0, 301, True))
def test_controller_refuses_unbounded_host_grants(seconds: object) -> None:
    with pytest.raises(HostHelperAuthorityError, match="expiry"):
        issuer().issue_grant(
            node_id="spk_" + "1" * 32,
            operation=HostHelperOperation(
                HostOperationKind.SCHEDULE_REBOOT, {"delay_seconds": 120}
            ),
            expires_in_seconds=seconds,
        )


def test_controller_refuses_mapping_shaped_or_untyped_operations() -> None:
    with pytest.raises(HostHelperAuthorityError, match="operation"):
        issuer().issue_grant(
            node_id="spk_" + "1" * 32,
            operation={"type": "restart-vonk-unit", "unit": "agent"},
            expires_in_seconds=30,
        )
