import json
from pathlib import Path

import pytest
from vonk_control.topology import Placement, TopologyError, validate_topology


def multinode():
    return json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-multinode.json").read_text()
    )


def placements():
    return (
        Placement("spk_" + "1" * 32, 1, "worker"),
        Placement("spk_" + "2" * 32, 0, "entrypoint"),
        Placement("spk_" + "3" * 32, 2, "worker"),
    )


def capabilities(values):
    return {
        item.node_id: ("runtime.vonk.v1", "fabric.full_mesh.mbps.10000")
        for item in values
    }


def test_three_node_topology_has_deterministic_ranks() -> None:
    values = placements()

    result = validate_topology(multinode(), "triple-tp3", values, capabilities(values))

    assert [item.rank for item in result] == [0, 1, 2]
    assert result[0].role == "entrypoint"


@pytest.mark.parametrize(
    "values",
    [
        (
            Placement("spk_" + "1" * 32, 0, "entrypoint"),
            Placement("spk_" + "2" * 32, 0, "worker"),
            Placement("spk_" + "3" * 32, 2, "worker"),
        ),
        (
            Placement("spk_" + "1" * 32, 0, "entrypoint"),
            Placement("spk_" + "1" * 32, 1, "worker"),
            Placement("spk_" + "3" * 32, 2, "worker"),
        ),
        (
            Placement("spk_" + "1" * 32, 0, "worker"),
            Placement("spk_" + "2" * 32, 1, "worker"),
            Placement("spk_" + "3" * 32, 2, "worker"),
        ),
    ],
)
def test_invalid_rank_node_and_role_shapes_are_blocked(values) -> None:
    with pytest.raises(TopologyError):
        validate_topology(multinode(), "triple-tp3", values, capabilities(values))


def test_missing_runtime_or_fabric_capability_is_blocking() -> None:
    values = placements()
    with pytest.raises(TopologyError) as caught:
        validate_topology(
            multinode(),
            "triple-tp3",
            values,
            {
                item.node_id: ("runtime.sglang.v1", "fabric.full_mesh.mbps.10000")
                for item in values
            },
        )
    assert caught.value.code == "topology.runtime_capability_missing"

    with pytest.raises(TopologyError) as caught:
        validate_topology(
            multinode(),
            "triple-tp3",
            values,
            {
                item.node_id: ("runtime.vonk.v1", "fabric.connected.mbps.10000")
                for item in values
            },
        )
    assert caught.value.code == "topology.fabric_insufficient"
