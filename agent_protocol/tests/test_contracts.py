from __future__ import annotations

import hashlib
import importlib.resources
import json
from pathlib import Path

import pytest

from dgx_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProgress,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)


def valid_claim() -> dict[str, object]:
    return {
        "schema_version": 1,
        "job_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "attempt": 1,
        "fence": "00000000-0000-4000-8000-000000000003",
        "node_id": "spk_00000000000000000000000000000001",
        "operation": "node.probe",
        "base_commit": "a" * 40,
        "payload_digest": hashlib.sha256(b"{}").hexdigest(),
        "payload": {},
        "deadline": "2026-08-03T12:00:00+00:00",
    }


def valid_attempt() -> dict[str, object]:
    return {
        key: valid_claim()[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    }


def test_claim_is_node_scoped_and_canonical() -> None:
    claim = AgentClaim.parse(valid_claim())

    assert json.loads(canonical_message(claim))["operation"] == "node.probe"


@pytest.mark.parametrize("field", ["command", "shell", "environment", "password"])
def test_protocol_rejects_execution_and_secret_fields(field: str) -> None:
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(valid_claim() | {"payload": {field: "unsafe"}})


def test_protocol_rejects_unsafe_keys_recursively() -> None:
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(valid_claim() | {"payload": {"safe": {"apiToken": "unsafe"}}})


def test_claim_rejects_changed_payload_digest() -> None:
    with pytest.raises(AgentProtocolError, match="digest"):
        AgentClaim.parse(valid_claim() | {"payload": {"healthy": True}})


@pytest.mark.parametrize(
    "deadline",
    ["2026-08-03T12:00:00", "2026-08-03T12:00:00+02:00"],
)
def test_claim_requires_an_aware_utc_deadline(deadline: str) -> None:
    with pytest.raises(AgentProtocolError, match="deadline"):
        AgentClaim.parse(valid_claim() | {"deadline": deadline})


def test_claim_copies_canonical_payload_before_becoming_frozen() -> None:
    source = valid_claim() | {"payload": {"nested": ["before"]}}
    source["payload_digest"] = hashlib.sha256(
        canonical_message(source["payload"])
    ).hexdigest()
    claim = AgentClaim.parse(source)
    source["payload"]["nested"].append("after")  # type: ignore[index]

    assert json.loads(canonical_message(claim))["payload"] == {"nested": ["before"]}
    with pytest.raises(AttributeError):
        claim.attempt = 2  # type: ignore[misc]


def test_progress_and_result_are_fenced_node_messages() -> None:
    progress = AgentProgress.parse(valid_attempt() | {"progress": {"phase": "probe"}})
    result = AgentResult.parse(
        valid_attempt() | {"state": "succeeded", "result": {"healthy": True}}
    )

    assert progress.node_id == "spk_00000000000000000000000000000001"
    assert result.state == "succeeded"


def test_results_are_bounded_and_reject_secret_bearing_keys() -> None:
    with pytest.raises(AgentProtocolError, match="large"):
        AgentResult.parse(valid_attempt() | {"state": "succeeded", "result": {"x": "x" * 65536}})
    with pytest.raises(AgentProtocolError):
        AgentResult.parse(
            valid_attempt() | {"state": "succeeded", "result": {"private_key": "unsafe"}}
        )


def test_operation_enum_contains_only_supported_operations() -> None:
    assert {member.value for member in AgentOperation} == {
        "node.probe",
        "release.install",
        "workload.prepare",
        "workload.start",
        "workload.stop",
        "workload.health",
        "workload.verify",
        "agent.update",
        "agent.rollback",
    }


@pytest.mark.parametrize("name", ["agent-job.schema.json", "agent-result.schema.json"])
def test_packaged_schemas_match_repository_bytes(name: str) -> None:
    repository_schema = (
        Path(__file__).parents[1] / "src" / "dgx_agent_protocol" / "schemas" / name
    ).read_bytes()
    packaged_schema = (
        importlib.resources.files("dgx_agent_protocol") / "schemas" / name
    ).read_bytes()

    assert packaged_schema == repository_schema
