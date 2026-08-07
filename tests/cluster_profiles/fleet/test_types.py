from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from cluster_profiles.fleet import (
    Fleet,
    ManagementEndpoint,
    NodeId,
    NodeRecord,
)


def _node(index: int, *, lifecycle: str = "ready") -> NodeRecord:
    return NodeRecord(
        id=NodeId.parse(f"spk_{index:032x}"),
        display_name=f"lab-{index}",
        hostname=f"node-{index}",
        management=ManagementEndpoint(
            host=f"node-{index}.local",
            user="operator",
        ),
        labels={"rack": "lab"},
        lifecycle=lifecycle,
    )


def test_fleet_accepts_more_than_sixteen_nodes_without_fixed_names() -> None:
    records = [_node(index) for index in range(32)]

    fleet = Fleet(schema_version=2, nodes={record.id: record for record in records})

    assert len(fleet.ready_nodes()) == 32
    assert fleet.ready_nodes()[17].display_name == "lab-17"
    assert all(node.id.value.startswith("spk_") for node in fleet.ready_nodes())


def test_ready_nodes_excludes_nonready_nodes_and_has_stable_identity_order() -> None:
    ready_late = _node(10)
    quarantined = _node(1, lifecycle="quarantined")
    ready_early = _node(2)
    fleet = Fleet(
        schema_version=2,
        nodes={
            ready_late.id: ready_late,
            quarantined.id: quarantined,
            ready_early.id: ready_early,
        },
    )

    assert fleet.ready_nodes() == (ready_early, ready_late)
    assert fleet.node(ready_late.id) is ready_late


def test_node_record_and_copied_labels_are_immutable() -> None:
    labels = {"rack": "lab"}
    record = NodeRecord(
        id=NodeId.parse("spk_00000000000000000000000000000001"),
        display_name="alpha",
        hostname="node-alpha",
        management=ManagementEndpoint(host="alpha.local", user="operator"),
        labels=labels,
        lifecycle="ready",
    )
    labels["rack"] = "changed"

    assert record.labels == {"rack": "lab"}
    with pytest.raises(FrozenInstanceError):
        record.id = NodeId.parse("spk_ffffffffffffffffffffffffffffffff")
    with pytest.raises(TypeError):
        record.labels["rack"] = "changed"


@pytest.mark.parametrize(
    "value",
    [
        "node1",
        "",
        "spk_1",
        "spk_" + "g" * 32,
        "SPK_" + "0" * 32,
    ],
)
def test_node_id_rejects_names_and_malformed_ids(value: str) -> None:
    with pytest.raises(
        ValueError,
        match=r"node id must match spk_<32 lowercase hex characters>",
    ):
        NodeId.parse(value)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (ManagementEndpoint(host="", user="operator"), "host"),
        (ManagementEndpoint(host="alpha.local", user=""), "user"),
        (ManagementEndpoint(host="alpha.local", user="operator", port=0), "port"),
        (
            ManagementEndpoint(host="alpha.local", user="operator", port=65536),
            "port",
        ),
    ],
)
def test_management_endpoint_rejects_unusable_connection_data(
    endpoint: ManagementEndpoint,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        NodeRecord(
            id=NodeId.parse("spk_00000000000000000000000000000001"),
            display_name="alpha",
            hostname="node-alpha",
            management=endpoint,
            labels={},
            lifecycle="ready",
        )


def test_fleet_rejects_duplicate_display_names() -> None:
    first = _node(1)
    second = NodeRecord(
        id=NodeId.parse("spk_00000000000000000000000000000002"),
        display_name=first.display_name,
        hostname="different-host",
        management=ManagementEndpoint(host="different.local", user="operator"),
        labels={},
        lifecycle="ready",
    )

    with pytest.raises(ValueError, match="display names must be unique"):
        Fleet(schema_version=2, nodes={first.id: first, second.id: second})


def test_fleet_rejects_empty_nodes_and_unknown_schema() -> None:
    with pytest.raises(ValueError, match="at least one node"):
        Fleet(schema_version=2, nodes={})
    with pytest.raises(ValueError, match="unsupported fleet schema version"):
        Fleet(schema_version=3, nodes={_node(1).id: _node(1)})
