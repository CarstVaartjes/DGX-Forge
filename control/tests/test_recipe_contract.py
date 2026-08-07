import json
from pathlib import Path

import pytest
from dgx_control.recipe_contract import (
    RecipeContractError,
    canonical_recipe,
    parse_recipe_json,
    recipe_content_sha256,
    validate_recipe,
)

ROOT = Path(__file__).resolve().parents[2]


def fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / "control/tests/fixtures/global" / name).read_text())


def contract_lock() -> dict[str, object]:
    return json.loads((ROOT / "schemas/global/contract.lock.json").read_text())


def test_recipe_hash_matches_global_fixture() -> None:
    expected = contract_lock()["fixtures"]["recipe-v1-minimal.json"][
        "content_sha256"
    ]

    assert recipe_content_sha256(fixture("recipe-v1-minimal.json")) == expected


def test_canonical_recipe_matches_global_bytes() -> None:
    assert canonical_recipe({"z": 1, "a": [True, None]}) == (
        b'{"a":[true,null],"z":1}'
    )


def test_recipe_parser_rejects_duplicate_keys_and_floats() -> None:
    with pytest.raises(RecipeContractError, match="duplicate object key"):
        parse_recipe_json(b'{"identity":{},"identity":{}}')
    with pytest.raises(RecipeContractError, match="floats are not permitted"):
        parse_recipe_json(b'{"value":1.5}')


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("runtime", "image"), "ghcr.io/vonk/vllm:latest", "image"),
        (("runtime", "command"), ["sh", "-c", "id"], "command"),
        (("security", "privileged"), True, "privileged"),
    ],
)
def test_recipe_validation_rejects_unsafe_values(path, value, message) -> None:
    document = fixture("recipe-v1-minimal.json")
    section, field = path
    document[section][field] = value

    with pytest.raises(RecipeContractError, match=message):
        validate_recipe(document)


def test_global_contract_lock_matches_vendored_bytes() -> None:
    lock = contract_lock()
    assert lock["source_commit"] == "5b9304d19ebd581270bf4f848ed177d9bcd9982d"
    for relative_path, metadata in lock["files"].items():
        payload = (ROOT / relative_path).read_bytes()
        assert __import__("hashlib").sha256(payload).hexdigest() == metadata["sha256"]
