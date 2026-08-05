"""Persisted, deterministic operation graphs for agent reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dgx_agent_protocol import AgentOperation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .logging import redact_text
from .models import Reconciliation, ReconciliationCompletionGeneration

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_OPERATION_ID = re.compile(r"[a-z0-9](?:[a-z0-9._:-]{0,126}[a-z0-9])?\Z")
_WORKLOAD_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_IMPLEMENTED_OPERATIONS = frozenset(
    {
        AgentOperation.NODE_PROBE.value,
        AgentOperation.RELEASE_INSTALL.value,
        AgentOperation.WORKLOAD_PREPARE.value,
        AgentOperation.WORKLOAD_START.value,
        AgentOperation.WORKLOAD_STOP.value,
        AgentOperation.WORKLOAD_HEALTH.value,
        AgentOperation.WORKLOAD_VERIFY.value,
    }
)
_PHASE_TRANSITIONS = {
    "planned": "routes-withdrawn",
    "routes-withdrawn": "dispatching",
    "dispatching": "accepting",
    "accepting": "completed",
}
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class OperationNode:
    """One immutable, dependency-fenced operation in a reconciliation graph."""

    operation_id: str
    node_id: str
    workload_id: str
    kind: str
    dependencies: tuple[str, ...]
    compensation_kind: str | None
    payload_digest: str

    def to_document(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "node_id": self.node_id,
            "workload_id": self.workload_id,
            "kind": self.kind,
            "dependencies": list(self.dependencies),
            "compensation_kind": self.compensation_kind,
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class OperationGraph:
    """Canonical topological graph persisted independently of input ordering."""

    reconciliation_id: str
    base_commit: str
    targets: tuple[str, ...]
    nodes: tuple[OperationNode, ...]
    digest: str

    @property
    def document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "base_commit": self.base_commit,
            "targets": list(self.targets),
            "nodes": [node.to_document() for node in self.nodes],
        }

    def dependencies(self, operation_id: str) -> tuple[str, ...]:
        for node in self.nodes:
            if node.operation_id == operation_id:
                return node.dependencies
        raise KeyError(operation_id)


class ReconciliationOrchestrator:
    """Validate and persist operation graphs without executing node work."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def plan(self, document: Mapping[str, Any]) -> OperationGraph:
        base_commit, targets, generation, nodes = _parse_plan(document)
        graph_document = _graph_document(base_commit, targets, nodes)
        digest = _digest(graph_document)
        graph = OperationGraph(
            reconciliation_id=str(uuid.uuid4()),
            base_commit=base_commit,
            targets=targets,
            nodes=nodes,
            digest=digest,
        )
        stored = Reconciliation(
            id=graph.reconciliation_id,
            base_commit=base_commit,
            status="planned",
            summary={
                "operation_count": len(nodes),
                "target_count": len(targets),
            },
            graph=graph_document,
            graph_digest=digest,
            current_phase="planned",
            route_withdrawal_generation=generation,
            terminal_reason=None,
            created_at=self._clock(),
        )
        with self._sessions.begin() as session:
            session.add(stored)
        return graph

    def advance(
        self,
        reconciliation_id: str,
        phase: str,
        *,
        route_withdrawal_generation: int | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            stored = self._stored(session, reconciliation_id)
            if stored.status in _TERMINAL_STATUSES:
                raise ValueError("reconciliation is terminal")
            if _PHASE_TRANSITIONS.get(stored.current_phase) != phase:
                raise ValueError("reconciliation phase transition is invalid")
            if route_withdrawal_generation is not None:
                if phase != "routes-withdrawn":
                    raise ValueError("route generation is invalid for this transition")
                _generation(route_withdrawal_generation)
                if route_withdrawal_generation < stored.route_withdrawal_generation:
                    raise ValueError("route generation must not decrease")
                stored.route_withdrawal_generation = route_withdrawal_generation
            stored.current_phase = phase
            if phase == "completed":
                stored.completion_generation = self._next_completion_generation(
                    session
                )
                stored.status = "succeeded"
            else:
                stored.status = "running"

    def store_resolved_plan(
        self,
        graph: OperationGraph,
        plan_digest: str,
        document: Mapping[str, object],
    ) -> None:
        """Attach the complete immutable plan to its accepted graph row."""

        stored_document = _resolved_document(plan_digest, document)
        with self._sessions.begin() as session:
            stored = self._stored(session, graph.reconciliation_id)
            if stored.graph_digest != graph.digest or stored.graph != graph.document:
                raise ValueError("resolved plan graph does not match persisted graph")
            if stored.plan_digest is not None:
                if (
                    stored.plan_digest != plan_digest
                    or stored.resolved_plan != stored_document
                ):
                    raise ValueError("reconciliation already has a different plan")
                return
            stored.plan_digest = plan_digest
            stored.resolved_plan = stored_document

    def get_or_create_resolved_plan(
        self,
        graph_plan: Mapping[str, Any],
        plan_digest: str,
        document: Mapping[str, object],
    ) -> OperationGraph:
        """Atomically persist or return the exact plan identified by its digest."""

        base_commit, targets, generation, nodes = _parse_plan(graph_plan)
        graph_document = _graph_document(base_commit, targets, nodes)
        graph_digest = _digest(graph_document)
        stored_document = _resolved_document(plan_digest, document)
        if stored_document.get("operation_graph") != graph_document:
            raise ValueError("resolved plan graph does not match operation graph")

        candidate = Reconciliation(
            id=str(uuid.uuid4()),
            base_commit=base_commit,
            status="planned",
            summary={
                "operation_count": len(nodes),
                "target_count": len(targets),
            },
            graph=graph_document,
            graph_digest=graph_digest,
            plan_digest=plan_digest,
            resolved_plan=stored_document,
            current_phase="planned",
            route_withdrawal_generation=generation,
            terminal_reason=None,
            created_at=self._clock(),
        )
        with self._sessions.begin() as session:
            try:
                with session.begin_nested():
                    session.add(candidate)
                    session.flush()
                accepted = candidate
            except IntegrityError:
                accepted = session.scalar(
                    select(Reconciliation).where(
                        Reconciliation.plan_digest == plan_digest
                    )
                )
                if accepted is None:
                    raise
            if (
                accepted.base_commit != base_commit
                or accepted.graph != graph_document
                or accepted.graph_digest != graph_digest
                or accepted.route_withdrawal_generation != generation
                or accepted.resolved_plan != stored_document
            ):
                raise ValueError("plan digest identifies different persisted content")
            return OperationGraph(
                accepted.id,
                accepted.base_commit,
                targets,
                nodes,
                accepted.graph_digest,
            )

    def resolved_plan(
        self, plan_digest: str
    ) -> tuple[OperationGraph, Mapping[str, object]] | None:
        """Load and revalidate a complete plan after a process restart."""

        if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
            return None
        with self._sessions() as session:
            stored = session.scalar(
                select(Reconciliation).where(
                    Reconciliation.plan_digest == plan_digest
                )
            )
            if stored is None or stored.resolved_plan is None:
                return None
            document = json.loads(
                json.dumps(stored.resolved_plan, sort_keys=True, separators=(",", ":"))
            )
            encoded = json.dumps(
                document, sort_keys=True, separators=(",", ":")
            ).encode()
            if hashlib.sha256(encoded).hexdigest() != plan_digest:
                raise ValueError("persisted resolved plan digest is invalid")
            graph_document = stored.graph
            if not isinstance(graph_document, Mapping):
                raise TypeError("persisted reconciliation graph is invalid")
            _, targets, _, nodes = _parse_plan(
                {
                    "base_commit": graph_document.get("base_commit"),
                    "targets": graph_document.get("targets"),
                    "route_withdrawal_generation": stored.route_withdrawal_generation,
                    "operations": graph_document.get("nodes"),
                }
            )
            expected_graph = _graph_document(stored.base_commit, targets, nodes)
            if (
                expected_graph != graph_document
                or _digest(expected_graph) != stored.graph_digest
            ):
                raise ValueError("persisted reconciliation graph digest is invalid")
            graph = OperationGraph(
                stored.id,
                stored.base_commit,
                targets,
                nodes,
                stored.graph_digest,
            )
            return graph, document

    def cancel(self, reconciliation_id: str, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancellation reason is required")
        safe_reason = redact_text(reason.strip())[:1024]
        with self._sessions.begin() as session:
            stored = self._stored(session, reconciliation_id)
            if stored.status in _TERMINAL_STATUSES:
                raise ValueError("reconciliation is terminal")
            stored.status = "cancelled"
            stored.current_phase = "cancelled"
            stored.terminal_reason = safe_reason

    @staticmethod
    def _stored(session: Session, reconciliation_id: str) -> Reconciliation:
        if not isinstance(reconciliation_id, str):
            raise KeyError("unknown reconciliation")
        stored = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == reconciliation_id)
            .with_for_update(of=Reconciliation)
        )
        if stored is None:
            raise KeyError("unknown reconciliation")
        return stored

    @staticmethod
    def _next_completion_generation(session: Session) -> int:
        statement = (
            select(ReconciliationCompletionGeneration)
            .where(ReconciliationCompletionGeneration.singleton_id == 1)
            .with_for_update()
        )
        counter = session.scalar(statement)
        if counter is None:
            try:
                with session.begin_nested():
                    session.add(ReconciliationCompletionGeneration(
                        singleton_id=1,
                        last_generation=0,
                    ))
                    session.flush()
            except IntegrityError:
                pass
            counter = session.scalar(statement)
        if counter is None or not 0 <= counter.last_generation < 2**63 - 1:
            raise RuntimeError("reconciliation completion generation is unavailable")
        counter.last_generation += 1
        return counter.last_generation


def _resolved_document(
    plan_digest: str, document: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
        raise ValueError("resolved plan digest is invalid")
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 1_048_576:
        raise ValueError("resolved reconciliation plan is too large")
    if hashlib.sha256(encoded).hexdigest() != plan_digest:
        raise ValueError("resolved plan content does not match its digest")
    return json.loads(encoded)


def _parse_plan(
    document: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], int, tuple[OperationNode, ...]]:
    if not isinstance(document, Mapping) or set(document) != {
        "base_commit",
        "targets",
        "route_withdrawal_generation",
        "operations",
    }:
        raise ValueError("reconciliation plan fields are invalid")
    base_commit = document["base_commit"]
    if not isinstance(base_commit, str) or _COMMIT.fullmatch(base_commit) is None:
        raise ValueError("base commit is invalid")
    targets = _targets(document["targets"])
    generation = _generation(document["route_withdrawal_generation"])
    raw_operations = document["operations"]
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ValueError("reconciliation operations are required")
    parsed = [_operation(raw) for raw in raw_operations]
    by_id: dict[str, OperationNode] = {}
    for node in parsed:
        if node.operation_id in by_id:
            raise ValueError("reconciliation operation ID is duplicate")
        by_id[node.operation_id] = node
        if node.node_id not in targets:
            raise ValueError("reconciliation operation target is unknown")
    for node in parsed:
        for dependency in node.dependencies:
            required = by_id.get(dependency)
            if required is None:
                raise ValueError("reconciliation operation dependency is unknown")
            if required.workload_id != node.workload_id:
                raise ValueError("cross-workload dependency is invalid")
    return base_commit, targets, generation, _topological(by_id)


def _targets(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("reconciliation targets are required")
    if not all(isinstance(item, str) and _NODE_ID.fullmatch(item) for item in value):
        raise ValueError("reconciliation target is invalid")
    targets = tuple(sorted(value))
    if len(targets) != len(set(targets)):
        raise ValueError("reconciliation target is duplicate")
    return targets


def _generation(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("route withdrawal generation is invalid")
    return value


def _operation(raw: Any) -> OperationNode:
    fields = {
        "operation_id",
        "node_id",
        "workload_id",
        "kind",
        "dependencies",
        "compensation_kind",
        "payload_digest",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ValueError("reconciliation operation fields are invalid")
    operation_id = raw["operation_id"]
    node_id = raw["node_id"]
    workload_id = raw["workload_id"]
    kind = raw["kind"]
    dependencies = raw["dependencies"]
    compensation = raw["compensation_kind"]
    payload_digest = raw["payload_digest"]
    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise ValueError("reconciliation operation ID is invalid")
    if not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None:
        raise ValueError("reconciliation operation target is invalid")
    if not isinstance(workload_id, str) or _WORKLOAD_ID.fullmatch(workload_id) is None:
        raise ValueError("reconciliation workload ID is invalid")
    if not isinstance(kind, str) or kind not in _IMPLEMENTED_OPERATIONS:
        raise ValueError("reconciliation operation kind is absent from the agent registry")
    if compensation is not None and (
        not isinstance(compensation, str) or compensation not in _IMPLEMENTED_OPERATIONS
    ):
        raise ValueError("reconciliation compensation kind is absent from the agent registry")
    if not isinstance(payload_digest, str) or _DIGEST.fullmatch(payload_digest) is None:
        raise ValueError("reconciliation payload digest is invalid")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and _OPERATION_ID.fullmatch(item) for item in dependencies
    ):
        raise ValueError("reconciliation operation dependencies are invalid")
    ordered_dependencies = tuple(sorted(dependencies))
    if len(ordered_dependencies) != len(set(ordered_dependencies)):
        raise ValueError("reconciliation operation dependency is duplicate")
    return OperationNode(
        operation_id=operation_id,
        node_id=node_id,
        workload_id=workload_id,
        kind=kind,
        dependencies=ordered_dependencies,
        compensation_kind=compensation,
        payload_digest=payload_digest,
    )


def _topological(by_id: Mapping[str, OperationNode]) -> tuple[OperationNode, ...]:
    unresolved = {
        operation_id: set(node.dependencies) for operation_id, node in by_id.items()
    }
    ordered: list[OperationNode] = []
    while unresolved:
        ready = sorted(
            operation_id
            for operation_id, dependencies in unresolved.items()
            if not dependencies
        )
        if not ready:
            raise ValueError("reconciliation operation graph contains a cycle")
        for operation_id in ready:
            ordered.append(by_id[operation_id])
            del unresolved[operation_id]
        for dependencies in unresolved.values():
            dependencies.difference_update(ready)
    return tuple(ordered)


def _graph_document(
    base_commit: str,
    targets: tuple[str, ...],
    nodes: tuple[OperationNode, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "base_commit": base_commit,
        "targets": list(targets),
        "nodes": [node.to_document() for node in nodes],
    }


def _digest(document: Mapping[str, object]) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
