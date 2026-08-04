"""Crash-recovering lifecycle for the outbound Spark agent."""
from __future__ import annotations

import argparse
import random
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from dgx_agent_protocol import AgentClaim, AgentResult

from .client import (
    AgentClient,
    AgentTransportError,
    CredentialProvider,
    CredentialStore,
    IssuedCredential,
)
from .config import DEFAULT_CONFIG_PATH, AgentConfig
from .nvidia_tools import InstalledPolicy
from .operations import OperationContext, OperationRegistry
from .probe import PinnedNodeProbe
from .state import AgentStateStore


class AgentControl(Protocol):
    def claim(self) -> AgentClaim | None: ...

    def result(self, result: AgentResult) -> None: ...

    def renew(self, csr: bytes) -> IssuedCredential: ...

    def activate(self, generation: int, credentials: CredentialProvider) -> None: ...


class Interrupt(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


class Agent:
    def __init__(
        self,
        client: AgentControl,
        registry: OperationRegistry,
        context: OperationContext,
        *,
        backoff_min_seconds: float = 1,
        backoff_max_seconds: float = 60,
        jitter: Callable[[float], float] | None = None,
        credentials: CredentialStore | None = None,
    ) -> None:
        if backoff_min_seconds <= 0 or backoff_max_seconds < backoff_min_seconds:
            raise ValueError("backoff bounds are invalid")
        self._client = client
        self._registry = registry
        self._context = context
        self._backoff_min = float(backoff_min_seconds)
        self._backoff_max = float(backoff_max_seconds)
        self._jitter = jitter or (lambda upper: random.uniform(0, upper))
        self._credentials = credentials

    def run_once(self) -> None:
        pending = self._context.state.recover_pending()
        if pending is not None:
            assert pending.result is not None
            self._submit(pending.result)
            return
        active = self._context.state.recover_active()
        if active is not None:
            execution = self._registry.execute(active.claim, self._context)
            self._submit(execution.result)
            return
        if self._credentials is not None:
            self._rotate_credentials()
        claim = self._client.claim()
        if claim is None:
            return
        execution = self._registry.execute(claim, self._context)
        self._submit(execution.result)

    def run_forever(self, stop: Interrupt) -> None:
        backoff = self._backoff_min
        while not stop.is_set():
            try:
                self.run_once()
            except AgentTransportError:
                delay = max(0.0, min(backoff, float(self._jitter(backoff))))
                if stop.wait(delay):
                    return
                backoff = min(self._backoff_max, backoff * 2)
            else:
                backoff = self._backoff_min

    def _submit(self, result: AgentResult) -> None:
        self._client.result(result)
        self._context.state.acknowledge(result)

    def _rotate_credentials(self) -> None:
        assert self._credentials is not None
        staged = self._credentials.staged_provider()
        if staged is not None:
            generation = self._credentials.staged_generation
            assert generation is not None
            self._client.activate(generation, staged)
            self._credentials.publish_active(generation)
            return
        pending = self._credentials.pending_rotation()
        if pending is None:
            if not self._credentials.renewal_due(datetime.now(UTC)):
                return
            pending = self._credentials.prepare_rotation(self._context.node_id)
        issued = self._client.renew(pending.csr_pem)
        self._credentials.stage(issued)
        staged = self._credentials.staged_provider()
        if staged is None:
            raise RuntimeError("staged credential was not published")
        self._client.activate(issued.generation, staged)
        self._credentials.publish_active(issued.generation)


def build_agent(config: AgentConfig) -> Agent:
    credentials = CredentialStore(
        config.state_root,
        config.ca_path,
        config.certificate_path,
        config.private_key_path,
    )
    client = AgentClient(
        config.control_origin,
        config.node_id,
        credentials,
        long_poll_seconds=min(60, config.poll_max_seconds),
        lease_seconds=max(30, min(300, config.poll_max_seconds * 2)),
    )
    state = AgentStateStore(config.state_root)
    policy = InstalledPolicy.load(config.installed_policy_path)
    context = OperationContext(
        node_id=config.node_id,
        state=state,
        probe=PinnedNodeProbe(policy),
    )
    return Agent(
        client,
        OperationRegistry(),
        context,
        backoff_min_seconds=config.poll_min_seconds,
        backoff_max_seconds=config.poll_max_seconds,
        credentials=credentials,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DGX Forge outbound Spark agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="absolute path to the restrictive agent configuration",
    )
    arguments = parser.parse_args(argv)
    config = AgentConfig.load(arguments.config)
    stop = threading.Event()

    def terminate(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    build_agent(config).run_forever(stop)
    return 0
