"""Read-only projection joining Git authority with operational observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .metrics import protocol_version_bucket
from .models import AgentCertificate, AgentNode, Observation, Reconciliation


class DashboardService:
    def __init__(
        self,
        repository,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        protocol_minimum: int = 1,
        protocol_maximum: int = 1,
    ) -> None:
        if protocol_minimum < 1 or protocol_maximum < protocol_minimum:
            raise ValueError("supported protocol range is invalid")
        self._repository = repository
        self._sessions = sessions
        self._clock = clock
        self._protocol_minimum = protocol_minimum
        self._protocol_maximum = protocol_maximum

    def fleet(self) -> dict[str, object]:
        commit = self._repository.head()
        document = self._repository.read_document(commit, "inventory/fleet.toml")
        parsed = document.parsed
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("nodes"), Mapping):
            raise TypeError("fleet document does not contain a node table")
        with self._sessions() as session:
            observations = list(session.scalars(select(Observation).where(Observation.kind == "health").order_by(Observation.observed_at.desc())))
            reconciliations = list(session.scalars(select(Reconciliation).where(Reconciliation.status == "succeeded").order_by(Reconciliation.created_at.desc()).limit(1)))
            agent_nodes = {
                node.node_id: node
                for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id))
            }
            certificates = list(
                session.scalars(
                    select(AgentCertificate)
                    .where(
                        AgentCertificate.state == "active",
                        AgentCertificate.revoked_at.is_(None),
                    )
                    .order_by(
                        AgentCertificate.node_id,
                        AgentCertificate.not_after.desc(),
                        AgentCertificate.generation.desc(),
                    )
                )
            )
        active_certificates = {}
        for certificate in certificates:
            active_certificates.setdefault(certificate.node_id, certificate)
        latest = {}
        for observation in observations:
            latest.setdefault(observation.node_id, (observation.payload, observation.observed_at))
        active_profiles = {}
        if reconciliations and isinstance(reconciliations[0].summary, Mapping):
            raw = reconciliations[0].summary.get("node_profiles", {})
            if isinstance(raw, Mapping): active_profiles = raw
        nodes = []
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        for node_id, raw in sorted(parsed["nodes"].items()):
            if not isinstance(node_id, str) or not isinstance(raw, Mapping):
                continue
            health, observed_at = latest.get(node_id, ({}, None))
            if observed_at is not None and observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            agent = agent_nodes.get(node_id)
            last_seen_at = None if agent is None else agent.last_seen_at
            if last_seen_at is not None and last_seen_at.tzinfo is None:
                last_seen_at = last_seen_at.replace(tzinfo=UTC)
            certificate = active_certificates.get(node_id)
            certificate_expires_at = None if certificate is None else certificate.not_after
            if certificate_expires_at is not None and certificate_expires_at.tzinfo is None:
                certificate_expires_at = certificate_expires_at.replace(tzinfo=UTC)
            nodes.append({
                "id": node_id,
                "display_name": str(raw.get("display_name", node_id)),
                "hostname": str(raw.get("hostname", "")),
                "lifecycle": str(raw.get("lifecycle", "unknown")),
                "healthy": health.get("status") in {"healthy", "warning"} if isinstance(health, Mapping) else None,
                "labels": dict(raw.get("labels", {})) if isinstance(raw.get("labels"), Mapping) else {},
                "profile": active_profiles.get(node_id),
                "memory_available_bytes": health.get("memory_available_bytes", 0) if isinstance(health, Mapping) else 0,
                "disk_available_bytes": health.get("disk_available_bytes", 0) if isinstance(health, Mapping) else 0,
                "probe_age_seconds": max(0.0, (now - observed_at).total_seconds()) if observed_at is not None else 0.0,
                "agent_state": None if agent is None else agent.state,
                "last_seen_at": None if last_seen_at is None else last_seen_at.isoformat(),
                "last_seen_age_seconds": None if last_seen_at is None else max(0.0, (now - last_seen_at).total_seconds()),
                "certificate_expires_at": None if certificate_expires_at is None else certificate_expires_at.isoformat(),
                "certificate_expiry_seconds": None if certificate_expires_at is None else max(0.0, (certificate_expires_at - now).total_seconds()),
                "compatibility": protocol_version_bucket(
                    None if agent is None else agent.protocol_version,
                    minimum=self._protocol_minimum,
                    maximum=self._protocol_maximum,
                ),
            })
        return {"commit": commit, "nodes": nodes}
