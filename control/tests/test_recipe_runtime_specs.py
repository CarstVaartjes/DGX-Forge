from __future__ import annotations

import json
from pathlib import Path

from vonk_control.recipe_runtime_specs import compile_runtime_spec


def test_runtime_spec_binds_built_image_role_and_mapping_parameters() -> None:
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-multinode.json").read_text()
    )
    parameter = {
        "name": "mapped-value",
        "description": "Mapped runtime fixture",
        "type": "integer",
        "default": 7,
        "change_effect": "restart",
    }
    document["parameters"].append(parameter)
    parameter_name = parameter["name"]
    document["runtime"]["arguments"].append(
        {"name": "mapped-value", "parameter": parameter_name}
    )

    spec = compile_runtime_spec(
        document,
        parameters={parameter_name: parameter["default"]},
        role="worker",
        recipe_build_id="00000000-0000-4000-8000-000000000001",
        image_digest="sha256:" + "d" * 64,
    )

    assert spec["runtime"]["image"] == (
        "localhost/vonk/recipe-build-00000000-0000-4000-8000-000000000001"
        "@sha256:" + "d" * 64
    )
    assert spec["runtime"]["adapter"] == document["runtime"]["adapter"]
    assert {item["name"]: item["value"] for item in spec["runtime"]["arguments"]}[
        "mapped-value"
    ] == parameter["default"]
    assert all("worker" in item["roles"] for item in spec["artifacts"])
    assert spec["endpoint"] == document["runtime"]["endpoint"]
    assert spec["security"] == document["runtime"]["security"]
    assert spec["lifecycle"] == document["runtime"]["lifecycle"]
