from __future__ import annotations

import pytest

from dgx_agent_protocol import (
    AgentOperation,
    AgentProtocolError,
    RecipeOperationRequest,
)


INSTALL = {
    "schema_version": 1,
    "installation_id": "00000000-0000-4000-8000-000000000001",
    "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
    "recipe_content_sha256": "a" * 64,
    "plan_digest": "b" * 64,
    "expected_bytes": 100,
}
START = {
    "schema_version": 1,
    "run_id": "00000000-0000-4000-8000-000000000003",
    "installation_id": INSTALL["installation_id"],
    "recipe_revision_id": INSTALL["recipe_revision_id"],
    "recipe_content_sha256": "a" * 64,
    "plan_digest": "c" * 64,
    "alias": "qwen3",
    "rank": 0,
    "role": "entrypoint",
    "port": 8000,
    "reserved_memory_bytes": 200,
}
STOP = {
    "schema_version": 1,
    "run_id": START["run_id"],
    "plan_digest": START["plan_digest"],
}
UNINSTALL = {
    "schema_version": 1,
    "installation_id": INSTALL["installation_id"],
    "recipe_content_sha256": INSTALL["recipe_content_sha256"],
    "plan_digest": INSTALL["plan_digest"],
}


def test_recipe_operation_vocabulary_is_closed() -> None:
    assert {
        operation.value
        for operation in AgentOperation
        if operation.value.startswith("recipe.")
    } == {
        "recipe.install",
        "recipe.start",
        "recipe.stop",
        "recipe.uninstall",
    }


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (AgentOperation.RECIPE_INSTALL, INSTALL),
        (AgentOperation.RECIPE_START, START),
        (AgentOperation.RECIPE_STOP, STOP),
        (AgentOperation.RECIPE_UNINSTALL, UNINSTALL),
    ],
)
def test_recipe_operation_payloads_are_typed_and_digest_bound(
    operation: AgentOperation, payload: dict[str, object]
) -> None:
    request = RecipeOperationRequest.parse(operation, payload)

    assert request.operation is operation
    assert request.plan_digest == payload["plan_digest"]


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (AgentOperation.RECIPE_INSTALL, INSTALL | {"shell": "curl evil"}),
        (AgentOperation.RECIPE_START, START | {"environment": {"TOKEN": "x"}}),
        (AgentOperation.RECIPE_STOP, STOP | {"plan_digest": "not-a-digest"}),
        (AgentOperation.RECIPE_UNINSTALL, UNINSTALL | {"host_path": "/tmp"}),
    ],
)
def test_recipe_operations_reject_hacks_unknown_fields_and_weak_identity(
    operation: AgentOperation, payload: dict[str, object]
) -> None:
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(operation, payload)
