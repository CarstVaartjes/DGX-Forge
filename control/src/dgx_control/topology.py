"""Deterministic rank, capability, and fabric validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class TopologyError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code; super().__init__(detail)


@dataclass(frozen=True, slots=True)
class Placement:
    node_id: str; rank: int; role: str


def validate_topology(recipe: Mapping[str, object], placements: Sequence[Placement], capabilities: Mapping[str, tuple[str, ...]]) -> tuple[Placement, ...]:
    topology, runtime = recipe.get("topology"), recipe.get("runtime")
    if not isinstance(topology, Mapping) or not isinstance(runtime, Mapping): raise TopologyError("topology.recipe_invalid", "recipe topology is invalid")
    ordered = tuple(sorted(placements, key=lambda item: item.rank)); nodes=[item.node_id for item in ordered]; ranks=[item.rank for item in ordered]
    if not ordered or len(nodes)!=len(set(nodes)) or ranks!=list(range(len(ordered))) or sum(item.role=="entrypoint" for item in ordered)!=1 or any(item.role not in {"entrypoint","worker"} for item in ordered):
        raise TopologyError("topology.placement_invalid", "placement must contain unique contiguous ranks and one entrypoint")
    if not int(topology["min_nodes"]) <= len(ordered) <= int(topology["max_nodes"]): raise TopologyError("topology.node_count", "placement node count is outside recipe bounds")
    required_runtime="runtime.vonk.v1"
    if any(required_runtime not in capabilities.get(node, ()) for node in nodes): raise TopologyError("topology.runtime_capability_missing", f"every Spark must advertise {required_runtime}")
    declared=topology.get("ranks")
    if topology.get("kind")=="gang":
        if not isinstance(declared,list) or [(item.get("rank"),item.get("role")) for item in declared if isinstance(item,Mapping)] != [(item.rank,item.role) for item in ordered]: raise TopologyError("topology.rank_mismatch", "placement ranks do not match recipe ranks")
        fabric=topology.get("fabric")
        if not isinstance(fabric,Mapping): raise TopologyError("topology.fabric_missing", "recipe fabric is missing")
        required=int(fabric["minimum_bandwidth_mbps"]); transport=str(fabric["transport"]); prefix=f"fabric.{transport}.mbps."
        for node in nodes:
            speeds=[int(value.removeprefix(prefix)) for value in capabilities.get(node,()) if value.startswith(prefix) and value.removeprefix(prefix).isdigit()]
            if not speeds or max(speeds)<required: raise TopologyError("topology.fabric_insufficient", f"{node} lacks {required} Mbps {transport} fabric")
    elif len(ordered)!=1 or ordered[0].rank!=0 or ordered[0].role!="entrypoint": raise TopologyError("topology.single_invalid", "single-node recipe requires rank-zero entrypoint")
    for argument in runtime.get("arguments", []):
        if isinstance(argument,Mapping) and argument.get("name") in {"tensor_parallel_size","pipeline_parallel_size"}:
            value=argument.get("value")
            if isinstance(value,int) and value>1 and len(ordered)%value!=0: raise TopologyError("topology.parallelism_mismatch", "rank count is not divisible by declared parallelism")
    return ordered
