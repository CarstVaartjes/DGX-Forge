from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spark_profiles.fleet.loaders import (
    TopologyValidationError,
    validate_topology_references,
)


@pytest.fixture
def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _validator(repository_root: Path, name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((repository_root / "schemas" / name).read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _fleet_document(count: int) -> dict[str, object]:
    nodes = {}
    for index in range(count):
        node_id = f"spk_{index:032x}"
        nodes[node_id] = {
            "display_name": f"node-{index}",
            "hostname": f"spark-{index}",
            "management": {
                "host": f"spark-{index}.local",
                "user": "operator",
                "port": 22,
            },
            "labels": {"rack": "lab"},
            "lifecycle": "ready",
        }
    return {"schema_version": 2, "nodes": nodes}


@pytest.mark.parametrize("count", [1, 16, 32])
def test_fleet_schema_accepts_variable_node_counts(
    repository_root: Path,
    count: int,
) -> None:
    _validator(repository_root, "fleet.schema.json").validate(
        _fleet_document(count)
    )


def test_fleet_schema_rejects_fixed_name_identity(repository_root: Path) -> None:
    document = _fleet_document(1)
    node = document["nodes"].pop("spk_00000000000000000000000000000000")
    document["nodes"]["spark1"] = node

    with pytest.raises(jsonschema.ValidationError):
        _validator(repository_root, "fleet.schema.json").validate(document)


def test_fleet_schema_rejects_embedded_credentials_and_unknown_fields(
    repository_root: Path,
) -> None:
    document = _fleet_document(1)
    node = document["nodes"]["spk_00000000000000000000000000000000"]
    node["management"]["password"] = "not-allowed"

    with pytest.raises(jsonschema.ValidationError):
        _validator(repository_root, "fleet.schema.json").validate(document)


def test_topology_schema_accepts_named_links_without_fixed_function_names(
    repository_root: Path,
) -> None:
    node_a = "spk_00000000000000000000000000000001"
    node_b = "spk_00000000000000000000000000000002"
    document = {
        "schema_version": 1,
        "nodes": [node_a, node_b],
        "links": [
            {
                "id": "fabric-lab-a",
                "kind": "direct-rdma",
                "accepted": True,
                "endpoints": [
                    {
                        "node_id": node_a,
                        "interface": "cx7-a",
                        "address": "10.90.0.1/30",
                    },
                    {
                        "node_id": node_b,
                        "interface": "cx7-z",
                        "address": "10.90.0.2/30",
                    },
                ],
            }
        ],
    }

    _validator(repository_root, "topology.schema.json").validate(document)


@pytest.mark.parametrize("kind", ["unknown", "head-worker", "function100"])
def test_topology_schema_rejects_undeclared_link_kinds(
    repository_root: Path,
    kind: str,
) -> None:
    node_a = "spk_00000000000000000000000000000001"
    node_b = "spk_00000000000000000000000000000002"
    document = {
        "schema_version": 1,
        "nodes": [node_a, node_b],
        "links": [
            {
                "id": "link-a",
                "kind": kind,
                "accepted": False,
                "endpoints": [
                    {"node_id": node_a, "interface": "a"},
                    {"node_id": node_b, "interface": "b"},
                ],
            }
        ],
    }

    with pytest.raises(jsonschema.ValidationError):
        _validator(repository_root, "topology.schema.json").validate(document)


def test_topology_reference_validation_rejects_unknown_endpoint_node() -> None:
    declared = "spk_00000000000000000000000000000001"
    unknown = "spk_ffffffffffffffffffffffffffffffff"
    document = {
        "schema_version": 1,
        "nodes": [declared],
        "links": [
            {
                "id": "management-a",
                "kind": "management",
                "accepted": True,
                "endpoints": [
                    {"node_id": declared, "interface": "wifi0"},
                    {"node_id": unknown, "interface": "wifi0"},
                ],
            }
        ],
    }

    with pytest.raises(TopologyValidationError, match=f"unknown node {unknown}"):
        validate_topology_references(document)


def test_topology_reference_validation_rejects_duplicate_link_ids() -> None:
    node_a = "spk_00000000000000000000000000000001"
    node_b = "spk_00000000000000000000000000000002"
    link = {
        "id": "management-a",
        "kind": "management",
        "accepted": True,
        "endpoints": [
            {"node_id": node_a, "interface": "wifi0"},
            {"node_id": node_b, "interface": "wifi0"},
        ],
    }

    with pytest.raises(TopologyValidationError, match="duplicate link id"):
        validate_topology_references(
            {"schema_version": 1, "nodes": [node_a, node_b], "links": [link, link]}
        )
