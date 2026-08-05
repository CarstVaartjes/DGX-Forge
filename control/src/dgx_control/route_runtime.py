"""Fail-closed, atomic route and LiteLLM bundle publication."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .presence import ManagementAddressPolicy, PresenceError

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NODE = re.compile(r"spk_[0-9a-f]{32}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_DIRECTORY = re.compile(r"[0-9]{8}-[0-9a-f]{64}\Z")
_ROUTE_FIELDS = {
    "workload_id",
    "nodes",
    "entrypoint_node_id",
    "scheme",
    "port",
    "path",
    "quota",
    "quota_digest",
}
_QUOTA_FIELDS = {"requests_per_minute", "tokens_per_minute"}
_MARKER_FIELDS = {
    "schema_version",
    "generation",
    "state",
    "reconciliation_id",
    "plan_digest",
    "evidence_set_digest",
    "routes_sha256",
    "litellm_sha256",
    "issued_at",
    "expires_at",
    "directory",
    "manifest_sha256",
}


class RouteRuntimeError(RuntimeError):
    """A route bundle could not be safely staged, activated, or inspected."""


def _encoded(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RouteRuntimeError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise RouteRuntimeError(f"activation {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RouteRuntimeError(f"activation {label} is invalid") from error
    return _aware(parsed, f"activation {label}")


@dataclass(frozen=True)
class AcceptedEndpointEvidence:
    """Endpoint address carried by already-accepted, fenced operation evidence."""

    node_id: str
    address: str
    observed_at: datetime
    operation_id: str
    verify_evidence_digest: str
    evidence_digest: str


@dataclass(frozen=True)
class RouteBundleRequest:
    reconciliation_id: str
    plan_digest: str
    evidence_set_digest: str
    routes: Mapping[str, object]
    endpoints: Mapping[str, AcceptedEndpointEvidence]
    expires_at: datetime


@dataclass(frozen=True)
class ActivationMarker:
    schema_version: int
    generation: int
    state: str
    reconciliation_id: str
    plan_digest: str
    evidence_set_digest: str
    routes_sha256: str
    litellm_sha256: str
    issued_at: str
    expires_at: str
    directory: str
    manifest_sha256: str

    def canonical_bytes(self) -> bytes:
        """Return the exact representation persisted as the activation marker."""

        return _encoded(asdict(self))

    @property
    def digest(self) -> str:
        """Bind a durable database receipt to the exact activation marker bytes."""

        return _sha256(self.canonical_bytes())


def endpoint_evidence_digest(
    *,
    node_id: str,
    address: str,
    observed_at: datetime,
    operation_id: str,
    verify_evidence_digest: str,
) -> str:
    """Bind authenticated presence to the exact accepted verify evidence."""

    return _sha256(
        _encoded(
            {
                "address": address,
                "node_id": node_id,
                "observed_at": observed_at.astimezone(UTC).isoformat(),
                "operation_id": operation_id,
                "schema_version": 1,
                "verify_evidence_digest": verify_evidence_digest,
            }
        )
    )


class AtomicRouteBundlePublisher:
    """Stage a complete bundle and replace its sole activation marker last."""

    def __init__(
        self,
        root: Path,
        *,
        management_policy: ManagementAddressPolicy,
        clock: Callable[[], datetime],
        maximum_lease_seconds: int = 300,
        validate_routes: Callable[[bytes], bool] | None = None,
        validate_litellm: Callable[[bytes], bool] | None = None,
    ) -> None:
        if root.is_symlink():
            raise RouteRuntimeError("route runtime root must not be a symlink")
        if not 1 <= maximum_lease_seconds <= 3600:
            raise RouteRuntimeError("route lease bound is invalid")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        generations = root / "generations"
        if generations.is_symlink():
            raise RouteRuntimeError("route generation root must not be a symlink")
        generations.mkdir(mode=0o700, exist_ok=True)
        self._root = root
        self._generations = generations
        self._policy = management_policy
        self._clock = clock
        self._maximum_lease = timedelta(seconds=maximum_lease_seconds)
        self._validate_routes = validate_routes or self._valid_json_mapping
        self._validate_litellm = validate_litellm or self._valid_litellm

    @staticmethod
    def _valid_json_mapping(content: bytes) -> bool:
        try:
            return isinstance(json.loads(content), dict)
        except (TypeError, json.JSONDecodeError):
            return False

    @staticmethod
    def _valid_litellm(content: bytes) -> bool:
        try:
            document = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(document, dict) and isinstance(
            document.get("model_list"), list
        )

    @staticmethod
    def empty_litellm() -> bytes:
        return _encoded(
            {
                "general_settings": {
                    "database_url": "os.environ/LITELLM_DATABASE_URL",
                    "disable_admin_ui": True,
                    "master_key": "os.environ/LITELLM_MASTER_KEY",
                },
                "litellm_settings": {
                    "drop_params": True,
                    "failure_callback": [],
                    "set_verbose": False,
                    "success_callback": [],
                },
                "model_list": [],
                "router_settings": {
                    "enable_pre_call_checks": True,
                    "routing_strategy": "simple-shuffle",
                },
            }
        )

    def _current_generation(self) -> int:
        marker = self._read_marker(
            optional=True, verify_files=False, verify_lease=False
        )
        return marker.generation if marker is not None else 0

    def _lease(self, expires_at: datetime) -> tuple[datetime, datetime]:
        issued = _aware(self._clock(), "route clock")
        expires = _aware(expires_at, "route lease expiry")
        if expires <= issued or expires - issued > self._maximum_lease:
            raise RouteRuntimeError(
                "route lease is invalid or exceeds its configured bound"
            )
        return issued, expires

    @staticmethod
    def _identity(
        reconciliation_id: str, plan_digest: str, evidence_digest: str
    ) -> None:
        try:
            parsed = uuid.UUID(reconciliation_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise RouteRuntimeError("reconciliation ID is invalid") from error
        if str(parsed) != reconciliation_id:
            raise RouteRuntimeError("reconciliation ID is not canonical")
        if (
            _DIGEST.fullmatch(plan_digest) is None
            or _DIGEST.fullmatch(evidence_digest) is None
        ):
            raise RouteRuntimeError("publication digest identity is invalid")

    def _render_routes(
        self,
        generation: int,
        request: RouteBundleRequest,
        now: datetime,
        expires: datetime,
    ) -> tuple[bytes, bytes]:
        if not request.routes:
            raise RouteRuntimeError("published routes must not be empty")
        exact_endpoints: set[str] = set()
        rendered_routes: dict[str, object] = {}
        models: list[dict[str, object]] = []
        for alias, raw in sorted(request.routes.items()):
            if not isinstance(alias, str) or _IDENTIFIER.fullmatch(alias) is None:
                raise RouteRuntimeError("route alias is invalid")
            if not isinstance(raw, Mapping) or set(raw) != _ROUTE_FIELDS:
                raise RouteRuntimeError("route fields do not match the resolved plan")
            workload_id = raw.get("workload_id")
            nodes = raw.get("nodes")
            node_id = raw.get("entrypoint_node_id")
            scheme = raw.get("scheme")
            port = raw.get("port")
            path = raw.get("path")
            quota = raw.get("quota")
            quota_digest = raw.get("quota_digest")
            if (
                not isinstance(workload_id, str)
                or _IDENTIFIER.fullmatch(workload_id) is None
                or not isinstance(nodes, (list, tuple))
                or not nodes
                or len(nodes) != len(set(nodes))
                or any(
                    not isinstance(node, str) or _NODE.fullmatch(node) is None
                    for node in nodes
                )
                or not isinstance(node_id, str)
                or node_id not in nodes
                or _NODE.fullmatch(node_id) is None
            ):
                raise RouteRuntimeError("route entrypoint is invalid")
            evidence = request.endpoints.get(node_id)
            if evidence is None or evidence.node_id != node_id:
                raise RouteRuntimeError("route endpoint evidence is unavailable")
            expected_operation = f"{workload_id}:{node_id}:workload.verify"
            if (
                _OPERATION.fullmatch(evidence.operation_id) is None
                or evidence.operation_id != expected_operation
                or _DIGEST.fullmatch(evidence.verify_evidence_digest) is None
                or _DIGEST.fullmatch(evidence.evidence_digest) is None
            ):
                raise RouteRuntimeError(
                    "route endpoint evidence is not exact verify evidence"
                )
            observed = _aware(evidence.observed_at, "endpoint evidence timestamp")
            if observed > now or now - observed > self._maximum_lease:
                raise RouteRuntimeError(
                    "route endpoint evidence is stale or in the future"
                )
            try:
                address = self._policy.validate(evidence.address)
            except PresenceError as error:
                raise RouteRuntimeError(
                    f"management address evidence is invalid: {error}"
                ) from error
            expected_endpoint_digest = endpoint_evidence_digest(
                node_id=node_id,
                address=address,
                observed_at=observed,
                operation_id=evidence.operation_id,
                verify_evidence_digest=evidence.verify_evidence_digest,
            )
            if evidence.evidence_digest != expected_endpoint_digest:
                raise RouteRuntimeError("endpoint evidence binding is invalid")
            if expires > observed + self._maximum_lease:
                raise RouteRuntimeError("route lease exceeds endpoint freshness")
            if (
                scheme not in {"http", "https"}
                or isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
                or not isinstance(path, str)
                or not path.startswith("/")
                or "?" in path
                or "#" in path
                or "//" in path
                or "/../" in f"{path}/"
            ):
                raise RouteRuntimeError("route structured endpoint is invalid")
            if not isinstance(quota, Mapping) or set(quota) != _QUOTA_FIELDS:
                raise RouteRuntimeError("route quota is invalid")
            rpm = quota.get("requests_per_minute")
            tpm = quota.get("tokens_per_minute")
            if (
                isinstance(rpm, bool)
                or not isinstance(rpm, int)
                or isinstance(tpm, bool)
                or not isinstance(tpm, int)
                or not 1 <= rpm <= 100_000
                or not 1 <= tpm <= 100_000_000
                or _DIGEST.fullmatch(quota_digest) is None
                or _sha256(_encoded(dict(quota))) != quota_digest
            ):
                raise RouteRuntimeError("route quota or quota digest is invalid")
            host = (
                f"[{address}]"
                if isinstance(ipaddress.ip_address(address), ipaddress.IPv6Address)
                else address
            )
            base = f"{scheme}://{host}:{port}{path.rstrip('/')}"
            exact_endpoints.add(node_id)
            rendered_routes[alias] = {
                "address": address,
                "evidence_digest": evidence.evidence_digest,
                "node_id": node_id,
                "observed_at": observed.isoformat(),
                "operation_id": evidence.operation_id,
                "path": path,
                "port": port,
                "scheme": scheme,
                "verify_evidence_digest": evidence.verify_evidence_digest,
            }
            models.append(
                {
                    "model_name": alias,
                    "litellm_params": {
                        "api_base": base,
                        "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                        "model": f"openai/{alias}",
                        "rpm": rpm,
                        "tpm": tpm,
                    },
                }
            )
        if set(request.endpoints) != exact_endpoints:
            raise RouteRuntimeError(
                "endpoint evidence must exactly cover route entrypoints"
            )
        route_content = _encoded(
            {
                "generation": generation,
                "routes": rendered_routes,
                "schema_version": 1,
                "state": "published",
            }
        )
        litellm_document = json.loads(self.empty_litellm())
        litellm_document["model_list"] = models
        return route_content, _encoded(litellm_document)

    def publish(self, request: RouteBundleRequest) -> ActivationMarker:
        self._identity(
            request.reconciliation_id,
            request.plan_digest,
            request.evidence_set_digest,
        )
        with self._locked():
            issued, expires = self._lease(request.expires_at)
            current = self._read_marker(
                optional=True,
                verify_files=True,
                verify_lease=False,
            )
            generation = current.generation if current is not None else 1
            routes, litellm = self._render_routes(generation, request, issued, expires)
            if (
                current is not None
                and current.state == "published"
                and current.reconciliation_id == request.reconciliation_id
                and current.plan_digest == request.plan_digest
                and current.evidence_set_digest == request.evidence_set_digest
                and current.routes_sha256 == _sha256(routes)
                and current.litellm_sha256 == _sha256(litellm)
                and _parse_time(current.expires_at, "expiry timestamp") > issued
            ):
                return current
            generation = (current.generation if current is not None else 0) + 1
            if current is not None:
                routes, litellm = self._render_routes(
                    generation, request, issued, expires
                )
            return self._activate(
                generation=generation,
                state="published",
                reconciliation_id=request.reconciliation_id,
                plan_digest=request.plan_digest,
                evidence_set_digest=request.evidence_set_digest,
                routes=routes,
                litellm=litellm,
                issued=issued,
                expires=expires,
            )

    def withdraw(
        self,
        *,
        reconciliation_id: str,
        plan_digest: str,
        targets: tuple[str, ...],
        reason: str,
    ) -> ActivationMarker:
        self._identity(reconciliation_id, plan_digest, "0" * 64)
        if (
            not targets
            or len(targets) != len(set(targets))
            or any(_NODE.fullmatch(target) is None for target in targets)
        ):
            raise RouteRuntimeError("maintenance targets are invalid")
        safe_reason = re.sub(
            r"(?i)(bearer|token|secret|password)[^\s]*",
            "<redacted>",
            reason,
        )[:256]
        with self._locked():
            issued = _aware(self._clock(), "route clock")
            expires = issued + self._maximum_lease
            current = self._read_marker(
                optional=True,
                verify_files=True,
                verify_lease=False,
            )
            generation = current.generation if current is not None else 1

            def maintenance_routes(number: int) -> bytes:
                return _encoded(
                    {
                        "generation": number,
                        "reason": safe_reason or "maintenance",
                        "routes": {},
                        "schema_version": 1,
                        "state": "maintenance",
                        "targets": sorted(targets),
                    }
                )

            routes = maintenance_routes(generation)
            empty = self.empty_litellm()
            if (
                current is not None
                and current.state == "maintenance"
                and current.reconciliation_id == reconciliation_id
                and current.plan_digest == plan_digest
                and current.routes_sha256 == _sha256(routes)
                and current.litellm_sha256 == _sha256(empty)
                and _parse_time(current.expires_at, "expiry timestamp") > issued
            ):
                return current
            generation = (current.generation if current is not None else 0) + 1
            routes = maintenance_routes(generation)
            return self._activate(
                generation=generation,
                state="maintenance",
                reconciliation_id=reconciliation_id,
                plan_digest=plan_digest,
                evidence_set_digest="0" * 64,
                routes=routes,
                litellm=empty,
                issued=issued,
                expires=expires,
            )

    @contextmanager
    def _locked(self):
        try:
            import fcntl

            path = self._root / ".publication.lock"
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RouteRuntimeError("route publication lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except RouteRuntimeError:
            raise
        except Exception as error:
            raise RouteRuntimeError("route publication lock is unavailable") from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _activate(
        self,
        *,
        generation: int,
        state: str,
        reconciliation_id: str,
        plan_digest: str,
        evidence_set_digest: str,
        routes: bytes,
        litellm: bytes,
        issued: datetime,
        expires: datetime,
    ) -> ActivationMarker:
        if self._validate_routes(routes) is not True:
            raise RouteRuntimeError("route validation rejected the staged bundle")
        if self._validate_litellm(litellm) is not True:
            raise RouteRuntimeError("LiteLLM validation rejected the staged bundle")
        manifest_document: dict[str, object] = {
            "schema_version": 1,
            "generation": generation,
            "state": state,
            "reconciliation_id": reconciliation_id,
            "plan_digest": plan_digest,
            "evidence_set_digest": evidence_set_digest,
            "routes_sha256": _sha256(routes),
            "litellm_sha256": _sha256(litellm),
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
        }
        manifest = _encoded(manifest_document)
        manifest_digest = _sha256(manifest)
        directory_name = f"{generation:08d}-{manifest_digest}"
        directory = self._generations / directory_name
        try:
            self._stage(directory, "routes.json", routes)
            self._stage(directory, "litellm.json", litellm)
            self._stage(directory, "manifest.json", manifest)
        except RouteRuntimeError:
            raise
        except Exception as error:
            raise RouteRuntimeError(
                "route bundle apply failed; previous activation retained"
            ) from error
        activation_document = {
            **manifest_document,
            "directory": directory_name,
            "manifest_sha256": manifest_digest,
        }
        marker = ActivationMarker(**activation_document)  # type: ignore[arg-type]
        try:
            self._atomic_write(
                self._root / "activation.json", _encoded(activation_document)
            )
        except Exception as error:
            raise RouteRuntimeError(
                "route bundle activation failed; previous activation retained"
            ) from error
        return marker

    @staticmethod
    def _atomic_write(target: Path, content: bytes, *, mode: int = 0o600) -> None:
        if target.is_symlink() or target.parent.is_symlink():
            raise RouteRuntimeError("route runtime target must not be a symlink")
        descriptor, temporary_raw = tempfile.mkstemp(
            prefix=f".{target.name}-", dir=target.parent
        )
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _stage(self, directory: Path, name: str, content: bytes) -> None:
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise RouteRuntimeError("staged route generation is unsafe")
        else:
            directory.mkdir(mode=0o700)
        target = directory / name
        if target.exists():
            if (
                target.is_symlink()
                or not target.is_file()
                or target.read_bytes() != content
            ):
                raise RouteRuntimeError(
                    "staged route generation conflicts with existing bytes"
                )
            return
        self._atomic_write(target, content)

    def inspect(self, *, expected: ActivationMarker | None = None) -> ActivationMarker:
        marker = self._read_marker(optional=False, verify_files=True, verify_lease=True)
        assert marker is not None
        if expected is not None and marker != expected:
            raise RouteRuntimeError(
                "active route marker does not match expected publication"
            )
        return marker

    def _read_marker(
        self,
        *,
        optional: bool,
        verify_files: bool,
        verify_lease: bool,
    ) -> ActivationMarker | None:
        active = self._root / "activation.json"
        if not active.exists():
            if optional:
                return None
            raise RouteRuntimeError("no route bundle is active")
        if active.is_symlink() or not active.is_file():
            raise RouteRuntimeError("route activation marker is unsafe")
        try:
            content = active.read_bytes()
            raw: Any = json.loads(content)
        except (OSError, json.JSONDecodeError) as error:
            raise RouteRuntimeError("route activation marker is unreadable") from error
        if not isinstance(raw, dict) or set(raw) != _MARKER_FIELDS:
            raise RouteRuntimeError("route activation marker fields are invalid")
        try:
            marker = ActivationMarker(**raw)
        except TypeError as error:
            raise RouteRuntimeError(
                "route activation marker fields are invalid"
            ) from error
        self._validate_marker(marker)
        if content != marker.canonical_bytes():
            raise RouteRuntimeError("route activation marker is not canonical")
        if verify_files:
            directory = self._generations / marker.directory
            if directory.is_symlink() or not directory.is_dir():
                raise RouteRuntimeError("active route generation is unavailable")
            manifest_document = {
                field: getattr(marker, field)
                for field in (
                    "schema_version",
                    "generation",
                    "state",
                    "reconciliation_id",
                    "plan_digest",
                    "evidence_set_digest",
                    "routes_sha256",
                    "litellm_sha256",
                    "issued_at",
                    "expires_at",
                )
            }
            expected_files = {
                "manifest.json": (marker.manifest_sha256, _encoded(manifest_document)),
                "routes.json": (marker.routes_sha256, None),
                "litellm.json": (marker.litellm_sha256, None),
            }
            for name, (digest, exact) in expected_files.items():
                target = directory / name
                if target.is_symlink() or not target.is_file():
                    raise RouteRuntimeError("active route generation file is unsafe")
                content = target.read_bytes()
                if _sha256(content) != digest or (
                    exact is not None and content != exact
                ):
                    raise RouteRuntimeError("active route generation checksum mismatch")
        if verify_lease:
            now = _aware(self._clock(), "route clock")
            issued = _parse_time(marker.issued_at, "issued timestamp")
            expires = _parse_time(marker.expires_at, "expiry timestamp")
            if (
                issued > now
                or now >= expires
                or expires <= issued
                or expires - issued > self._maximum_lease
            ):
                raise RouteRuntimeError("active route lease is invalid or expired")
        return marker

    @staticmethod
    def _validate_marker(marker: ActivationMarker) -> None:
        if (
            marker.schema_version != 1
            or isinstance(marker.generation, bool)
            or not isinstance(marker.generation, int)
            or marker.generation <= 0
            or marker.state not in {"maintenance", "published"}
            or _DIRECTORY.fullmatch(marker.directory) is None
            or marker.directory != f"{marker.generation:08d}-{marker.manifest_sha256}"
            or any(
                _DIGEST.fullmatch(value) is None
                for value in (
                    marker.plan_digest,
                    marker.evidence_set_digest,
                    marker.routes_sha256,
                    marker.litellm_sha256,
                    marker.manifest_sha256,
                )
            )
        ):
            raise RouteRuntimeError("route activation marker identity is invalid")
        AtomicRouteBundlePublisher._identity(
            marker.reconciliation_id,
            marker.plan_digest,
            marker.evidence_set_digest,
        )
