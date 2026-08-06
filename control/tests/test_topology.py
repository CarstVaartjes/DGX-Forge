import json
from pathlib import Path

import pytest

from dgx_control.topology import Placement, TopologyError, validate_topology


def multinode():
    document = json.loads((Path(__file__).parent/"fixtures/global/recipe-v1-multinode.json").read_text())
    return document


def test_multinode_topology_has_one_entrypoint_and_deterministic_ranks() -> None:
    placements = (Placement("spk_"+"1"*32, 1, "worker"), Placement("spk_"+"2"*32, 0, "entrypoint"))
    result = validate_topology(multinode(), placements, {item.node_id: ("runtime.sglang.v1", "fabric.tcp.mbps.10000") for item in placements})
    assert [item.rank for item in result] == [0, 1]
    assert result[0].role == "entrypoint"


@pytest.mark.parametrize("placements", [
    (Placement("spk_"+"1"*32, 0, "entrypoint"), Placement("spk_"+"2"*32, 0, "worker")),
    (Placement("spk_"+"1"*32, 0, "entrypoint"), Placement("spk_"+"1"*32, 1, "worker")),
    (Placement("spk_"+"1"*32, 0, "worker"), Placement("spk_"+"2"*32, 1, "worker")),
])
def test_invalid_rank_node_and_entrypoint_shapes_are_blocked(placements) -> None:
    with pytest.raises(TopologyError):
        validate_topology(multinode(), placements, {item.node_id: ("runtime.sglang.v1", "fabric.tcp.mbps.10000") for item in placements})


def test_missing_runtime_or_fabric_capability_is_blocking() -> None:
    placements = (Placement("spk_"+"1"*32, 0, "entrypoint"), Placement("spk_"+"2"*32, 1, "worker"))
    with pytest.raises(TopologyError) as caught:
        validate_topology(multinode(), placements, {item.node_id: ("runtime.vllm.v1", "fabric.tcp.mbps.10") for item in placements})
    assert caught.value.code == "topology.runtime_capability_missing"
