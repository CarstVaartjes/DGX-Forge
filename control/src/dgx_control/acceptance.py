"""Deterministic platform simulators used by pre-release acceptance gates."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from .reconcile import Reconciler


class _Eligible:
    def eligible(self, _commit: str):
        return type("Eligibility", (), {"ok": True, "reasons": ()})()


class _Routes:
    def __init__(self) -> None:
        self.state = "unavailable"
        self.publishes = 0

    def withdraw(self, _targets) -> None:
        self.state = "maintenance"

    def publish_atomically(self, _routes) -> None:
        self.publishes += 1
        self.state = "published"


class _Leases:
    @contextmanager
    def acquire(self, _targets):
        yield


class _Controller:
    def __init__(self, fault: str | None = None) -> None:
        self.fault = fault
        self.mutations: list[str] = []

    def apply(self, plan) -> None:
        if self.fault in {"postgres", "worker", "caddy", "litellm", "git", "ssh", "host"}:
            raise RuntimeError("injected service failure")
        for target in plan.targets:
            self.mutations.append(target)

    def verify(self, _plan) -> bool:
        return self.fault is None


@dataclass(frozen=True)
class AcceptanceResult:
    nodes: int
    fault: str | None
    planned_nodes: int
    duplicate_mutations: int
    terminal_job_state: str
    route_state: str


def simulate(nodes: int, fault: str | None = None) -> AcceptanceResult:
    if nodes < 1:
        raise ValueError("fleet must contain at least one node")
    targets = [f"spk_{index:032x}" for index in range(nodes)]
    routes = _Routes()
    controller = _Controller(fault)
    reconciler = Reconciler(
        _Eligible(),
        lambda _commit: {"targets": targets, "routes": {"model": "upstream"}},
        routes,
        controller,
        _Leases(),
    )
    result = reconciler.execute(reconciler.plan("a" * 40))
    return AcceptanceResult(
        nodes=nodes,
        fault=fault,
        planned_nodes=len(result.targets),
        duplicate_mutations=len(controller.mutations) - len(set(controller.mutations)),
        terminal_job_state=result.status,
        route_state=routes.state,
    )
