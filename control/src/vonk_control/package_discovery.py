"""Metadata-only workload release discovery.

Discovery is deliberately kept independent from the database and the GPU node
agent.  Providers return bounded, immutable metadata records; the control
plane can persist the records through the small ``CandidateStore`` protocol.
No provider in this module fetches a workload payload.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from cluster_profiles.workload_packages import PackageFamily

MAX_METADATA_BYTES = 1024 * 1024
MAX_CURSOR_BYTES = 64 * 1024
MAX_DETAIL_FIELDS = 16


class DiscoveryError(RuntimeError):
    """A discovery operation failed with a stable, operator-facing reason."""

    def __init__(self, reason_code: str, detail: Mapping[str, object] | None = None):
        self.reason_code = reason_code
        self.detail = _bounded_detail(detail or {})
        super().__init__(reason_code)

    def __str__(self) -> str:
        if self.detail:
            return f"{self.reason_code}: " + ", ".join(
                f"{key}={value}" for key, value in self.detail.items()
            )
        return self.reason_code


def _canonical(value: object, *, maximum: int, label: str) -> bytes:
    def plain(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): plain(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(child) for child in item]
        return item

    try:
        raw = json.dumps(
            plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ValueError(f"{label} is not canonical JSON") from error
    if not raw or len(raw) > maximum:
        raise ValueError(f"{label} exceeds its size bound")
    return raw


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _bounded_detail(value: Mapping[str, object]) -> Mapping[str, object]:
    """Keep diagnostics small and prevent credentials/URLs from being echoed."""
    result: dict[str, object] = {}
    for key in sorted(value)[:MAX_DETAIL_FIELDS]:
        if not isinstance(key, str) or len(key) > 64:
            continue
        lowered = key.lower()
        if any(
            word in lowered
            for word in ("token", "password", "secret", "authorization", "credential")
        ):
            result[key] = "[redacted]"
            continue
        item = value[key]
        if isinstance(item, (str, int, float, bool)) or item is None:
            text = item
            if isinstance(item, str) and len(item) > 256:
                text = item[:256] + "…"
            result[key] = text
        else:
            result[key] = "[redacted]"
    return MappingProxyType(result)


@dataclass(frozen=True)
class DiscoveryCandidate:
    """A release identity and bounded discovery metadata, never its payload."""

    id: str
    family_id: str
    release_key: str
    upstream_version: str
    channel: str
    published_at: str | None
    upstream_identity: Mapping[str, object]
    upstream_identity_digest: str
    metadata: Mapping[str, object]
    metadata_bytes: bytes
    metadata_digest: str

    @classmethod
    def create(
        cls,
        *,
        family_id: str,
        release_key: str,
        upstream_version: str,
        channel: str,
        published_at: str | None,
        upstream_identity: Mapping[str, object],
        metadata: Mapping[str, object],
    ) -> DiscoveryCandidate:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (family_id, release_key, upstream_version, channel)
        ):
            raise ValueError("candidate identity fields must be non-empty text")
        identity_bytes = _canonical(
            upstream_identity, maximum=64 * 1024, label="upstream identity"
        )
        metadata_bytes = _canonical(
            metadata, maximum=MAX_METADATA_BYTES, label="discovery metadata"
        )
        identity_digest = hashlib.sha256(identity_bytes).hexdigest()
        metadata_digest = hashlib.sha256(metadata_bytes).hexdigest()
        candidate_key = _canonical(
            {
                "family_id": family_id,
                "release_key": release_key,
                "upstream_identity_digest": identity_digest,
                "metadata_digest": metadata_digest,
            },
            maximum=64 * 1024,
            label="candidate identity",
        )
        candidate_id = hashlib.sha256(candidate_key).hexdigest()
        return cls(
            id=candidate_id,
            family_id=family_id,
            release_key=release_key,
            upstream_version=upstream_version,
            channel=channel,
            published_at=published_at,
            upstream_identity=_freeze(json.loads(identity_bytes)),
            upstream_identity_digest=identity_digest,
            metadata=_freeze(json.loads(metadata_bytes)),
            metadata_bytes=metadata_bytes,
            metadata_digest=metadata_digest,
        )


@dataclass(frozen=True)
class DiscoveryPage:
    candidates: tuple[DiscoveryCandidate, ...]
    next_cursor: Mapping[str, object] | None = None
    not_modified: bool = False

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        if any(not isinstance(item, DiscoveryCandidate) for item in candidates):
            raise TypeError(
                "discovery page candidates must be DiscoveryCandidate values"
            )
        ids = [item.id for item in candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("discovery page contains duplicate candidates")
        cursor = self.next_cursor
        if cursor is not None:
            _canonical(cursor, maximum=MAX_CURSOR_BYTES, label="discovery cursor")
            object.__setattr__(self, "next_cursor", _freeze(cursor))
        object.__setattr__(self, "candidates", candidates)


class DiscoveryProvider(Protocol):
    def discover(
        self, family: PackageFamily, cursor: Mapping[str, object] | None = None
    ) -> DiscoveryPage: ...


@dataclass(frozen=True)
class CandidateRecord:
    candidate: DiscoveryCandidate
    state: str = "discovered"
    reason_code: str | None = None
    detail: Mapping[str, object] = MappingProxyType({})


class CandidateStore(Protocol):
    def cursor(self, family_id: str) -> Mapping[str, object] | None: ...
    def set_cursor(
        self, family_id: str, cursor: Mapping[str, object] | None
    ) -> None: ...
    def upsert(self, candidate: DiscoveryCandidate) -> CandidateRecord: ...
    def records(self, family_id: str | None = None) -> tuple[CandidateRecord, ...]: ...
    def get(self, candidate_id: str) -> CandidateRecord | None: ...


class InMemoryCandidateStore:
    """Small deterministic store used by workers and service-level tests.

    The SQL-backed W11 projection can implement the same protocol without
    changing discovery or resolution semantics.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cursors: dict[str, Mapping[str, object] | None] = {}
        self._records: dict[str, CandidateRecord] = {}
        self._release_keys: dict[tuple[str, str], str] = {}

    def cursor(self, family_id: str) -> Mapping[str, object] | None:
        with self._lock:
            value = self._cursors.get(family_id)
            return _freeze(value) if value is not None else None

    def set_cursor(self, family_id: str, cursor: Mapping[str, object] | None) -> None:
        if cursor is not None:
            _canonical(cursor, maximum=MAX_CURSOR_BYTES, label="discovery cursor")
        with self._lock:
            self._cursors[family_id] = _freeze(cursor) if cursor is not None else None

    def upsert(self, candidate: DiscoveryCandidate) -> CandidateRecord:
        with self._lock:
            exact = self._records.get(candidate.id)
            if exact is not None:
                return exact
            key = (candidate.family_id, candidate.release_key)
            previous_id = self._release_keys.get(key)
            if previous_id is not None:
                previous = self._records.pop(previous_id)
                if (
                    previous.candidate.upstream_identity_digest
                    != candidate.upstream_identity_digest
                    or previous.candidate.metadata_digest != candidate.metadata_digest
                ):
                    record = CandidateRecord(
                        candidate=candidate,
                        state="quarantined",
                        reason_code="upstream_mutation",
                        detail=MappingProxyType({"release_key": candidate.release_key}),
                    )
                else:
                    record = previous
            else:
                record = CandidateRecord(candidate=candidate)
            self._records[candidate.id] = record
            self._release_keys[key] = candidate.id
            return record

    def records(self, family_id: str | None = None) -> tuple[CandidateRecord, ...]:
        with self._lock:
            values = tuple(self._records.values())
            if family_id is not None:
                values = tuple(
                    item for item in values if item.candidate.family_id == family_id
                )
            return tuple(sorted(values, key=lambda item: item.candidate.id))

    def get(self, candidate_id: str) -> CandidateRecord | None:
        with self._lock:
            return self._records.get(candidate_id)


class CandidateService:
    """Poll a configured provider and persist candidates exactly once."""

    def __init__(
        self,
        *,
        providers: Mapping[str, DiscoveryProvider],
        store: CandidateStore,
        families: Mapping[str, PackageFamily] | None = None,
    ):
        self._providers = dict(providers)
        self._store = store
        self._families = dict(families or {})

    def register_family(self, family: PackageFamily) -> None:
        self._families[family.family_id] = family

    def poll(
        self,
        family: PackageFamily | str,
        *,
        definition: PackageFamily | None = None,
    ) -> tuple[CandidateRecord, ...]:
        if isinstance(family, str):
            family_id = family
            family = definition or self._families.get(family_id)
            if family is None:
                raise DiscoveryError("resolution_unsupported", {"family_id": family_id})
        provider_name = family.source["provider"]
        provider = self._providers.get(provider_name)
        if provider is None:
            raise DiscoveryError(
                "resolution_unsupported", {"provider": str(provider_name)}
            )
        cursor = self._store.cursor(family.family_id)
        try:
            page = provider.discover(family, cursor)
        except DiscoveryError:
            raise
        except Exception as error:
            raise DiscoveryError(
                "discovery_unavailable",
                {"provider": provider_name, "error": type(error).__name__},
            ) from error
        for candidate in page.candidates:
            if candidate.family_id != family.family_id:
                raise DiscoveryError(
                    "resolution_unsupported", {"reason": "candidate family mismatch"}
                )
            self._store.upsert(candidate)
        if page.next_cursor is not None:
            self._store.set_cursor(family.family_id, page.next_cursor)
        return self._store.records(family.family_id)


__all__ = [
    "CandidateRecord",
    "CandidateService",
    "CandidateStore",
    "DiscoveryCandidate",
    "DiscoveryError",
    "DiscoveryPage",
    "DiscoveryProvider",
    "InMemoryCandidateStore",
]
