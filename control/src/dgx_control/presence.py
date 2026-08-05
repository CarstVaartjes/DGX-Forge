"""Authenticated agent presence and bounded management-address policy."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import AgentNode, Observation

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_MANAGEMENT_ADDRESS_KIND = "management-address"
type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network
type _Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class PresenceError(ValueError):
    """Presence input is invalid or unavailable."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
            raise PresenceError(f"{label} must contain canonical CIDR networks") from error
        if str(network) != raw:
            raise PresenceError(f"{label} must contain canonical CIDR networks")
        if network in networks:
            raise PresenceError(f"{label} cannot contain duplicate networks")
        networks.append(network)
    return tuple(networks)


@dataclass(frozen=True)
class ManagementAddressPolicy:
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
            raise PresenceError("management address must be a canonical IP literal") from error
        if str(address) != value:
            raise PresenceError("management address must be a canonical IP literal")
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise PresenceError("management address belongs to a prohibited address class")
        matching = [
            network
            for network in self.allowed_networks
            if address.version == network.version and address in network
        ]
        if not matching:
            raise PresenceError("management address is outside configured management CIDRs")
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
            raise PresenceError("management address cannot be a network or broadcast address")
        return address.compressed


@dataclass(frozen=True)
class ManagementAddressObservation:
    node_id: str
    address: str
    observed_at: datetime


class AgentPresenceService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        policy: ManagementAddressPolicy,
    ) -> None:
        self._sessions = sessions
        self.policy = policy

    def observe(
        self,
        node_id: str,
        address: str,
        observed_at: datetime,
    ) -> ManagementAddressObservation:
        if _NODE_ID.fullmatch(node_id) is None:
            raise PresenceError("node ID is invalid")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise PresenceError("observation timestamp must include a timezone")
        canonical_address = self.policy.validate(address)
        timestamp = _utc(observed_at)
        with self._sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            if node is None or node.state != "active" or node.revoked_at is not None:
                raise PresenceError("agent node is not active")
            node.last_seen_at = timestamp
            session.add(
                Observation(
                    node_id=node_id,
                    kind=_MANAGEMENT_ADDRESS_KIND,
                    payload={"address": canonical_address},
                    observed_at=timestamp,
                )
            )
        return ManagementAddressObservation(node_id, canonical_address, timestamp)

    def latest(
        self,
        node_id: str,
        *,
        maximum_age_seconds: int,
        now: datetime,
    ) -> ManagementAddressObservation:
        if _NODE_ID.fullmatch(node_id) is None:
            raise PresenceError("node ID is invalid")
        if maximum_age_seconds <= 0:
            raise PresenceError("maximum age must be positive")
        if now.tzinfo is None or now.utcoffset() is None:
            raise PresenceError("current timestamp must include a timezone")
        with self._sessions() as session:
            node = session.get(AgentNode, node_id)
            if node is None or node.state != "active" or node.revoked_at is not None:
                raise PresenceError("agent node is not active")
            observation = session.scalar(
                select(Observation)
                .where(
                    Observation.node_id == node_id,
                    Observation.kind == _MANAGEMENT_ADDRESS_KIND,
                )
                .order_by(Observation.observed_at.desc())
                .limit(1)
            )
        if observation is None:
            raise PresenceError("management address observation is unavailable")
        timestamp = _utc(observation.observed_at)
        current = _utc(now)
        if timestamp > current:
            raise PresenceError("management address observation is in the future")
        if current - timestamp > timedelta(seconds=maximum_age_seconds):
            raise PresenceError("management address observation is stale")
        raw_address = observation.payload.get("address")
        if not isinstance(raw_address, str):
            raise PresenceError("management address observation is invalid")
        address = self.policy.validate(raw_address)
        return ManagementAddressObservation(node_id, address, timestamp)
