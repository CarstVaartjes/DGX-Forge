from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from dgx_agent.package_operations import OperationBinding, PackageDisposition
from dgx_agent.packages.adapter import AdapterEvidence, AdapterOperation
from dgx_agent.packages.engine import (
    PackageCancelled,
    PackageEngine,
    PackageEngineError,
)
from dgx_agent.packages.materialize import MaterializedGeneration
from dgx_agent.packages.state import PackageState
from dgx_agent.packages.store import StoreObject
from dgx_agent_protocol import AgentOperation, PackageOperationRequest

RELEASE_A = "a" * 64
RELEASE_B = "b" * 64
DEPLOYMENT = "c" * 64


@dataclass(frozen=True)
class Component:
    name: str
    digest: str
    size: int = 8
    kind: str = "model"


@dataclass(frozen=True)
class Lock:
    digest: str
    components: tuple[Component, ...]
    adapter: Component
    compatibility: dict[str, object]


class Trust:
    def __init__(self, locks: dict[str, Lock]) -> None:
        self.locks = locks
        self.calls: list[str] = []

    def refresh(self) -> None:
        self.calls.append("refresh")

    def trusted_lock(self, digest: str) -> Lock:
        self.calls.append(digest)
        return self.locks[digest]


class Acquisition:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def fetch(self, descriptor, binding, progress, cancelled, *, deadline=None):
        del binding, deadline
        if cancelled():
            raise PackageCancelled("cancelled")
        self.events.append(f"fetch:{descriptor.name}")
        progress({"phase": "fetch", "component": descriptor.name})
        return StoreObject(
            descriptor.digest.removeprefix("sha256:"),
            descriptor.size,
            descriptor.kind,
            f"objects/sha256/{descriptor.digest.removeprefix('sha256:')}",
        )


class Materializer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def materialize(self, lock, objects, staging: Path):
        self.events.append("materialize")
        generation = staging / lock.digest
        generation.mkdir(parents=True, exist_ok=True)
        generation.chmod(0o555)
        digests = tuple(sorted(objects))
        return MaterializedGeneration(
            release_digest=lock.digest,
            root_object_digest="d" * 64,
            object_digests=digests,
            environment_digest=None,
        )


class Adapter:
    def __init__(
        self,
        generation: str,
        release_digest: str,
        events: list[str],
        *,
        after=None,
        fail_health: bool = False,
    ) -> None:
        self.generation = generation
        self.release_digest = release_digest
        self.events = events
        self.after = after or (lambda _operation: None)
        self.fail_health = fail_health

    def execute(self, operation, invocation, deadline):
        del deadline
        self.events.append(operation.value)
        if operation is AdapterOperation.HEALTH and self.fail_health:
            raise RuntimeError("unhealthy")
        self.after(operation)
        return AdapterEvidence(
            operation=operation,
            status="ok",
            release_digest=self.release_digest,
            generation=self.generation,
            fence=invocation.fence,
            evidence_digest="e" * 64,
        )


class AdapterFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.after = None
        self.fail_health_for: str | None = None
        self.requests = []

    def __call__(self, lock, generation_id, generation_path, objects, *, request=None):
        del generation_path, objects
        self.requests.append(request)
        return Adapter(
            generation_id,
            lock.digest,
            self.events,
            after=self.after,
            fail_health=self.fail_health_for == lock.digest,
        )


def _binding(index: int) -> OperationBinding:
    return OperationBinding(
        job_id=f"10000000-0000-4000-8000-{index:012d}",
        operation_id=f"20000000-0000-4000-8000-{index:012d}",
        attempt=1,
        fence=f"30000000-0000-4000-8000-{index:012d}",
        node_id="spk_" + f"{index:032x}",
    )


def _request(operation: AgentOperation, release: str) -> PackageOperationRequest:
    return PackageOperationRequest(
        operation=operation,
        schema_version=1,
        deployment_id="future-stack",
        release_digest=release,
        deployment_digest=DEPLOYMENT,
    )


def _engine(tmp_path: Path, *, cancelled=lambda _binding: False, crash_hook=None):
    events: list[str] = []
    locks = {
        RELEASE_A: Lock(
            RELEASE_A,
            (Component("weights", f"sha256:{'1' * 64}"),),
            Component("adapter", f"sha256:{'2' * 64}", kind="adapter"),
            {},
        ),
        RELEASE_B: Lock(
            RELEASE_B,
            (Component("weights", f"sha256:{'3' * 64}"),),
            Component("adapter", f"sha256:{'4' * 64}", kind="adapter"),
            {},
        ),
    }
    trust = Trust(locks)
    adapters = AdapterFactory(events)
    state = PackageState(tmp_path / "state")
    engine = PackageEngine(
        state=state,
        trust=trust,
        acquisition=Acquisition(events),
        materializer=Materializer(events),
        generation_root=tmp_path / "generations",
        pointer_root=tmp_path / "pointers",
        adapter_factory=adapters,
        preflight=lambda lock, request, binding: events.append("preflight"),
        progress=lambda _binding, _value: None,
        cancelled=cancelled,
        crash_hook=crash_hook,
    )
    return engine, state, trust, adapters, events


def test_prepare_activate_update_and_offline_rollback_preserve_predecessor(
    tmp_path: Path,
) -> None:
    engine, state, trust, _adapters, events = _engine(tmp_path)

    prepared_a = engine.execute(
        _request(AgentOperation.PACKAGE_PREPARE, RELEASE_A), _binding(1), None
    )
    activated_a = engine.execute(
        _request(AgentOperation.PACKAGE_ACTIVATE, RELEASE_A), _binding(2), None
    )
    engine.execute(
        _request(AgentOperation.PACKAGE_PREPARE, RELEASE_B), _binding(3), None
    )
    activated_b = engine.execute(
        _request(AgentOperation.PACKAGE_ACTIVATE, RELEASE_B), _binding(4), None
    )
    trust_calls_before_rollback = tuple(trust.calls)
    rolled_back = engine.execute(
        _request(AgentOperation.PACKAGE_ROLLBACK, RELEASE_A), _binding(5), None
    )

    assert prepared_a.status == "validated"
    assert activated_a.status == "active"
    assert activated_b.status == "active"
    assert rolled_back.status == "active"
    assert rolled_back.release_digest == RELEASE_A
    assert trust.calls == list(trust_calls_before_rollback)
    assert state.active_generation("future-stack").release_digest == RELEASE_A
    assert state.generation_for_release("future-stack", RELEASE_B).state == "retained"
    assert events[:6] == [
        "preflight",
        "fetch:weights",
        "fetch:adapter",
        "materialize",
        "prepare",
        "verify",
    ]


def test_package_engine_passes_operation_request_to_adapter_factory(tmp_path: Path) -> None:
    engine, _state, _trust, adapters, _events = _engine(tmp_path)
    request = _request(AgentOperation.PACKAGE_PREPARE, RELEASE_A)

    engine.execute(request, _binding(20), None)

    assert adapters.requests == [request]


def test_cancellation_before_activation_is_safe_and_after_selection_rolls_back(
    tmp_path: Path,
) -> None:
    cancelled = {"value": True}
    engine, state, _trust, adapters, _events = _engine(
        tmp_path, cancelled=lambda _binding: cancelled["value"]
    )

    with pytest.raises(PackageCancelled):
        engine.execute(
            _request(AgentOperation.PACKAGE_PREPARE, RELEASE_A), _binding(1), None
        )
    assert state.active_generation("future-stack") is None

    cancelled["value"] = False
    engine.execute(
        _request(AgentOperation.PACKAGE_PREPARE, RELEASE_A), _binding(2), None
    )
    engine.execute(
        _request(AgentOperation.PACKAGE_ACTIVATE, RELEASE_A), _binding(3), None
    )
    engine.execute(
        _request(AgentOperation.PACKAGE_PREPARE, RELEASE_B), _binding(4), None
    )
    adapters.after = lambda operation: (
        cancelled.update(value=True) if operation is AdapterOperation.START else None
    )

    result = engine.execute(
        _request(AgentOperation.PACKAGE_ACTIVATE, RELEASE_B), _binding(5), None
    )

    assert result.status == "rolled-back"
    assert state.active_generation("future-stack").release_digest == RELEASE_A


def test_health_failure_rolls_back_and_crash_inspection_requires_compensation(
    tmp_path: Path,
) -> None:
    crashes = {"enabled": False}

    def crash(phase: str) -> None:
        if crashes["enabled"] and phase == "pointer-selected":
            crashes["enabled"] = False
            raise RuntimeError("crash")

    engine, state, _trust, adapters, _events = _engine(tmp_path, crash_hook=crash)
    engine.execute(
        _request(AgentOperation.PACKAGE_PREPARE, RELEASE_A), _binding(1), None
    )
    engine.execute(
        _request(AgentOperation.PACKAGE_ACTIVATE, RELEASE_A), _binding(2), None
    )
    engine.execute(
        _request(AgentOperation.PACKAGE_PREPARE, RELEASE_B), _binding(3), None
    )
    crashes["enabled"] = True
    request = _request(AgentOperation.PACKAGE_ACTIVATE, RELEASE_B)
    binding = _binding(4)

    with pytest.raises(RuntimeError, match="crash"):
        engine.execute(request, binding, None)
    assert (
        engine.inspect(request, binding, None).disposition
        is PackageDisposition.COMPENSATE
    )

    adapters.fail_health_for = RELEASE_B
    with pytest.raises(PackageEngineError, match="health"):
        engine.execute(request, binding, None)
    assert state.active_generation("future-stack").release_digest == RELEASE_A
