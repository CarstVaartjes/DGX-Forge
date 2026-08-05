"""Repository-pinned desired state resolved into deterministic agent work."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from dgx_agent_protocol import AgentOperation, canonical_message
from jsonschema import ValidationError, validate
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from spark_profiles.fleet import Fleet, ManagementEndpoint, NodeId, NodeRecord
from spark_profiles.placement import (
    NodeObservation,
    PlacementPlanner,
    PlacementRequirement,
)

from .models import (
    AgentNode,
    AgentOperationAttempt,
    Job,
    Observation,
    Reconciliation,
)
from .models import AgentOperation as StoredOperation
from .orchestration import OperationGraph, OperationNode

if TYPE_CHECKING:
    from .reconcile import ReconciliationPlan
    from .repository import RepositoryService, TypedDocument

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SUPPORTED_PROTOCOL_RANGE = (1, 1)
_REQUIRED_CAPABILITIES = frozenset(
    {
        AgentOperation.RELEASE_INSTALL.value,
        AgentOperation.WORKLOAD_PREPARE.value,
        AgentOperation.WORKLOAD_START.value,
        AgentOperation.WORKLOAD_STOP.value,
        AgentOperation.WORKLOAD_HEALTH.value,
        AgentOperation.WORKLOAD_VERIFY.value,
    }
)
_IMPLEMENTED_CAPABILITIES = _REQUIRED_CAPABILITIES | {
    AgentOperation.NODE_PROBE.value
}
_SUPPORTED_ADAPTERS = frozenset({"spark-runtime-v1"})
_PLANNED_OPERATIONS = (
    AgentOperation.WORKLOAD_STOP.value,
    AgentOperation.RELEASE_INSTALL.value,
    AgentOperation.WORKLOAD_PREPARE.value,
    AgentOperation.WORKLOAD_START.value,
    AgentOperation.WORKLOAD_HEALTH.value,
    AgentOperation.WORKLOAD_VERIFY.value,
)


@dataclass(frozen=True)
class CurrentWorkloadState:
    """Accepted durable evidence for one currently active workload."""

    workload_id: str
    release_digest: str
    adapter_id: str
    managed: bool = True

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.workload_id) is None:
            raise ValueError("current workload ID is invalid")
        if _DIGEST.fullmatch(self.release_digest) is None:
            raise ValueError("current workload release digest is invalid")
        if self.adapter_id not in _SUPPORTED_ADAPTERS:
            raise ValueError("current workload adapter is not reviewed")
        if not isinstance(self.managed, bool):
            raise TypeError("current workload management state is invalid")


@dataclass(frozen=True)
class DesiredStateObservation:
    """Durable capacity and connected-agent evidence for one fleet node."""

    node_id: str
    observed_at: datetime
    healthy: bool
    memory_available_bytes: int
    disk_available_bytes: int
    occupied: bool
    agent_state: str
    protocol_version: int | None
    capabilities: tuple[str, ...]
    current_workloads: tuple[CurrentWorkloadState, ...] = ()
    memory_total_bytes: int | None = None
    disk_total_bytes: int | None = None

    def __post_init__(self) -> None:
        NodeId.parse(self.node_id)
        if not isinstance(self.observed_at, datetime):
            raise TypeError("observation timestamp is invalid")
        if not isinstance(self.healthy, bool) or not isinstance(self.occupied, bool):
            raise TypeError("observation health state is invalid")
        if (
            not isinstance(self.memory_available_bytes, int)
            or isinstance(self.memory_available_bytes, bool)
            or self.memory_available_bytes < 0
            or not isinstance(self.disk_available_bytes, int)
            or isinstance(self.disk_available_bytes, bool)
            or self.disk_available_bytes < 0
        ):
            raise ValueError("observation capacity is invalid")
        capabilities = tuple(self.capabilities)
        if len(capabilities) != len(set(capabilities)) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise ValueError("agent capabilities are invalid")
        object.__setattr__(self, "capabilities", capabilities)
        current_workloads = tuple(self.current_workloads)
        if len({item.workload_id for item in current_workloads}) != len(
            current_workloads
        ):
            raise ValueError("current workload evidence is duplicate")
        if self.occupied is not bool(current_workloads):
            raise ValueError("node occupancy does not match current workload evidence")
        object.__setattr__(self, "current_workloads", current_workloads)
        for total, available, field in (
            (self.memory_total_bytes, self.memory_available_bytes, "memory"),
            (self.disk_total_bytes, self.disk_available_bytes, "disk"),
        ):
            if total is not None and (
                not isinstance(total, int)
                or isinstance(total, bool)
                or total < available
            ):
                raise ValueError(f"node {field} total capacity is invalid")


class DesiredStateResolver:
    """Resolve V2 repository definitions without static-plan fallback."""

    def __init__(
        self,
        repository: RepositoryService,
        *,
        clock: Callable[[], datetime],
        observation_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if observation_ttl <= timedelta(0):
            raise ValueError("observation TTL must be positive")
        self._repository = repository
        self._clock = clock
        self._observation_ttl = observation_ttl

    def resolve(
        self,
        commit: str,
        profile_id: str,
        observations: Iterable[DesiredStateObservation],
    ) -> ReconciliationPlan:
        if not isinstance(profile_id, str) or _IDENTIFIER.fullmatch(profile_id) is None:
            raise ValueError("profile ID is invalid")
        documents: dict[str, TypedDocument] = {}

        def read(path: str) -> TypedDocument:
            document = self._repository.read_document(commit, path)
            documents[path] = document
            return document

        fleet_document = read("inventory/fleet.toml")
        topology_document = read("inventory/topology.json")
        profile_document = read(f"config/cluster-profiles/{profile_id}.toml")
        fleet_data = _mapping(fleet_document.parsed, "fleet")
        topology = _mapping(topology_document.parsed, "topology")
        profile = _mapping(profile_document.parsed, "cluster profile")
        _schema(fleet_data, "fleet.schema.json", "fleet")
        _schema(topology, "topology.schema.json", "topology")
        _schema(profile, "cluster-profile-v2.schema.json", "cluster profile")
        if profile.get("id") != profile_id:
            raise ValueError("cluster profile ID does not match its path")
        fleet = _fleet(fleet_data)
        topology = _canonical_topology(topology)

        evidence_path = _repository_path(
            profile["accepted_evidence"], prefix="inventory/reports/"
        )
        evidence = _mapping(read(evidence_path).parsed, "accepted evidence")
        if set(evidence) != {"accepted", "schema_version"} or evidence != {
            "accepted": True,
            "schema_version": 1,
        }:
            raise ValueError("profile accepted evidence is invalid")

        workload_ids = tuple(sorted(cast(Sequence[str], profile["workloads"])))
        if not all(_IDENTIFIER.fullmatch(item) for item in workload_ids):
            raise ValueError("profile workload reference is invalid")
        requirements = _requirements(profile, workload_ids)
        workloads: dict[str, Mapping[str, Any]] = {}
        releases: dict[str, Mapping[str, Any]] = {}
        for workload_id in workload_ids:
            workload_document = read(f"config/workloads/{workload_id}.toml")
            workload = _mapping(workload_document.parsed, "workload")
            _schema(workload, "workload-v2.schema.json", "workload")
            if workload.get("id") != workload_id:
                raise ValueError("profile workload reference does not match its path")
            if workload.get("adapter") not in _SUPPORTED_ADAPTERS:
                raise ValueError("workload adapter is not in the reviewed adapter registry")
            workload_hash = cast(str, workload["definition_hash"])
            if requirements[workload_id].definition_hash != workload_hash:
                raise ValueError("profile definition hash does not match workload")
            distributed_supported = workload["distributed_supported"]
            if (
                distributed_supported
                is not requirements[workload_id].model_supports_distributed
                or (
                    requirements[workload_id].node_count > 1
                    and distributed_supported is not True
                )
            ):
                raise ValueError(
                    "profile and workload distributed support do not match"
                )
            release_path = f"manifests/releases/{workload_id}.json"
            release_document = read(release_path)
            release = _release(
                _mapping(release_document.parsed, "release manifest"),
                workload_id=workload_id,
                definition_hash=workload_hash,
                adapter_id=cast(str, workload["adapter"]),
            )
            workloads[workload_id] = workload
            releases[workload_id] = MappingProxyType(
                {
                    "manifest_path": release_path,
                    "manifest_sha256": release_document.sha256,
                    "definition_hash": workload_hash,
                    "release_request": release["release_request"],
                    "workload_requests": release["workload_requests"],
                    "endpoint": release["endpoint"],
                }
            )
        _profile_cross_references(profile, workloads)

        observation_values = tuple(observations)
        placement_observations = _placement_observations(
            observation_values,
            fleet=fleet,
            now=self._clock(),
            ttl=self._observation_ttl,
            desired_workloads=frozenset(workload_ids),
            desired_releases=releases,
        )
        placements: dict[str, tuple[str, ...]] = {}
        available = placement_observations
        planner = PlacementPlanner()
        for workload_id in workload_ids:
            requirement = requirements[workload_id]
            placement = planner.plan(requirement, fleet, topology, available.values())
            placements[workload_id] = tuple(node.value for node in placement.nodes)
            if requirement.exclusive:
                available = dict(available)
                for node_id in placement.nodes:
                    available[node_id] = replace(available[node_id], occupied=True)

        routes = _routes(profile, placements, releases)
        operation_graph, payloads = _operations(
            commit,
            placements=placements,
            releases=releases,
            lifecycle=_mapping(profile["lifecycle"], "profile lifecycle"),
            observations={item.node_id: item for item in observation_values},
        )
        input_digests = {
            path: document.sha256 for path, document in sorted(documents.items())
        }
        from .reconcile import resolved_reconciliation_plan

        return resolved_reconciliation_plan(
            commit=commit,
            targets=operation_graph.targets,
            placements=placements,
            routes=routes,
            releases=releases,
            input_digests=input_digests,
            operation_graph=operation_graph,
            operation_payloads=payloads,
            agent_protocol_range=_SUPPORTED_PROTOCOL_RANGE,
        )


def durable_desired_state_observations(
    sessions: sessionmaker[Session],
) -> tuple[DesiredStateObservation, ...]:
    """Project latest durable health and agent state into resolver evidence."""

    with sessions() as session:
        agents = tuple(
            session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        )
        health = tuple(
            session.scalars(
                select(Observation)
                .where(Observation.kind == "health")
                .order_by(Observation.observed_at.desc(), Observation.id)
            )
        )
        current = _accepted_current_workloads(session)
    latest: dict[str, Observation] = {}
    for observation in health:
        latest.setdefault(observation.node_id, observation)
    projected: list[DesiredStateObservation] = []
    for agent in agents:
        observation = latest.get(agent.node_id)
        if observation is None:
            continue
        payload = _mapping(observation.payload, "health observation")
        memory = payload.get("memory_available_bytes")
        disk = payload.get("disk_available_bytes")
        memory_total = payload.get("memory_total_bytes")
        disk_total = payload.get("disk_total_bytes")
        if (
            not isinstance(memory, int)
            or isinstance(memory, bool)
            or not isinstance(disk, int)
            or isinstance(disk, bool)
            or (
                memory_total is not None
                and (not isinstance(memory_total, int) or isinstance(memory_total, bool))
            )
            or (
                disk_total is not None
                and (not isinstance(disk_total, int) or isinstance(disk_total, bool))
            )
        ):
            raise TypeError("health observation capacity is invalid")
        observed_at = _aware(observation.observed_at)
        state = agent.state
        if agent.last_seen_at is None:
            state = "offline"
        else:
            observed_at = min(observed_at, _aware(agent.last_seen_at))
        projected.append(
            DesiredStateObservation(
                node_id=agent.node_id,
                observed_at=observed_at,
                healthy=payload.get("status") in {"healthy", "warning"},
                memory_available_bytes=memory,
                disk_available_bytes=disk,
                occupied=bool(current.get(agent.node_id)),
                agent_state=state,
                protocol_version=agent.protocol_version,
                capabilities=tuple(sorted(agent.capabilities)),
                current_workloads=tuple(
                    sorted(
                        current.get(agent.node_id, {}).values(),
                        key=lambda item: item.workload_id,
                    )
                ),
                memory_total_bytes=cast(int | None, memory_total),
                disk_total_bytes=cast(int | None, disk_total),
            )
        )
    return tuple(projected)


def _accepted_current_workloads(
    session: Session,
) -> dict[str, dict[str, CurrentWorkloadState]]:
    """Replay accepted start/stop evidence from completed reconciliations."""

    all_reconciliations = tuple(
        session.scalars(
            select(Reconciliation)
            .order_by(Reconciliation.created_at, Reconciliation.id)
        )
    )
    jobs = tuple(session.scalars(select(Job).where(Job.kind == "reconcile")))
    operations = tuple(session.scalars(select(StoredOperation)))
    attempts = tuple(session.scalars(select(AgentOperationAttempt)))
    operations_by_job: dict[str, list[StoredOperation]] = {}
    for operation in operations:
        operations_by_job.setdefault(operation.parent_job_id, []).append(operation)
    attempts_by_operation = {
        (attempt.operation_id, attempt.attempt): attempt for attempt in attempts
    }
    reconciliations = tuple(
        item
        for item in all_reconciliations
        if item.status == "succeeded" and item.current_phase == "completed"
    )
    accepted_ids = {item.id for item in reconciliations}
    unaccepted_job_ids = {
        job.id
        for job in jobs
        if job.payload.get("reconciliation_id")
        in {item.id for item in all_reconciliations if item.id not in accepted_ids}
    }
    if any(
        operation.parent_job_id in unaccepted_job_ids
        and operation.state == "succeeded"
        and operation.kind
        in {
            AgentOperation.WORKLOAD_START.value,
            AgentOperation.WORKLOAD_STOP.value,
        }
        for operation in operations
    ):
        raise ValueError("unaccepted workload mutation makes current state uncertain")
    if not reconciliations:
        return {}
    current: dict[str, dict[str, CurrentWorkloadState]] = {}
    for reconciliation in reconciliations:
        matched_jobs = [
            job
            for job in jobs
            if job.payload.get("reconciliation_id") == reconciliation.id
        ]
        if len(matched_jobs) != 1 or matched_jobs[0].state != "succeeded":
            raise ValueError("completed reconciliation lacks exact operation evidence")
        job = matched_jobs[0]
        graph = _mapping(reconciliation.graph, "completed reconciliation graph")
        raw_nodes = graph.get("nodes")
        if not isinstance(raw_nodes, list):
            raise TypeError("completed reconciliation operation evidence is invalid")
        stored = list(operations_by_job.get(job.id, ()))
        if len(stored) != len(raw_nodes):
            raise ValueError("completed reconciliation lacks exact operation evidence")
        for raw_node in raw_nodes:
            node = _mapping(raw_node, "completed reconciliation operation")
            node_id = node.get("node_id")
            workload_id = node.get("workload_id")
            kind = node.get("kind")
            payload_digest = node.get("payload_digest")
            matches = [
                operation
                for operation in stored
                if operation.node_id == node_id
                and operation.kind == kind
                and operation.payload_digest == payload_digest
            ]
            if len(matches) != 1:
                raise ValueError("completed reconciliation lacks exact operation evidence")
            operation = matches[0]
            stored.remove(operation)
            try:
                exact_payload_digest = hashlib.sha256(
                    canonical_message(operation.payload)
                ).hexdigest()
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "completed reconciliation operation payload is invalid"
                ) from error
            attempt = attempts_by_operation.get(
                (operation.id, operation.current_attempt)
            )
            if (
                operation.base_commit != reconciliation.base_commit
                or exact_payload_digest != operation.payload_digest
                or operation.state != "succeeded"
                or attempt is None
                or attempt.state != "succeeded"
                or not isinstance(attempt.result, Mapping)
                or attempt.result.get("status") != "ok"
            ):
                raise ValueError("completed reconciliation operation evidence is invalid")
            if kind not in {
                AgentOperation.WORKLOAD_START.value,
                AgentOperation.WORKLOAD_STOP.value,
            }:
                continue
            payload = _mapping(operation.payload, "accepted workload payload")
            evidence = _mapping(
                attempt.result.get("evidence"), "accepted workload evidence"
            )
            action = cast(str, kind).removeprefix("workload.")
            expected_status = "started" if action == "start" else "stopped"
            if (
                node_id != operation.node_id
                or workload_id != payload.get("workload_id")
                or evidence.get("action") != action
                or evidence.get("status") != expected_status
                or evidence.get("workload_id") != workload_id
                or evidence.get("release_digest") != payload.get("release_digest")
                or not _valid_digest(evidence.get("evidence_digest"))
                or hashlib.sha256(canonical_message(payload)).hexdigest()
                != operation.payload_digest
            ):
                raise ValueError("completed reconciliation workload evidence is invalid")
            state = CurrentWorkloadState(
                cast(str, workload_id),
                cast(str, payload.get("release_digest")),
                cast(str, payload.get("adapter_id")),
            )
            node_state = current.setdefault(cast(str, node_id), {})
            existing = node_state.get(state.workload_id)
            if action == "start":
                if existing is not None:
                    raise ValueError("completed reconciliation workload drift is uncertain")
                node_state[state.workload_id] = state
            else:
                if existing != state:
                    raise ValueError("completed reconciliation workload drift is uncertain")
                del node_state[state.workload_id]
    return current


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field} must be an object")
    return cast(Mapping[str, Any], value)


def _schema(document: Mapping[str, Any], name: str, field: str) -> None:
    try:
        with resources.files("spark_profiles.schemas").joinpath(name).open(
            encoding="utf-8"
        ) as source:
            schema = json.load(source)
        validate(instance=document, schema=schema)
    except ValidationError as error:
        raise ValueError(f"{field} schema is invalid") from error


def _fleet(document: Mapping[str, Any]) -> Fleet:
    records: dict[NodeId, NodeRecord] = {}
    for node_id_text, raw_node in sorted(_mapping(document["nodes"], "fleet nodes").items()):
        node = _mapping(raw_node, "fleet node")
        management = _mapping(node["management"], "fleet management")
        node_id = NodeId.parse(node_id_text)
        records[node_id] = NodeRecord(
            node_id,
            cast(str, node["display_name"]),
            cast(str, node["hostname"]),
            ManagementEndpoint(
                cast(str, management["host"]),
                cast(str, management["user"]),
                cast(int, management["port"]),
                cast(str | None, management.get("credential_ref")),
            ),
            dict(_mapping(node["labels"], "fleet labels")),
            cast(Any, node["lifecycle"]),
        )
    return Fleet(2, records)


def _canonical_topology(document: Mapping[str, Any]) -> Mapping[str, object]:
    links = []
    for raw in cast(Sequence[Mapping[str, Any]], document["links"]):
        link = dict(raw)
        link["endpoints"] = sorted(
            (dict(item) for item in cast(Sequence[Mapping[str, object]], raw["endpoints"])),
            key=lambda item: (str(item["node_id"]), str(item["interface"])),
        )
        links.append(link)
    return {
        "schema_version": 1,
        "nodes": sorted(cast(Sequence[str], document["nodes"])),
        "links": sorted(links, key=lambda item: str(item["id"])),
    }


def _requirements(
    profile: Mapping[str, Any], workload_ids: tuple[str, ...]
) -> Mapping[str, PlacementRequirement]:
    requirements: dict[str, PlacementRequirement] = {}
    for raw in cast(Sequence[Mapping[str, Any]], profile["requirements"]):
        workload_id = cast(str, raw["workload"])
        if workload_id in requirements:
            raise ValueError("profile workload requirement is duplicate")
        requirements[workload_id] = PlacementRequirement(
            name=workload_id,
            definition_hash=cast(str, raw["definition_hash"]),
            node_count=cast(int, raw["node_count"]),
            required_labels=dict(
                _mapping(raw["required_labels"], "required labels")
            ),
            min_memory_bytes=cast(int, raw["min_memory_bytes"]),
            min_disk_bytes=cast(int, raw["min_disk_bytes"]),
            exclusive=cast(bool, raw["exclusive"]),
            distributed=cast(int, raw["node_count"]) > 1,
            model_supports_distributed=cast(bool, raw["distributed_supported"]),
            preferred_node_ids=tuple(cast(Sequence[str], raw.get("preferred_node_ids", ()))),
        )
    if set(requirements) != set(workload_ids):
        raise ValueError("profile workload reference lacks one exact requirement")
    return MappingProxyType(requirements)


def _repository_path(value: object, *, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or ".." in value:
        raise ValueError("repository reference is invalid")
    return value


def _release(
    document: Mapping[str, Any],
    *,
    workload_id: str,
    definition_hash: str,
    adapter_id: str,
) -> Mapping[str, Any]:
    if set(document) != {
        "schema_version",
        "workload_id",
        "definition_hash",
        "release_request",
        "workload_requests",
        "endpoint",
    } or document.get("schema_version") != 1:
        raise ValueError("release manifest schema is invalid")
    if document.get("workload_id") != workload_id:
        raise ValueError("release workload reference is invalid")
    if document.get("definition_hash") != definition_hash:
        raise ValueError("release definition hash does not match workload")
    release_request = _mapping(document["release_request"], "release request")
    if (
        set(release_request)
        != {
            "schema_version",
            "target_name",
            "oci_manifest_digest",
            "target_digest",
            "provenance_digest",
            "adapter_id",
        }
        or release_request.get("schema_version") != 1
        or not isinstance(release_request.get("target_name"), str)
        or _IDENTIFIER.fullmatch(cast(str, release_request["target_name"])) is None
        or not isinstance(release_request.get("oci_manifest_digest"), str)
        or _OCI_DIGEST.fullmatch(
            cast(str, release_request["oci_manifest_digest"])
        )
        is None
        or not isinstance(release_request.get("target_digest"), str)
        or _DIGEST.fullmatch(cast(str, release_request["target_digest"])) is None
        or not isinstance(release_request.get("provenance_digest"), str)
        or _DIGEST.fullmatch(cast(str, release_request["provenance_digest"])) is None
        or release_request.get("adapter_id") != adapter_id
    ):
        raise ValueError("release request is invalid")
    workload_requests = _mapping(
        document["workload_requests"], "workload requests"
    )
    expected_actions = {"prepare", "start", "stop", "health", "verify"}
    if set(workload_requests) != expected_actions:
        raise ValueError("release operations are outside the closed agent registry")
    parsed_requests: dict[str, Mapping[str, object]] = {}
    extras = {
        "prepare": "profile_digest",
        "start": "preparation_digest",
        "stop": None,
        "health": None,
        "verify": "expected_digest",
    }
    common = {
        "schema_version",
        "workload_id",
        "release_digest",
        "adapter_id",
    }
    for action, extra in extras.items():
        request = _mapping(workload_requests[action], f"workload {action} request")
        fields = common | ({extra} if extra is not None else set())
        if (
            set(request) != fields
            or request.get("schema_version") != 1
            or request.get("workload_id") != workload_id
            or request.get("adapter_id") != adapter_id
            or request.get("release_digest") != release_request["target_digest"]
            or (extra is not None and _valid_digest(request.get(extra)) is False)
        ):
            raise ValueError(f"workload {action} request is invalid")
        parsed_requests[action] = MappingProxyType(dict(request))
    endpoint = _mapping(document["endpoint"], "release endpoint")
    if (
        set(endpoint) != {"scheme", "port", "path"}
        or endpoint.get("scheme") not in {"http", "https"}
        or not isinstance(endpoint.get("port"), int)
        or isinstance(endpoint.get("port"), bool)
        or not 1 <= cast(int, endpoint["port"]) <= 65535
        or not isinstance(endpoint.get("path"), str)
        or not cast(str, endpoint["path"]).startswith("/")
    ):
        raise ValueError("release endpoint schema is invalid")
    return MappingProxyType(
        {
            **document,
            "release_request": MappingProxyType(dict(release_request)),
            "workload_requests": MappingProxyType(parsed_requests),
            "endpoint": MappingProxyType(dict(endpoint)),
        }
    )


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _profile_cross_references(
    profile: Mapping[str, Any], workloads: Mapping[str, Mapping[str, Any]]
) -> None:
    endpoints = _mapping(profile["endpoints"], "profile endpoints")
    quotas = _mapping(profile["quotas"], "profile quotas")
    if not all(
        _IDENTIFIER.fullmatch(alias) and workload_id in workloads
        for alias, workload_id in endpoints.items()
    ):
        raise ValueError("profile route reference is invalid")
    if set(quotas) != set(endpoints):
        raise ValueError("profile quota reference is invalid")
    selected = set(workloads)
    for workload_id, workload in workloads.items():
        conflicts = set(cast(Sequence[str], workload["conflicts"]))
        if conflicts & (selected - {workload_id}):
            raise ValueError("profile selects conflicting workloads")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _placement_observations(
    observations: Iterable[DesiredStateObservation],
    *,
    fleet: Fleet,
    now: datetime,
    ttl: timedelta,
    desired_workloads: frozenset[str],
    desired_releases: Mapping[str, Mapping[str, Any]],
) -> dict[NodeId, NodeObservation]:
    now = _aware(now)
    resolved: dict[NodeId, NodeObservation] = {}
    for observation in observations:
        node_id = NodeId.parse(observation.node_id)
        if node_id in resolved:
            raise ValueError("duplicate durable observation")
        age = now - _aware(observation.observed_at)
        if age < timedelta(0) or age > ttl:
            raise ValueError("node observation is stale")
        if observation.agent_state != "active":
            raise ValueError("node does not have a connected active agent")
        if observation.protocol_version != _SUPPORTED_PROTOCOL_RANGE[0]:
            raise ValueError("agent protocol version is incompatible")
        capabilities = set(observation.capabilities)
        if not _REQUIRED_CAPABILITIES <= capabilities <= _IMPLEMENTED_CAPABILITIES:
            raise ValueError("agent capabilities are incompatible")
        managed = bool(observation.current_workloads) and all(
            item.managed for item in observation.current_workloads
        )
        reclaimable = managed and any(
            item.workload_id not in desired_workloads
            or item.release_digest
            != cast(
                Mapping[str, object],
                desired_releases[item.workload_id]["release_request"],
            )["target_digest"]
            or item.adapter_id
            != cast(
                Mapping[str, object],
                desired_releases[item.workload_id]["release_request"],
            )["adapter_id"]
            for item in observation.current_workloads
        )
        retained = managed and not reclaimable
        if (reclaimable or retained) and (
            observation.memory_total_bytes is None
            or observation.disk_total_bytes is None
        ):
            raise ValueError("reclaimable node total capacity is unavailable")
        resolved[node_id] = NodeObservation(
            node_id,
            observation.healthy,
            cast(int, observation.memory_total_bytes)
            if managed
            else observation.memory_available_bytes,
            cast(int, observation.disk_total_bytes)
            if managed
            else observation.disk_available_bytes,
            observation.occupied and not managed,
        )
    if set(resolved) != set(fleet.nodes):
        raise ValueError("durable observations must exactly cover the fleet")
    return resolved


def _routes(
    profile: Mapping[str, Any],
    placements: Mapping[str, tuple[str, ...]],
    releases: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for alias, workload_id in sorted(
        _mapping(profile["endpoints"], "profile endpoints").items()
    ):
        endpoint = cast(Mapping[str, object], releases[workload_id]["endpoint"])
        quota = _mapping(
            _mapping(profile["quotas"], "profile quotas")[alias], "profile quota"
        )
        result[alias] = MappingProxyType(
            {
                "workload_id": workload_id,
                "nodes": placements[workload_id],
                "entrypoint_node_id": placements[workload_id][0],
                "scheme": endpoint["scheme"],
                "port": endpoint["port"],
                "path": endpoint["path"],
                "quota": MappingProxyType(dict(quota)),
                "quota_digest": hashlib.sha256(
                    canonical_message(quota)
                ).hexdigest(),
            }
        )
    return MappingProxyType(result)


def _payload_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_message(payload)).hexdigest()


def _operations(
    commit: str,
    *,
    placements: Mapping[str, tuple[str, ...]],
    releases: Mapping[str, Mapping[str, Any]],
    lifecycle: Mapping[str, Any],
    observations: Mapping[str, DesiredStateObservation],
) -> tuple[OperationGraph, Mapping[str, Mapping[str, object]]]:
    nodes: dict[str, OperationNode] = {}
    payloads: dict[str, Mapping[str, object]] = {}
    current = {
        (item.workload_id, observation.node_id): item
        for observation in observations.values()
        for item in observation.current_workloads
        if item.managed
    }
    desired_exact: set[tuple[str, str]] = set()
    for workload_id, targets in placements.items():
        release = cast(Mapping[str, object], releases[workload_id]["release_request"])
        for node_id in targets:
            state = current.get((workload_id, node_id))
            if state is not None and (
                state.release_digest == release["target_digest"]
                and state.adapter_id == release["adapter_id"]
            ):
                desired_exact.add((workload_id, node_id))
    teardown = {
        key: state for key, state in current.items() if key not in desired_exact
    }
    stop_ids = {
        key: f"{key[0]}:{key[1]}:{AgentOperation.WORKLOAD_STOP.value}"
        for key in teardown
    }
    for workload_id in sorted({key[0] for key in teardown}):
        old_nodes = sorted(node_id for old_id, node_id in teardown if old_id == workload_id)
        head = old_nodes[0]
        for node_id in old_nodes:
            state = teardown[(workload_id, node_id)]
            operation_id = stop_ids[(workload_id, node_id)]
            dependencies = ()
            if node_id != head and lifecycle["stop_order"] == "entrypoint-before-workers":
                dependencies = (stop_ids[(workload_id, head)],)
            payload: Mapping[str, object] = MappingProxyType(
                {
                    "schema_version": 1,
                    "workload_id": state.workload_id,
                    "release_digest": state.release_digest,
                    "adapter_id": state.adapter_id,
                }
            )
            payloads[operation_id] = payload
            nodes[operation_id] = OperationNode(
                operation_id,
                node_id,
                workload_id,
                AgentOperation.WORKLOAD_STOP.value,
                dependencies,
                None,
                _payload_digest(payload),
            )
    all_stops = tuple(sorted(stop_ids.values()))
    for workload_id, targets in sorted(placements.items()):
        deploy_targets = tuple(
            node_id for node_id in targets if (workload_id, node_id) not in desired_exact
        )
        operation_ids: dict[tuple[str, str], str] = {}
        for node_id in deploy_targets:
            for kind in _PLANNED_OPERATIONS[1:]:
                operation_ids[(node_id, kind)] = f"{workload_id}:{node_id}:{kind}"
        worker_starts = tuple(
            operation_ids[(node_id, AgentOperation.WORKLOAD_START.value)]
            for node_id in deploy_targets
            if node_id != targets[0]
        )
        for node_id in deploy_targets:
            for kind in _PLANNED_OPERATIONS[1:]:
                operation_id = operation_ids[(node_id, kind)]
                if kind == AgentOperation.RELEASE_INSTALL.value:
                    dependencies = all_stops
                elif kind == AgentOperation.WORKLOAD_PREPARE.value:
                    dependencies = (
                        operation_ids[(node_id, AgentOperation.RELEASE_INSTALL.value)],
                    )
                elif kind == AgentOperation.WORKLOAD_START.value:
                    dependencies = (
                        operation_ids[(node_id, AgentOperation.WORKLOAD_PREPARE.value)],
                    )
                    if (
                        node_id == targets[0]
                        and lifecycle["start_order"] == "workers-before-entrypoint"
                    ):
                        dependencies += worker_starts
                elif kind == AgentOperation.WORKLOAD_HEALTH.value:
                    dependencies = (
                        operation_ids[(node_id, AgentOperation.WORKLOAD_START.value)],
                    )
                else:
                    dependencies = (
                        operation_ids[(node_id, AgentOperation.WORKLOAD_HEALTH.value)],
                    )
                if kind == AgentOperation.RELEASE_INSTALL.value:
                    payload = cast(
                        Mapping[str, object],
                        releases[workload_id]["release_request"],
                    )
                else:
                    requests = cast(
                        Mapping[str, Mapping[str, object]],
                        releases[workload_id]["workload_requests"],
                    )
                    payload = requests[kind.removeprefix("workload.")]
                payloads[operation_id] = MappingProxyType(dict(payload))
                nodes[operation_id] = OperationNode(
                    operation_id=operation_id,
                    node_id=node_id,
                    workload_id=workload_id,
                    kind=kind,
                    dependencies=tuple(sorted(set(dependencies))),
                    compensation_kind=(
                        AgentOperation.WORKLOAD_STOP.value
                        if kind == AgentOperation.WORKLOAD_START.value
                        else None
                    ),
                    payload_digest=_payload_digest(payload),
                )
    ordered = _topological(nodes)
    targets = tuple(
        sorted(
            {node.node_id for node in ordered}
            | {
                node_id
                for workload_targets in placements.values()
                for node_id in workload_targets
            }
        )
    )
    graph_document = {
        "schema_version": 1,
        "base_commit": commit,
        "targets": list(targets),
        "nodes": [node.to_document() for node in ordered],
    }
    digest = hashlib.sha256(
        json.dumps(graph_document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        OperationGraph(f"pending:{digest}", commit, targets, ordered, digest),
        MappingProxyType(dict(sorted(payloads.items()))),
    )


def _topological(nodes: Mapping[str, OperationNode]) -> tuple[OperationNode, ...]:
    unresolved = {
        operation_id: set(node.dependencies) for operation_id, node in nodes.items()
    }
    ordered: list[OperationNode] = []
    while unresolved:
        ready = sorted(
            operation_id
            for operation_id, dependencies in unresolved.items()
            if not dependencies
        )
        if not ready:
            raise ValueError("resolved operation graph contains a cycle")
        for operation_id in ready:
            ordered.append(nodes[operation_id])
            del unresolved[operation_id]
        for dependencies in unresolved.values():
            dependencies.difference_update(ready)
    return tuple(ordered)
