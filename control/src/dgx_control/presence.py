"""Bounded management-address policy for authenticated agent evidence."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network
type _Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class PresenceError(ValueError):
    """A management address or its configured policy is invalid."""


def _parse_networks(value: str, *, label: str, required: bool) -> tuple[_Network, ...]:
    if not value.strip():
        if required:
            raise PresenceError(f"{label} cannot be empty")
        return ()
    raw_networks = [item.strip() for item in value.split(",")]
    if any(not item for item in raw_networks):
        raise PresenceError(f"{label} cannot contain an empty network")
    networks: list[_Network] = []
    for raw in raw_networks:
        try:
            network = ipaddress.ip_network(raw, strict=True)
        except ValueError as error:
            raise PresenceError(
                f"{label} must contain canonical CIDR networks"
            ) from error
        if str(network) != raw:
            raise PresenceError(f"{label} must contain canonical CIDR networks")
        if network in networks:
            raise PresenceError(f"{label} cannot contain duplicate networks")
        networks.append(network)
    return tuple(networks)


@dataclass(frozen=True)
class ManagementAddressPolicy:
    """Allow canonical IP literals from explicit management networks only."""

    allowed_networks: tuple[_Network, ...]
    forbidden_networks: tuple[_Network, ...] = ()

    @classmethod
    def parse(
        cls,
        value: str,
        *,
        forbidden_cidrs: str = "",
    ) -> ManagementAddressPolicy:
        allowed = _parse_networks(value, label="management CIDRs", required=True)
        forbidden = _parse_networks(
            forbidden_cidrs,
            label="forbidden CIDRs",
            required=False,
        )
        for network in allowed:
            if any(
                network.version == blocked.version and network.subnet_of(blocked)
                for blocked in forbidden
            ):
                raise PresenceError(f"management CIDR {network} is fully forbidden")
        return cls(allowed, forbidden)

    def validate(self, value: str) -> str:
        try:
            address: _Address = ipaddress.ip_address(value)
        except ValueError as error:
            raise PresenceError(
                "management address must be a canonical IP literal"
            ) from error
        if str(address) != value:
            raise PresenceError("management address must be a canonical IP literal")
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise PresenceError(
                "management address belongs to a prohibited address class"
            )
        matching = tuple(
            network
            for network in self.allowed_networks
            if address.version == network.version and address in network
        )
        if not matching:
            raise PresenceError(
                "management address is outside configured management CIDRs"
            )
        if any(
            address.version == network.version and address in network
            for network in self.forbidden_networks
        ):
            raise PresenceError("management address belongs to a forbidden CIDR")
        if any(
            address == network.network_address
            or (
                isinstance(network, ipaddress.IPv4Network)
                and address == network.broadcast_address
            )
            for network in matching
        ):
            raise PresenceError(
                "management address cannot be a network or broadcast address"
            )
        return address.compressed
