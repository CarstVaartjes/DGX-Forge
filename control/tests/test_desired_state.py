from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dgx_agent_protocol import canonical_message
from dgx_control.desired_state import (
    DesiredStateObservation,
    DesiredStateResolver,
    durable_desired_state_observations,
)
from dgx_control.models import AgentNode, Base, Observation
from dgx_control.repository import RepositoryService
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
DEFINITION_HASH = "d" * 64
REQUIRED_CAPABILITIES = (
    "release.install",
    "workload.health",
    "workload.prepare",
    "workload.start",
    "workload.stop",
    "workload.verify",
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GIT_AUTHOR_NAME": "Resolver Test",
            "GIT_AUTHOR_EMAIL": "resolver@example.invalid",
            "GIT_COMMITTER_NAME": "Resolver Test",
            "GIT_COMMITTER_EMAIL": "resolver@example.invalid",
        },
    ).stdout.strip()


def _node_id(index: int) -> str:
    return f"spk_{index:032x}"


def _fleet_toml(count: int, *, reversed_nodes: bool = False) -> str:
    indexes = list(range(count))
    if reversed_nodes:
        indexes.reverse()
    lines = ["schema_version = 2", ""]
    for index in indexes:
        node_id = _node_id(index)
        lines.extend(
            [
                f'[nodes."{node_id}"]',
                f'display_name = "Node {index}"',
                f'hostname = "node-{index}.example.invalid"',
                'lifecycle = "ready"',
                "",
                f'[nodes."{node_id}".management]',
                f'host = "node-{index}.example.invalid"',
                'user = "operator"',
                "port = 22",
                "",
                f'[nodes."{node_id}".labels]',
                'pool = "default"',
                "",
            ]
        )
    return "\n".join(lines)


def _profile_toml(count: int, *, definition_hash: str = DEFINITION_HASH) -> str:
    start_order = "workers-before-entrypoint" if count > 1 else "independent"
    stop_order = "entrypoint-before-workers" if count > 1 else "independent"
    return f'''schema_version = 2
id = "inference"
accepted_evidence = "inventory/reports/inference.json"
workloads = ["model"]

[[requirements]]
workload = "model"
definition_hash = "{definition_hash}"
node_count = {count}
min_memory_bytes = 100
min_disk_bytes = 200
exclusive = true
distributed_supported = true

[requirements.required_labels]
pool = "default"

[endpoints]
chat = "model"

[lifecycle]
start_order = "{start_order}"
stop_order = "{stop_order}"
'''


def _workload_toml(
    *, definition_hash: str = DEFINITION_HASH, distributed_supported: bool = True
) -> str:
    distributed = str(distributed_supported).lower()
    return f'''schema_version = 2
id = "model"
adapter = "repository-agent"
definition_hash = "{definition_hash}"
conflicts = []
distributed_supported = {distributed}
'''


def _topology(count: int) -> dict[str, object]:
    nodes = [_node_id(index) for index in range(count)]
    links: list[dict[str, object]] = []
    if count > 1:
        links.append(
            {
                "id": "fabric",
                "kind": "switched-rdma",
                "accepted": True,
                "endpoints": [
                    {"node_id": node_id, "interface": f"fabric{index}"}
                    for index, node_id in enumerate(nodes)
                ],
            }
        )
    return {"schema_version": 1, "nodes": nodes, "links": links}


def _release(
    *,
    definition_hash: str = DEFINITION_HASH,
    operations: tuple[str, ...] = REQUIRED_CAPABILITIES,
) -> dict[str, object]:
    requests: dict[str, dict[str, object]] = {}
    for operation in operations:
        if operation == "release.install":
            continue
        action = operation.removeprefix("workload.")
        request: dict[str, object] = {
            "schema_version": 1,
            "workload_id": "model",
            "release_digest": "a" * 64,
            "adapter_id": "repository-agent",
        }
        if action == "prepare":
            request["profile_digest"] = "c" * 64
        elif action == "start":
            request["preparation_digest"] = "e" * 64
        elif action == "verify":
            request["expected_digest"] = "f" * 64
        requests[action] = request
    return {
        "schema_version": 1,
        "workload_id": "model",
        "definition_hash": definition_hash,
        "release_request": {
            "schema_version": 1,
            "target_name": "model",
            "oci_manifest_digest": "sha256:" + "a" * 64,
            "target_digest": "a" * 64,
            "provenance_digest": "b" * 64,
            "adapter_id": "repository-agent",
        },
        "workload_requests": requests,
        "endpoint": {"scheme": "http", "port": 8000, "path": "/v1"},
    }


def _repository(
    tmp_path: Path,
    count: int,
    *,
    reversed_nodes: bool = False,
    requirement_hash: str = DEFINITION_HASH,
    workload_hash: str = DEFINITION_HASH,
    release_hash: str = DEFINITION_HASH,
    operations: tuple[str, ...] = REQUIRED_CAPABILITIES,
    workload_distributed_supported: bool = True,
) -> tuple[RepositoryService, str, dict[str, bytes]]:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    documents = {
        "inventory/fleet.toml": _fleet_toml(
            count, reversed_nodes=reversed_nodes
        ).encode(),
        "inventory/topology.json": json.dumps(
            _topology(count), sort_keys=not reversed_nodes, separators=(",", ":")
        ).encode(),
        "inventory/reports/inference.json": b'{"accepted":true,"schema_version":1}',
        "config/cluster-profiles/inference.toml": _profile_toml(
            count, definition_hash=requirement_hash
        ).encode(),
        "config/workloads/model.toml": _workload_toml(
            definition_hash=workload_hash,
            distributed_supported=workload_distributed_supported,
        ).encode(),
        "manifests/releases/model.json": json.dumps(
            _release(definition_hash=release_hash, operations=operations),
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    }
    for name, content in documents.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "desired state")
    return RepositoryService(root), _git(root, "rev-parse", "HEAD"), documents


def _observations(count: int) -> tuple[DesiredStateObservation, ...]:
    return tuple(
        DesiredStateObservation(
            node_id=_node_id(index),
            observed_at=NOW - timedelta(seconds=index),
            healthy=True,
            memory_available_bytes=1_000,
            disk_available_bytes=2_000,
            occupied=False,
            agent_state="active",
            protocol_version=1,
            capabilities=REQUIRED_CAPABILITIES,
        )
        for index in range(count)
    )


def _resolve(
    repository: RepositoryService,
    commit: str,
    observations: tuple[DesiredStateObservation, ...],
):
    return DesiredStateResolver(repository, clock=lambda: NOW).resolve(
        commit, "inference", observations
    )


@pytest.mark.parametrize("count", [1, 2, 16])
def test_resolves_one_two_and_sixteen_nodes_from_exact_repository_objects(
    tmp_path: Path, count: int
) -> None:
    repository, commit, documents = _repository(tmp_path, count)

    plan = _resolve(repository, commit, _observations(count))

    targets = tuple(_node_id(index) for index in range(count))
    assert plan.commit == commit
    assert plan.targets == targets
    assert plan.placements == {"model": targets}
    assert plan.agent_protocol_range == (1, 1)
    assert plan.routes == {
        "chat": {
            "workload_id": "model",
            "nodes": targets,
            "scheme": "http",
            "port": 8000,
            "path": "/v1",
        }
    }
    assert plan.input_digests == {
        path: hashlib.sha256(content).hexdigest()
        for path, content in sorted(documents.items())
    }
    assert plan.operation_graph.reconciliation_id
    assert len(plan.operation_graph.nodes) == count * 6
    assert {node.kind for node in plan.operation_graph.nodes} == {
        "release.install",
        "workload.prepare",
        "workload.start",
        "workload.stop",
        "workload.health",
        "workload.verify",
    }
    assert all(
        node.operation_id in plan.operation_payloads
        and len(node.payload_digest) == 64
        for node in plan.operation_graph.nodes
    )


def test_every_emitted_payload_is_accepted_by_the_exact_agent_parser(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 2)
    plan = _resolve(repository, commit, _observations(2))
    agent_source = Path(__file__).parents[2] / "agent" / "src"
    sys.path.insert(0, str(agent_source))
    try:
        from dgx_agent.releases import ReleaseRequest
        from dgx_agent.workloads import WorkloadAction, WorkloadRequest

        for node in plan.operation_graph.nodes:
            payload = plan.operation_payloads[node.operation_id]
            if node.kind == "release.install":
                ReleaseRequest.parse(payload)
            else:
                action = WorkloadAction(node.kind.removeprefix("workload."))
                WorkloadRequest.parse(action, payload)
            assert node.payload_digest == hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
    finally:
        sys.path.remove(str(agent_source))


def test_start_and_stop_dependencies_follow_lifecycle_order(tmp_path: Path) -> None:
    repository, commit, _ = _repository(tmp_path, 2)
    plan = _resolve(repository, commit, _observations(2))
    head, worker = plan.targets
    graph = plan.operation_graph

    assert graph.dependencies(f"model:{head}:workload.start") == (
        f"model:{head}:workload.prepare",
        f"model:{worker}:workload.start",
    )
    assert graph.dependencies(f"model:{head}:workload.stop") == ()
    assert graph.dependencies(f"model:{worker}:workload.stop") == (
        f"model:{head}:workload.stop",
    )
    assert graph.dependencies(f"model:{head}:release.install") == (
        f"model:{worker}:workload.stop",
    )


def test_rejects_distributed_profile_when_workload_disallows_distribution(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(
        tmp_path, 2, workload_distributed_supported=False
    )

    with pytest.raises(ValueError, match="distributed support"):
        _resolve(repository, commit, _observations(2))


def test_reordered_repository_tables_and_observations_keep_placement_and_graph_stable(
    tmp_path: Path,
) -> None:
    first_repository, first_commit, _ = _repository(tmp_path / "first", 16)
    second_repository, second_commit, _ = _repository(
        tmp_path / "second", 16, reversed_nodes=True
    )

    first = _resolve(first_repository, first_commit, _observations(16))
    second = _resolve(
        second_repository, second_commit, tuple(reversed(_observations(16)))
    )

    assert first.placements == second.placements
    assert first.targets == second.targets
    assert first.operation_graph.nodes == second.operation_graph.nodes
    assert first.operation_payloads == second.operation_payloads


@pytest.mark.parametrize(
    ("requirement_hash", "workload_hash", "release_hash", "message"),
    [
        ("e" * 64, DEFINITION_HASH, DEFINITION_HASH, "profile definition hash"),
        (DEFINITION_HASH, "e" * 64, DEFINITION_HASH, "profile definition hash"),
        (DEFINITION_HASH, DEFINITION_HASH, "e" * 64, "release definition hash"),
    ],
)
def test_rejects_mismatched_repository_hash_cross_references(
    tmp_path: Path,
    requirement_hash: str,
    workload_hash: str,
    release_hash: str,
    message: str,
) -> None:
    repository, commit, _ = _repository(
        tmp_path,
        1,
        requirement_hash=requirement_hash,
        workload_hash=workload_hash,
        release_hash=release_hash,
    )

    with pytest.raises(ValueError, match=message):
        _resolve(repository, commit, _observations(1))


def test_rejects_missing_profile_workload_reference(tmp_path: Path) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    root = repository.root
    profile = root / "config/cluster-profiles/inference.toml"
    profile.write_text(profile.read_text().replace('workloads = ["model"]', 'workloads = ["missing"]'))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "missing reference")
    commit = _git(root, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="profile workload reference"):
        _resolve(repository, commit, _observations(1))


def test_rejects_stale_observation_and_insufficient_capacity(tmp_path: Path) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    current = _observations(1)[0]

    with pytest.raises(ValueError, match="stale"):
        _resolve(
            repository,
            commit,
            (replace(current, observed_at=NOW - timedelta(minutes=6)),),
        )
    with pytest.raises(ValueError, match="insufficient eligible nodes"):
        _resolve(repository, commit, (replace(current, memory_available_bytes=99),))


@pytest.mark.parametrize(
    ("observation", "message"),
    [
        (replace(_observations(1)[0], agent_state="offline"), "connected"),
        (replace(_observations(1)[0], protocol_version=2), "protocol"),
        (
            replace(
                _observations(1)[0],
                capabilities=tuple(
                    item
                    for item in REQUIRED_CAPABILITIES
                    if item != "workload.verify"
                ),
            ),
            "capabilities",
        ),
    ],
)
def test_rejects_disconnected_or_incompatible_agents(
    tmp_path: Path, observation: DesiredStateObservation, message: str
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)

    with pytest.raises(ValueError, match=message):
        _resolve(repository, commit, (observation,))


def test_rejects_release_operations_outside_closed_agent_registry(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(
        tmp_path, 1, operations=REQUIRED_CAPABILITIES + ("agent.update",)
    )

    with pytest.raises(ValueError, match="release operations"):
        _resolve(repository, commit, _observations(1))


def test_agent_may_advertise_other_implemented_closed_registry_operations(
    tmp_path: Path,
) -> None:
    repository, commit, _ = _repository(tmp_path, 1)
    observation = replace(
        _observations(1)[0],
        capabilities=REQUIRED_CAPABILITIES + ("node.probe",),
    )

    assert _resolve(repository, commit, (observation,)).targets == (_node_id(0),)


def test_resolution_is_pinned_to_commit_not_mutable_checkout(tmp_path: Path) -> None:
    repository, commit, documents = _repository(tmp_path, 1)
    (repository.root / "inventory/fleet.toml").write_text("schema_version = 999\n")

    plan = _resolve(repository, commit, _observations(1))

    assert plan.input_digests["inventory/fleet.toml"] == hashlib.sha256(
        documents["inventory/fleet.toml"]
    ).hexdigest()


def test_durable_projection_joins_latest_health_with_agent_compatibility(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'observations.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    node_id = _node_id(0)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                protocol_version=1,
                capabilities=list(REQUIRED_CAPABILITIES),
                last_seen_at=NOW - timedelta(seconds=1),
            )
        )
        session.add_all(
            [
                Observation(
                    node_id=node_id,
                    kind="health",
                    payload={"status": "critical"},
                    observed_at=NOW - timedelta(minutes=1),
                ),
                Observation(
                    node_id=node_id,
                    kind="health",
                    payload={
                        "status": "healthy",
                        "memory_available_bytes": 1_000,
                        "disk_available_bytes": 2_000,
                        "occupied": False,
                    },
                    observed_at=NOW - timedelta(seconds=2),
                ),
            ]
        )

    assert durable_desired_state_observations(sessions) == (
        DesiredStateObservation(
            node_id=node_id,
            observed_at=NOW - timedelta(seconds=2),
            healthy=True,
            memory_available_bytes=1_000,
            disk_available_bytes=2_000,
            occupied=False,
            agent_state="active",
            protocol_version=1,
            capabilities=REQUIRED_CAPABILITIES,
        ),
    )
