"""Deterministic fleet compatibility evaluation for workload release locks.

The evaluator intentionally consumes authenticated observation projections rather
than SQL models.  That keeps package policy independent from the discovery and
operational-state migrations and makes the result safe to bind to a release
digest, fleet observation digest, and validation run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


class CompatibilityError(ValueError):
    """A lock or authenticated fleet projection is not evaluable."""


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _get(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    else:
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
    return default


def _tuple_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(item) for item in value)  # type: ignore[arg-type]
    except TypeError:
        return ()


def _architecture(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    normalized = normalized.removeprefix("linux-")
    return {"aarch64": "arm64", "amd64": "x86_64", "x86-64": "x86_64"}.get(
        normalized, normalized
    )


def _version(value: object) -> tuple[object, ...]:
    """Return a comparison key for dotted numeric or opaque versions."""

    text = str(value).strip()
    if not text:
        return ()
    parts: list[object] = []
    for part in re.split(r"[._+\-]", text):
        parts.append(int(part) if part.isdigit() else part.lower())
    return tuple(parts)


def _at_least(actual: object, minimum: object) -> bool:
    if actual is None or minimum is None:
        return False
    left = _version(actual)
    right = _version(minimum)
    # Numeric release versions are compared component-wise.  For opaque
    # versions lexical ordering is the only safe deterministic fallback.
    try:
        return left >= right
    except TypeError:
        return str(actual) >= str(minimum)


def _lock_value(lock: object, name: str, default: object = None) -> object:
    return _get(lock, name, default=default)


def _release_digest(lock: object) -> str:
    digest = _get(lock, "digest", "release_digest")
    if isinstance(digest, str) and _DIGEST.fullmatch(digest):
        return digest
    canonical = _get(lock, "canonical_bytes")
    if callable(canonical):
        canonical = canonical()
    if isinstance(canonical, str):
        canonical = canonical.encode()
    if isinstance(canonical, bytes):
        return hashlib.sha256(canonical).hexdigest()
    if isinstance(lock, Mapping):
        return hashlib.sha256(
            json.dumps(dict(lock), sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
    raise CompatibilityError("workload release lock has no digest")


@dataclass(frozen=True)
class CompatibilityReport:
    release_digest: str
    compatible_node_ids: tuple[str, ...]
    incompatible: Mapping[str, tuple[str, ...]]
    required_platform_capabilities: tuple[str, ...]
    digest: str

    @property
    def compatible(self) -> bool:
        return bool(self.compatible_node_ids)

    @property
    def incompatible_node_ids(self) -> tuple[str, ...]:
        return tuple(self.incompatible)

    @property
    def incompatible_nodes(self) -> Mapping[str, tuple[str, ...]]:
        """Compatibility spelling used by fleet projections."""

        return self.incompatible

    @property
    def canonical_bytes(self) -> bytes:
        value = {
            "compatible_node_ids": list(self.compatible_node_ids),
            "incompatible": {
                key: list(reasons) for key, reasons in self.incompatible.items()
            },
            "release_digest": self.release_digest,
            "required_platform_capabilities": list(self.required_platform_capabilities),
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _nodes(fleet: object) -> tuple[tuple[str, object], ...]:
    if isinstance(fleet, Mapping):
        for key in ("nodes", "observations"):
            if key in fleet and isinstance(fleet[key], (Mapping, list, tuple)):
                fleet = fleet[key]
                break
        if isinstance(fleet, Mapping):
            return tuple(sorted(((str(key), value) for key, value in fleet.items()), key=lambda item: item[0]))
    if isinstance(fleet, Iterable) and not isinstance(fleet, (str, bytes)):
        values: list[tuple[str, object]] = []
        for value in fleet:
            node_id = _get(value, "node_id", "id")
            if isinstance(node_id, str):
                values.append((node_id, value))
        return tuple(sorted(values, key=lambda item: item[0]))
    raise CompatibilityError("fleet snapshot is invalid")


class CompatibilityEvaluator:
    """Evaluate a complete release lock against an authenticated fleet snapshot."""

    def evaluate(self, lock: object, fleet: object) -> CompatibilityReport:
        digest = _release_digest(lock)
        compatibility = _lock_value(lock, "compatibility", {})
        if not isinstance(compatibility, Mapping):
            raise CompatibilityError("release compatibility policy is invalid")
        architectures = {
            normalized
            for item in _tuple_strings(compatibility.get("architectures"))
            if (normalized := _architecture(item)) is not None
        }
        operating_systems = set(
            _tuple_strings(compatibility.get("operating_systems", compatibility.get("oses")))
        )
        required_capabilities = tuple(
            sorted(_tuple_strings(compatibility.get("required_capabilities")))
        )
        minimum_storage = compatibility.get(
            "minimum_storage_bytes", compatibility.get("min_storage_bytes", 0)
        )
        minimum_memory = compatibility.get(
            "minimum_memory_bytes", compatibility.get("min_memory_bytes", 0)
        )
        minimum_driver = compatibility.get("minimum_driver", compatibility.get("driver"))
        minimum_cuda = compatibility.get("minimum_cuda", compatibility.get("cuda"))
        required_backends = set(_tuple_strings(compatibility.get("backends")))
        adapter_abi = _lock_value(lock, "adapter_abi")
        incompatible: dict[str, tuple[str, ...]] = {}
        compatible: list[str] = []

        for node_id, node in _nodes(fleet):
            reasons: list[str] = []
            authenticated = _get(node, "authenticated", "authenticated_observation", default=False)
            if authenticated is not True:
                reasons.append("authentication-missing")
            if _get(node, "online", "available", default=True) is not True:
                reasons.append("offline")
            if _get(node, "healthy", "ready", default=True) is False:
                reasons.append("unhealthy")
            architecture = _architecture(_get(node, "architecture", "platform"))
            if architectures and architecture not in architectures:
                reasons.append("architecture-incompatible")
            operating_system = _get(node, "operating_system", "os", "platform_os")
            if operating_systems and (
                not isinstance(operating_system, str)
                or operating_system not in operating_systems
            ):
                reasons.append("operating-system-incompatible")
            memory = _get(node, "memory_bytes", "memory_available_bytes", "available_memory_bytes")
            if isinstance(minimum_memory, int) and minimum_memory > 0 and (
                not isinstance(memory, int) or memory < minimum_memory
            ):
                reasons.append("memory-insufficient")
            storage = _get(node, "storage_available_bytes", "available_storage_bytes", "storage_bytes")
            if isinstance(minimum_storage, int) and minimum_storage > 0 and (
                not isinstance(storage, int) or storage < minimum_storage
            ):
                reasons.append("storage-insufficient")
            capabilities = set(_tuple_strings(_get(node, "capabilities", "platform_capabilities", default=())))
            missing = sorted(set(required_capabilities) - capabilities)
            if missing:
                reasons.append("capability-missing")
            if minimum_driver is not None and not _at_least(
                _get(node, "driver", "driver_version"), minimum_driver
            ):
                reasons.append("driver-incompatible")
            if minimum_cuda is not None and not _at_least(
                _get(node, "cuda", "cuda_version"), minimum_cuda
            ):
                reasons.append("cuda-incompatible")
            backends = set(
                _tuple_strings(
                    _get(node, "backends", "available_backends", default=())
                )
            )
            if not backends:
                # Agent observations advertise the stable capability token;
                # older projections do not have a separate backend field.
                backends = {
                    capability.removeprefix("package-backend-").removesuffix("-v1")
                    for capability in capabilities
                    if capability.startswith("package-backend-")
                    and capability.endswith("-v1")
                }
            if required_backends and not required_backends.issubset(backends):
                reasons.append("backend-missing")
            if adapter_abi is not None:
                adapter_abis = _get(node, "adapter_abis", "supported_adapter_abis", default=())
                if isinstance(adapter_abis, int):
                    adapter_abis = (adapter_abis,)
                if not adapter_abis and "package-abi-v1" in capabilities:
                    adapter_abis = (1,)
                if adapter_abi not in set(adapter_abis):
                    reasons.append("adapter-abi-incompatible")
            if reasons:
                incompatible[node_id] = tuple(dict.fromkeys(reasons))
            else:
                compatible.append(node_id)
        report_value = {
            "compatible_node_ids": compatible,
            "incompatible": {key: list(value) for key, value in incompatible.items()},
            "release_digest": digest,
            "required_platform_capabilities": list(required_capabilities),
        }
        canonical = json.dumps(report_value, sort_keys=True, separators=(",", ":")).encode()
        return CompatibilityReport(
            release_digest=digest,
            compatible_node_ids=tuple(compatible),
            incompatible=MappingProxyType(incompatible),
            required_platform_capabilities=required_capabilities,
            digest=hashlib.sha256(canonical).hexdigest(),
        )


__all__ = ["CompatibilityError", "CompatibilityEvaluator", "CompatibilityReport"]
