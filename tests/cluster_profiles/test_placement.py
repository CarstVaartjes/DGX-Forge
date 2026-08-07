from __future__ import annotations

from dataclasses import replace

import pytest

from cluster_profiles.fleet import Fleet, ManagementEndpoint, NodeId, NodeRecord
from cluster_profiles.placement import (
    NodeObservation,
    PlacementError,
    PlacementPlanner,
    PlacementRequirement,
)


def fleet(count: int, *, lifecycle: str = "ready") -> Fleet:
    records = {}
    for index in range(count):
        node_id = NodeId.parse(f"spk_{index:032x}")
        records[node_id] = NodeRecord(
            node_id, f"node-{index}", f"node-{index}.local",
            ManagementEndpoint(f"node-{index}.local", "operator"),
            {"pool": "default", "zone": "a" if index % 2 == 0 else "b"},
            lifecycle,
        )
    return Fleet(2, records)


def observations(subject: Fleet) -> tuple[NodeObservation, ...]:
    return tuple(
        NodeObservation(node_id, True, 1000, 2000, False)
        for node_id in subject.nodes
    )


def topology(subject: Fleet, *, accepted: bool = True) -> dict[str, object]:
    return {
        "schema_version": 1,
        "nodes": [node_id.value for node_id in subject.nodes],
        "links": [{
            "id": "fabric", "kind": "switched-rdma", "accepted": accepted,
            "endpoints": [{"node_id": node_id.value, "interface": f"eth{index}"} for index, node_id in enumerate(subject.nodes)],
        }],
    }


def requirement(**overrides) -> PlacementRequirement:
    base = PlacementRequirement(
        name="agent", definition_hash="a" * 64, node_count=1,
        required_labels={"pool": "default"}, min_memory_bytes=500,
        min_disk_bytes=1000, exclusive=True, distributed=False,
        model_supports_distributed=False, preferred_node_ids=(),
    )
    return replace(base, **overrides)


def test_single_node_placement_is_deterministic() -> None:
    subject = fleet(4)
    first = PlacementPlanner().plan(requirement(), subject, topology(subject), observations(subject))
    second = PlacementPlanner().plan(requirement(), subject, topology(subject), reversed(observations(subject)))
    assert first == second
    assert first.nodes == (NodeId.parse("spk_00000000000000000000000000000000"),)


def test_distributed_requirement_rejects_unaccepted_link() -> None:
    subject = fleet(2)
    distributed = requirement(node_count=2, distributed=True, model_supports_distributed=True)
    with pytest.raises(PlacementError, match="accepted topology"):
        PlacementPlanner().plan(distributed, subject, topology(subject, accepted=False), observations(subject))


def test_distributed_requirement_requires_definition_support() -> None:
    subject = fleet(2)
    with pytest.raises(PlacementError, match="definition"):
        PlacementPlanner().plan(requirement(node_count=2, distributed=True), subject, topology(subject), observations(subject))


def test_filters_health_capacity_exclusivity_and_labels() -> None:
    subject = fleet(4)
    values = list(observations(subject))
    values[0] = replace(values[0], healthy=False)
    values[1] = replace(values[1], memory_available_bytes=1)
    values[2] = replace(values[2], occupied=True)
    selected = PlacementPlanner().plan(requirement(), subject, topology(subject), values)
    assert selected.nodes == (NodeId.parse("spk_00000000000000000000000000000003"),)
    assert set(selected.reasons) == {node_id.value for node_id in subject.nodes}


def test_explicitly_unavailable_node_is_rejected_for_nonexclusive_placement() -> None:
    subject = fleet(2)
    values = list(observations(subject))
    values[0] = replace(values[0], available_for_placement=False)

    selected = PlacementPlanner().plan(
        requirement(exclusive=False), subject, topology(subject), values
    )

    assert selected.nodes == (
        NodeId.parse("spk_00000000000000000000000000000001"),
    )


def test_explicit_preference_then_node_id_controls_stable_order() -> None:
    subject = fleet(4)
    preferred = "spk_00000000000000000000000000000003"
    plan = PlacementPlanner().plan(requirement(preferred_node_ids=(preferred,)), subject, topology(subject), observations(subject))
    assert plan.nodes[0].value == preferred
