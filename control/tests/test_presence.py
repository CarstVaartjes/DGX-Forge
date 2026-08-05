from __future__ import annotations

import pytest
from dgx_control.presence import ManagementAddressPolicy, PresenceError


def test_management_address_policy_accepts_only_canonical_bounded_addresses() -> None:
    policy = ManagementAddressPolicy.parse(
        "10.0.0.0/24,2001:db8:42::/64",
        forbidden_cidrs="10.0.0.240/28",
    )

    assert policy.validate("10.0.0.42") == "10.0.0.42"
    assert policy.validate("2001:db8:42::2") == "2001:db8:42::2"

    for address in (
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "10.0.0.241",
        "10.0.1.1",
        "10.0.0.0",
        "10.0.0.255",
    ):
        with pytest.raises(PresenceError):
            policy.validate(address)


def test_management_address_policy_rejects_ambiguous_network_policy() -> None:
    for allowed, forbidden, error in (
        ("10.0.0.1/24", "", "canonical"),
        ("10.0.0.0/24,10.0.0.0/24", "", "duplicate"),
        ("", "", "empty"),
        ("10.0.0.0/24", "10.0.0.0/24", "fully forbidden"),
    ):
        with pytest.raises(PresenceError, match=error):
            ManagementAddressPolicy.parse(allowed, forbidden_cidrs=forbidden)
