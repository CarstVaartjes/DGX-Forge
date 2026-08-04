"""Read-only projection joining Git authority with operational observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import AgentNode, Observation, Reconciliation


class DashboardService:
    def __init__(
        self,
        repository,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        agent_online_window_seconds: int = 150,
    ) -> None:
        if agent_online_window_seconds <= 0:
            raise ValueError("agent online window must be positive")
        self._repository = repository
        self._sessions = sessions
        self._clock = clock
        self._agent_online_window_seconds = agent_online_window_seconds

    def fleet(self) -> dict[str, object]:
        commit = self._repository.head()
        document = self._repository.read_document(commit, "inventory/fleet.toml")
        parsed = document.parsed
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("nodes"), Mapping):
            raise ValueError("fleet document does not contain a node table")
        with self._sessions() as session:
            observations = list(session.scalars(select(Observation).where(Observation.kind == "health").order_by(Observation.observed_at.desc())))
            reconciliations = list(session.scalars(select(Reconciliation).where(Reconciliation.status == "succeeded").order_by(Reconciliation.created_at.desc()).limit(1)))
            agent_nodes = {
                node.node_id: node
                for node in session.scalars(select(AgentNode))
            }
        latest = {}
        for observation in observations:
            latest.setdefault(observation.node_id, (observation.payload, observation.observed_at))
        active_profiles = {}
        if reconciliations and isinstance(reconciliations[0].summary, Mapping):
            raw = reconciliations[0].summary.get("node_profiles", {})
            if isinstance(raw, Mapping): active_profiles = raw
        nodes = []
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("dashboard clock must be timezone-aware")
        current = current.astimezone(UTC)
        for node_id, raw in sorted(parsed["nodes"].items()):
            if not isinstance(node_id, str) or not isinstance(raw, Mapping):
                continue
            health, observed_at = latest.get(node_id, ({}, None))
            if observed_at is not None and observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            agent_node = agent_nodes.get(node_id)
            agent_last_seen_at = agent_node.last_seen_at if agent_node is not None else None
            if agent_last_seen_at is not None:
                agent_last_seen_at = (
                    agent_last_seen_at.replace(tzinfo=UTC)
                    if agent_last_seen_at.tzinfo is None
                    else agent_last_seen_at.astimezone(UTC)
                )
            agent_age = (
                (current - agent_last_seen_at).total_seconds()
                if agent_last_seen_at is not None
                else None
            )
            agent_online = (
                agent_node is not None
                and agent_node.state == "active"
                and agent_node.revoked_at is None
                and agent_age is not None
                and 0 <= agent_age <= self._agent_online_window_seconds
            )
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
                "probe_age_seconds": max(0.0, (datetime.now(UTC) - observed_at).total_seconds()) if observed_at is not None else 0.0,
                "agent_state": agent_node.state if agent_node is not None else "unregistered",
                "agent_last_seen_at": agent_last_seen_at.isoformat() if agent_last_seen_at is not None else None,
                "agent_online": agent_online,
            })
        return {"commit": commit, "nodes": nodes}
