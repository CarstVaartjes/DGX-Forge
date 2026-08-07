import json
from pathlib import Path

from cluster_profiles.admission import check_generic_admission
from cluster_profiles.contracts import (
    ClusterProfile,
    GenericClusterProfile,
    LifecycleConstraints,
)
from cluster_profiles.fleet import Fleet, ManagementEndpoint, NodeId, NodeRecord
from cluster_profiles.placement import NodeObservation, PlacementRequirement
from cluster_profiles.profile_compat import adapt_legacy_profile

ROOT = Path(__file__).resolve().parents[2]


def legacy_fleet() -> Fleet:
    records = {}
    for index in range(2):
        node_id = NodeId.parse(f"spk_{index:032x}")
        records[node_id] = NodeRecord(node_id, f"node-{index}", f"node-{index}.local", ManagementEndpoint(f"node-{index}.local", "operator"), {}, "ready")
    return Fleet(2, records)


def test_current_dual_profile_adapts_to_same_lifecycle_order() -> None:
    profile = ClusterProfile(
        "agent-full-dual", Path("evidence.json"),
        {"spark1": ("deepseek",), "spark2": ("deepseek",)},
        {"agent": "deepseek"},
    )
    generic = adapt_legacy_profile(profile, legacy_fleet())
    assert generic.requirements[0].node_count == 2
    assert generic.lifecycle.start_order == "workers-before-entrypoint"
    assert generic.lifecycle.stop_order == "entrypoint-before-workers"
    assert all(node.startswith("spk_") for node in generic.requirements[0].preferred_node_ids)


def test_single_legacy_placement_pins_one_generated_node() -> None:
    profile = ClusterProfile("creative", Path("evidence.json"), {"spark1": ("trellis",), "spark2": ()}, {"creative": "trellis"})
    generic = adapt_legacy_profile(profile, legacy_fleet())
    assert generic.requirements[0].node_count == 1
    assert generic.requirements[0].preferred_node_ids == ("spk_00000000000000000000000000000000",)


def test_v2_schemas_have_no_spark_named_properties() -> None:
    for name in ("cluster-profile-v2.schema.json", "workload-v2.schema.json"):
        encoded = json.dumps(json.loads((ROOT / "schemas" / name).read_text()))
        assert '"spark1"' not in encoded and '"spark2"' not in encoded


def test_generic_admission_reserves_exclusive_nodes_across_requirements() -> None:
    subject = legacy_fleet()
    requirement = lambda name: PlacementRequirement(name, "a" * 64, 1, {}, 1, 1, True, False, False)
    profile = GenericClusterProfile("two", Path("evidence"), ("a", "b"), (requirement("a"), requirement("b")), {}, {}, LifecycleConstraints("independent", "independent"))
    topology = {"schema_version": 1, "nodes": [node.value for node in subject.nodes], "links": []}
    observations = tuple(NodeObservation(node, True, 10, 10, False) for node in subject.nodes)
    report, plans = check_generic_admission(profile, subject, topology, observations)
    assert report.ok
    assert plans[0].nodes != plans[1].nodes
