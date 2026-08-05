"""Production bridge from authenticated presence to live LiteLLM routes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .git_content import read_commit_file
from .litellm import LiteLlmGeneration, LiteLlmPolicy, LiteLlmPublisher
from .presence import AgentPresenceService, ManagementAddressPolicy, PresenceError
from .routes import (
    RouteCandidate,
    RouteEndpoint,
    RouteEndpointPolicy,
    RoutePublisher,
    RouteState,
    RouteValidationError,
)

_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_NODE = re.compile(r"spk_[0-9a-f]{32}")
_WORKLOAD = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_ROUTE_FIELDS = {
    "node_id",
    "workload",
    "requests_per_minute",
    "tokens_per_minute",
}


class RouteRuntimeError(RuntimeError):
    """A live route could not be safely derived or applied."""


@dataclass(frozen=True)
class RouteRuntimeResult:
    route_state: RouteState
    litellm_generation: LiteLlmGeneration


class AtomicConfigTarget:
    def __init__(self, target: Path, *, mode: int = 0o644) -> None:
        if target.is_symlink() or target.parent.is_symlink():
            raise RouteRuntimeError("LiteLLM live config path must not be a symlink")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self._target = target
        self._mode = mode

    def write(self, content: bytes) -> None:
        try:
            json.loads(content)
        except (TypeError, json.JSONDecodeError) as error:
            raise RouteRuntimeError("LiteLLM live config is invalid") from error
        descriptor, temporary_raw = tempfile.mkstemp(prefix=".config-", dir=self._target.parent)
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, self._mode)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._target)
            directory = os.open(self._target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


class LeasedConfigTarget:
    """Atomically bind live config bytes to a short, supervisor-visible lease."""

    def __init__(
        self,
        target: Path,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._config = AtomicConfigTarget(target)
        self._lease = AtomicConfigTarget(target.parent / "lease.json", mode=0o600)
        self._clock = clock

    def write(self, content: bytes, *, expires_at: datetime) -> None:
        issued_at = self._clock()
        if (
            issued_at.tzinfo is None
            or issued_at.utcoffset() is None
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or expires_at.astimezone(UTC) <= issued_at.astimezone(UTC)
        ):
            raise RouteRuntimeError("LiteLLM route lease is invalid")
        lease = {
            "config_sha256": hashlib.sha256(content).hexdigest(),
            "expires_at": expires_at.astimezone(UTC).isoformat(),
            "issued_at": issued_at.astimezone(UTC).isoformat(),
        }
        self._config.write(content)
        try:
            self._lease.write(
                (json.dumps(lease, sort_keys=True, separators=(",", ":")) + "\n").encode()
            )
        except Exception:
            self.withdraw()
            raise

    def withdraw(self) -> None:
        content = LiteLlmPublisher.render_empty()
        self._config.write(content)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RouteRuntimeError("route clock must be timezone-aware")
        lease = {
            "config_sha256": hashlib.sha256(content).hexdigest(),
            "expires_at": now.astimezone(UTC).isoformat(),
            "issued_at": now.astimezone(UTC).isoformat(),
        }
        self._lease.write(
            (json.dumps(lease, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )


def probe_openai_endpoint(url: str, upstream_key: str) -> None:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {upstream_key}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if not 200 <= response.status < 300:
            raise OSError("upstream returned a non-success status")
        if len(response.read(65_537)) > 65_536:
            raise OSError("upstream probe response exceeded its bound")


class ProductionRouteManager:
    def __init__(
        self,
        repository_root: Path,
        *,
        state_root: Path,
        live_config: Path,
        presence: AgentPresenceService,
        management_policy: ManagementAddressPolicy,
        upstream_key: str,
        probe: Callable[[str, str], None] = probe_openai_endpoint,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        maximum_age_seconds: int = 150,
        refresh_interval_seconds: int = 60,
        repository_reader: Callable[[str, str], bytes] | None = None,
    ) -> None:
        if not upstream_key or any(character.isspace() for character in upstream_key):
            raise RouteRuntimeError("LiteLLM upstream key is invalid")
        if maximum_age_seconds <= 0:
            raise RouteRuntimeError("route maximum age must be positive")
        if not 1 <= refresh_interval_seconds < maximum_age_seconds:
            raise RouteRuntimeError("route refresh interval must be below maximum age")
        self._repository_root = repository_root
        self._state_root = state_root
        self._presence = presence
        self._management_policy = management_policy
        self._upstream_key = upstream_key
        self._probe = probe
        self._clock = clock
        self._maximum_age_seconds = maximum_age_seconds
        self._refresh_interval_seconds = refresh_interval_seconds
        self._repository_reader = repository_reader or (
            lambda commit, path: read_commit_file(repository_root, commit, path)
        )
        self._last_refresh_attempt: datetime | None = None
        self._target = LeasedConfigTarget(live_config, clock=clock)
        self._desired = AtomicConfigTarget(
            state_root / "desired-route.json",
            mode=0o600,
        )
        # A restored named volume must never make an old route live. The worker
        # revalidates and re-leases desired state through refresh_if_due().
        self._target.withdraw()

    def _publisher(
        self,
        allowed_ports: frozenset[int],
    ) -> tuple[RoutePublisher, RouteEndpointPolicy]:
        endpoint_policy = RouteEndpointPolicy(
            management=self._management_policy,
            allowed_ports=allowed_ports or frozenset({1}),
            maximum_age_seconds=self._maximum_age_seconds,
            clock=self._clock,
        )

        def apply_route(content: bytes) -> None:
            payload = json.loads(content)
            if payload.get("state") == "maintenance":
                self._target.withdraw()

        return (
            RoutePublisher(
                self._state_root / "routes",
                endpoint_policy=endpoint_policy,
                validate=lambda content: isinstance(json.loads(content), Mapping),
                apply=apply_route,
            ),
            endpoint_policy,
        )

    def withdraw(self, targets: tuple[str, ...]) -> RouteState:
        try:
            publisher, _policy = self._publisher(frozenset())
            return publisher.maintenance(
                targets,
                "reconciliation in progress",
            )
        except (OSError, RouteValidationError) as error:
            raise RouteRuntimeError(str(error)) from error

    def _workload_port(self, commit: str, workload: str) -> int:
        try:
            content = self._repository_reader(
                commit,
                f"config/workloads/{workload}.toml",
            )
            document = tomllib.loads(content.decode("utf-8"))
            if document.get("id") != workload:
                raise RouteRuntimeError("route workload identity does not match its file")
            endpoint = document["endpoint"]
            if not isinstance(endpoint, Mapping) or endpoint.get("host") != "127.0.0.1":
                raise RouteRuntimeError("route workload endpoint must declare loopback locally")
            port = endpoint["port"]
        except (KeyError, OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as error:
            raise RouteRuntimeError("route workload endpoint is invalid") from error
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise RouteRuntimeError("route workload port is invalid")
        return port

    def _require_accepted_fleet(self, commit: str, targets: tuple[str, ...]) -> None:
        try:
            content = self._repository_reader(commit, "inventory/fleet.toml")
            document = tomllib.loads(content.decode("utf-8"))
            nodes = document["nodes"]
        except (KeyError, OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as error:
            raise RouteRuntimeError("accepted fleet inventory is invalid") from error
        if not isinstance(nodes, Mapping) or any(
            not isinstance(nodes.get(node_id), Mapping)
            or nodes[node_id].get("lifecycle") != "ready"
            for node_id in targets
        ):
            raise RouteRuntimeError("route target is not ready in the accepted fleet")

    def _publish(
        self,
        *,
        commit: str,
        profile: str,
        targets: tuple[str, ...],
        routes: Mapping[str, object],
        persist_desired: bool,
    ) -> RouteRuntimeResult:
        if not routes:
            raise RouteRuntimeError("reconciliation routes must not be empty")
        endpoints: dict[str, RouteEndpoint] = {}
        quotas: dict[str, Mapping[str, int]] = {}
        workloads: set[str] = set()
        ports: set[int] = set()
        observations = []
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RouteRuntimeError("route clock must be timezone-aware")
        self._require_accepted_fleet(commit, targets)
        for alias, raw in sorted(routes.items()):
            if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
                raise RouteRuntimeError("route alias is invalid")
            if not isinstance(raw, Mapping) or set(raw) != _ROUTE_FIELDS:
                raise RouteRuntimeError("route fields are invalid")
            node_id = raw.get("node_id")
            workload = raw.get("workload")
            rpm = raw.get("requests_per_minute")
            tpm = raw.get("tokens_per_minute")
            if not isinstance(node_id, str) or _NODE.fullmatch(node_id) is None or node_id not in targets:
                raise RouteRuntimeError("route node is not a reconciliation target")
            if not isinstance(workload, str) or _WORKLOAD.fullmatch(workload) is None:
                raise RouteRuntimeError("route workload is invalid")
            port = self._workload_port(commit, workload)
            try:
                observation = self._presence.latest(
                    node_id,
                    maximum_age_seconds=self._maximum_age_seconds,
                    now=now,
                )
            except PresenceError as error:
                raise RouteRuntimeError(str(error)) from error
            endpoints[alias] = RouteEndpoint(
                node_id=node_id,
                address=observation.address,
                port=port,
                scheme="http",
                observed_at=observation.observed_at,
            )
            quotas[alias] = {
                "requests_per_minute": rpm,
                "tokens_per_minute": tpm,
            }
            workloads.add(workload)
            ports.add(port)
            observations.append(observation)
        publisher, endpoint_policy = self._publisher(frozenset(ports))
        try:
            desired = {
                "commit": commit,
                "profile": profile,
                "routes": routes,
                "targets": list(targets),
            }
            if persist_desired:
                self._desired.write(
                    (json.dumps(desired, sort_keys=True, separators=(",", ":")) + "\n").encode()
                )
                self._last_refresh_attempt = self._clock()

            rendered = {
                alias: endpoint_policy.render(endpoint, node_ids=targets)
                for alias, endpoint in endpoints.items()
            }
            try:
                current = publisher.snapshot()
            except RouteValidationError:
                current = None
            address_changed = (
                current is not None
                and current.state == "published"
                and dict(current.aliases) != rendered
            )
            if address_changed:
                publisher.maintenance(targets, "route endpoint changed")

            for endpoint in endpoints.values():
                url = f"http://{endpoint.address}:{endpoint.port}/v1/models"
                try:
                    self._probe(url, self._upstream_key)
                except Exception as error:
                    publisher.maintenance(targets, "route endpoint probe failed")
                    raise RouteRuntimeError("route endpoint probe failed") from error

            health_timestamp = self._clock()
            candidate = RouteCandidate(
                commit=commit,
                profile=profile,
                workload=(next(iter(workloads)) if len(workloads) == 1 else "profile-routes"),
                node_ids=targets,
                aliases=endpoints,
                health_timestamp=health_timestamp,
            )
            route_state = publisher.publish(candidate)
            expires_at = min(
                observation.observed_at.astimezone(UTC)
                + timedelta(seconds=self._maximum_age_seconds)
                for observation in observations
            )
            litellm = LiteLlmPublisher(
                self._state_root / "litellm",
                validate=lambda content: isinstance(json.loads(content).get("model_list"), list),
                apply=lambda content: self._target.write(content, expires_at=expires_at),
            )
            generation = litellm.publish(route_state, LiteLlmPolicy(models=quotas))
        except (OSError, RouteValidationError, ValueError) as error:
            try:
                publisher.maintenance(targets, "route publication failed")
            except (OSError, RouteValidationError):
                self._target.withdraw()
            raise RouteRuntimeError(str(error)) from error
        return RouteRuntimeResult(route_state, generation)

    def publish(
        self,
        *,
        commit: str,
        profile: str,
        targets: tuple[str, ...],
        routes: Mapping[str, object],
    ) -> RouteRuntimeResult:
        return self._publish(
            commit=commit,
            profile=profile,
            targets=targets,
            routes=routes,
            persist_desired=True,
        )

    def refresh_if_due(
        self,
        current_commit: Callable[[], str],
        *,
        eligible: Callable[[str], bool] = lambda _commit: True,
    ) -> bool:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RouteRuntimeError("route clock must be timezone-aware")
        if self._last_refresh_attempt is not None:
            elapsed = now.astimezone(UTC) - self._last_refresh_attempt.astimezone(UTC)
            if elapsed.total_seconds() < self._refresh_interval_seconds:
                return False
        self._last_refresh_attempt = now
        desired_path = self._state_root / "desired-route.json"
        if desired_path.is_symlink() or not desired_path.is_file():
            return False
        try:
            desired = json.loads(desired_path.read_bytes())
            commit = desired["commit"]
            profile = desired["profile"]
            targets = tuple(desired["targets"])
            routes = desired["routes"]
            if not isinstance(routes, Mapping):
                raise TypeError
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RouteRuntimeError("desired route state is unreadable") from error
        if current_commit() != commit or eligible(commit) is not True:
            self.withdraw(targets)
            return False
        try:
            self._publish(
                commit=commit,
                profile=profile,
                targets=targets,
                routes=routes,
                persist_desired=False,
            )
        except RouteRuntimeError:
            self.withdraw(targets)
            return False
        return True
