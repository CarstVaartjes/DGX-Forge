"""Explicit projection of accepted two-node profiles into V2 requirements."""

from __future__ import annotations

import hashlib
from types import MappingProxyType

from .contracts import ClusterProfile, GenericClusterProfile, LifecycleConstraints
from .fleet import Fleet
from .placement import PlacementRequirement


def adapt_legacy_profile(profile: ClusterProfile, fleet: Fleet) -> GenericClusterProfile:
    active = [
        node_id for node_id, record in sorted(fleet.nodes.items())
        if record.lifecycle != "retired"
    ]
    if len(active) < 2:
        raise ValueError("legacy two-Spark profiles require at least two active fleet nodes")
    aliases = {"spark1": active[0], "spark2": active[1]}
    if set(profile.placements) != set(aliases):
        raise ValueError("legacy profile must contain exact spark1/spark2 placements")
    assigned: dict[str, list[str]] = {}
    per_alias_count = {alias: len(profile.placements[alias]) for alias in aliases}
    for alias in ("spark1", "spark2"):
        for workload in profile.placements[alias]:
            assigned.setdefault(workload, []).append(aliases[alias].value)
    requirements = []
    for workload in sorted(assigned):
        preferred = tuple(assigned[workload])
        distributed = len(preferred) > 1
        exclusive = all(per_alias_count[alias] == 1 for alias in aliases if aliases[alias].value in preferred)
        requirements.append(PlacementRequirement(
            name=workload,
            definition_hash=hashlib.sha256(f"legacy:{workload}".encode()).hexdigest(),
            node_count=len(preferred), required_labels={}, min_memory_bytes=0,
            min_disk_bytes=0, exclusive=exclusive, distributed=distributed,
            model_supports_distributed=distributed, preferred_node_ids=preferred,
        ))
    has_distributed = any(item.distributed for item in requirements)
    lifecycle = LifecycleConstraints(
        "workers-before-entrypoint" if has_distributed else "independent",
        "entrypoint-before-workers" if has_distributed else "independent",
    )
    return GenericClusterProfile(
        profile.id, profile.accepted_evidence, tuple(sorted(assigned)),
        tuple(requirements), MappingProxyType(dict(profile.endpoints)), lifecycle,
    )
