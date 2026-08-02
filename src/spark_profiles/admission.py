"""Fail-closed admission checks for whole-cluster workload profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import Catalog, fingerprint
from .contracts import ClusterProfile, WorkloadDefinition

_VALID_PLACEMENT_DECLARATIONS = frozenset(
    {
        ("distributed", "dual-exclusive", "exclusive"),
        ("distributed", "dual-pipeline-experimental", "exclusive"),
        ("single", "single-exclusive", "exclusive"),
        ("single", "single-shareable", "accepted"),
    }
)


@dataclass(frozen=True)
class AdmissionReport:
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _value(measurement: Any, name: str) -> int | None:
    if not isinstance(measurement, Mapping):
        return None
    direct = measurement.get(name)
    if isinstance(direct, int) and not isinstance(direct, bool):
        return direct
    memory = measurement.get("memory")
    if name == "free_memory_bytes" and isinstance(memory, Mapping):
        value = memory.get("available_bytes")
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    return None


def _healthy(measurement: Any) -> bool:
    return isinstance(measurement, Mapping) and measurement.get("healthy") is True


def _accepted_for_profile(
    profile: ClusterProfile,
    definitions: Mapping[str, WorkloadDefinition],
    catalog: Catalog,
    accepted: Mapping[str, tuple[str, ...]] | None,
) -> bool:
    hashes = sorted(
        catalog.definition_fingerprints[identifier] for identifier in definitions
    )
    index = catalog.accepted_profiles if accepted is None else accepted
    return tuple(hashes) == tuple(index.get(fingerprint(profile), ()))


def _placement_declaration_is_consistent(definition: WorkloadDefinition) -> bool:
    return (
        definition.topology,
        definition.placement_class,
        definition.co_location,
    ) in _VALID_PLACEMENT_DECLARATIONS


def _can_co_locate(definition: WorkloadDefinition) -> bool:
    return (
        definition.topology == "single"
        and definition.placement_class == "single-shareable"
        and definition.co_location == "accepted"
    )


def _placement_policy(
    profile: ClusterProfile, catalog: Catalog
) -> tuple[
    AdmissionReport,
    dict[str, set[str]],
    dict[str, list[WorkloadDefinition]],
]:
    errors: set[str] = set()
    placements = profile.placements
    known_nodes = {"spark1", "spark2"}
    if set(placements) != known_nodes:
        errors.add("profile must specify placements for spark1 and spark2")

    assigned: dict[str, set[str]] = {}
    per_node: dict[str, list[WorkloadDefinition]] = {node: [] for node in known_nodes}
    for node in sorted(known_nodes):
        for identifier in placements.get(node, ()):
            definition = catalog.definitions.get(identifier)
            if definition is None:
                errors.add(f"unknown workload: {identifier}")
                continue
            assigned.setdefault(identifier, set()).add(node)
            per_node[node].append(definition)

    for identifier, nodes in assigned.items():
        definition = catalog.definitions[identifier]
        if not _placement_declaration_is_consistent(definition):
            errors.add(f"inconsistent workload placement declaration: {identifier}")
        if definition.topology == "distributed" and nodes != set(definition.nodes):
            errors.add(f"distributed reservation is partial for {identifier}")
        elif definition.topology == "single" and nodes - set(definition.nodes):
            errors.add(f"single workload placement is invalid for {identifier}")

    for node, definitions in per_node.items():
        if len(definitions) > 1:
            incompatible = sorted(
                definition.id
                for definition in definitions
                if not _can_co_locate(definition)
            )
            if incompatible:
                errors.add(
                    f"incompatible co-location on {node}: {', '.join(incompatible)}"
                )
        for definition in definitions:
            if definition.conflicts and any(
                other.id in definition.conflicts for other in definitions
            ):
                errors.add(f"conflicting workloads on {node}: {definition.id}")

    for endpoint, identifier in profile.endpoints.items():
        if identifier not in catalog.definitions:
            errors.add(f"endpoint {endpoint} references unknown workload: {identifier}")
        elif identifier not in assigned:
            errors.add(f"endpoint {endpoint} targets unassigned workload: {identifier}")

    return AdmissionReport(errors=tuple(sorted(errors))), assigned, per_node


def check_placement_policy(
    profile: ClusterProfile, catalog: Catalog
) -> AdmissionReport:
    """Return deterministic structural placement and co-residency violations."""
    report, _, _ = _placement_policy(profile, catalog)
    return report


def check_admission(
    profile: ClusterProfile,
    catalog: Catalog,
    inventory: Mapping[str, Any],
    accepted: Mapping[str, tuple[str, ...]] | None = None,
) -> AdmissionReport:
    """Return all deterministic reasons a profile must not be activated."""
    placement_report, assigned, per_node = _placement_policy(profile, catalog)
    errors = set(placement_report.errors)

    for identifier in assigned:
        definition = catalog.definitions[identifier]
        if catalog.maturity.get(identifier) != "accepted":
            errors.add(
                f"{identifier} maturity is {catalog.maturity.get(identifier, 'missing')}"
            )
        elif (
            catalog.maturity_fingerprints.get(identifier)
            != catalog.definition_fingerprints[identifier]
        ):
            errors.add(f"{identifier} accepted fingerprint does not match definition")
        elif definition.checkpoint.manifest_sha256 is None:
            errors.add("accepted definition requires manifest_sha256")

    for node, definitions in per_node.items():
        paths: dict[Path, str] = {}
        ports: dict[int, str] = {}
        for definition in definitions:
            for kind, path in (
                ("cache", definition.paths.cache),
                ("output", definition.paths.output),
            ):
                previous = paths.get(path)
                if previous is not None and previous != kind:
                    errors.add(f"cache/output overlap on {node}: {path.as_posix()}")
                paths[path] = kind
            previous_port = ports.get(definition.endpoint.port)
            if previous_port is not None and previous_port != definition.id:
                errors.add(f"port collision on {node}: {definition.endpoint.port}")
            ports[definition.endpoint.port] = definition.id
        required_memory = sum(
            item.resources.minimum_free_memory_bytes for item in definitions
        )
        required_disk = sum(
            item.resources.minimum_free_disk_bytes for item in definitions
        )
        measured_memory = _value(inventory.get(node), "free_memory_bytes")
        measured_disk = _value(inventory.get(node), "free_disk_bytes")
        if definitions and (
            measured_memory is None or measured_memory < required_memory
        ):
            errors.add(f"insufficient measured memory on {node}")
        if definitions and (measured_disk is None or measured_disk < required_disk):
            errors.add(f"insufficient measured disk on {node}")
        if definitions and not _healthy(inventory.get(node)):
            errors.add(f"{node} is unhealthy")

    all_definitions = {
        identifier: catalog.definitions[identifier]
        for identifier in assigned
        if identifier in catalog.definitions
    }
    if all_definitions and not _accepted_for_profile(
        profile, all_definitions, catalog, accepted
    ):
        errors.add("profile has no exact accepted evidence")

    endpoint_ports: dict[int, str] = {}
    for endpoint, identifier in profile.endpoints.items():
        definition = catalog.definitions.get(identifier)
        if definition is None:
            errors.add(f"endpoint {endpoint} references unknown workload: {identifier}")
            continue
        if identifier not in assigned:
            errors.add(f"endpoint {endpoint} targets unassigned workload: {identifier}")
            continue
        previous = endpoint_ports.get(definition.endpoint.port)
        if previous is not None:
            errors.add(
                f"port collision for published endpoints: {previous}, {endpoint}"
            )
        endpoint_ports[definition.endpoint.port] = endpoint
        if catalog.maturity.get(identifier) != "accepted":
            errors.add(f"endpoint {endpoint} targets unaccepted workload: {identifier}")
        if any(
            not _healthy(inventory.get(node))
            for node in assigned.get(identifier, set())
        ):
            errors.add(f"endpoint {endpoint} targets unhealthy workload: {identifier}")

    return AdmissionReport(errors=tuple(sorted(errors)))
