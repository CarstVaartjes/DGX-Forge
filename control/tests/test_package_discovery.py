from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest
from vonk_control.package_discovery import (
    CandidateService,
    DiscoveryCandidate,
    DiscoveryError,
    DiscoveryPage,
    InMemoryCandidateStore,
)

from cluster_profiles.workload_packages import PackageFamily


def _family(provider: str = "signed-http-index") -> PackageFamily:
    return PackageFamily.load(
        {
            "schema_version": 1,
            "family_id": "synthetic-runtime",
            "source": {
                "provider": provider,
                "locator": "https://releases.example/index.json",
                "policy_refs": ["policy://origin/releases"],
            },
            "versions": {
                "scheme": "semver",
                "channels": ["stable"],
                "include_prereleases": False,
            },
            "discovery": {
                "poll_interval_seconds": 60,
                "bindings": [
                    {
                        "target": "upstream_identity.commit",
                        "source": "release.commit",
                        "value_type": "git-commit",
                        "required": True,
                    }
                ],
            },
            "resolution": {
                "recipe_version": 1,
                "components": [
                    {
                        "name": "runtime",
                        "kind": "artifact",
                        "media_type": "application/octet-stream",
                        "materialization": "file",
                        "platforms": ["linux/arm64"],
                    }
                ],
                "dependencies": [],
            },
            "policy": {
                "required_evidence": ["checksum"],
                "license_policy_refs": [],
            },
            "compatibility": {
                "architectures": ["linux-arm64"],
                "operating_systems": ["linux"],
                "min_memory_bytes": 1,
                "min_storage_bytes": 1,
            },
            "execution": {"backend": "native", "adapter_abi": 1},
            "validation": [{"kind": "health", "timeout_seconds": 30}],
            "retention": {"release_count": 3, "rollback_count": 1},
        }
    )


@dataclass
class FakeProvider:
    pages: list[DiscoveryPage]
    calls: list[object]

    def discover(self, family: PackageFamily, cursor: object = None) -> DiscoveryPage:
        self.calls.append(cursor)
        if not self.pages:
            raise AssertionError("unexpected discovery call")
        return self.pages.pop(0)


def _candidate(*, version: str = "1.2.3", commit: str = "a" * 40) -> DiscoveryCandidate:
    metadata = {
        "release": {
            "version": version,
            "channel": "stable",
            "commit": commit,
        },
        "components": [],
    }
    identity = {
        "provider": "git",
        "repository": "https://git.example/release.git",
        "commit": commit,
    }
    return DiscoveryCandidate.create(
        family_id="synthetic-runtime",
        release_key=version,
        upstream_version=version,
        channel="stable",
        published_at="2026-08-06T10:00:00Z",
        upstream_identity=identity,
        metadata=metadata,
    )


def test_candidate_identity_is_deterministic_and_metadata_is_frozen() -> None:
    first = _candidate()
    second = DiscoveryCandidate.create(
        family_id="synthetic-runtime",
        release_key="1.2.3",
        upstream_version="1.2.3",
        channel="stable",
        published_at="2026-08-06T10:00:00Z",
        upstream_identity={
            "commit": "a" * 40,
            "repository": "https://git.example/release.git",
            "provider": "git",
        },
        metadata={
            "components": [],
            "release": {"commit": "a" * 40, "channel": "stable", "version": "1.2.3"},
        },
    )

    assert first.id == second.id
    assert first.metadata_digest == hashlib.sha256(first.metadata_bytes).hexdigest()
    with pytest.raises(TypeError):
        first.metadata["release"] = {}  # type: ignore[index]


def test_candidate_poll_is_repeat_safe_and_advances_durable_cursor() -> None:
    candidate = _candidate()
    provider = FakeProvider(
        pages=[
            DiscoveryPage(candidates=(candidate,), next_cursor={"page": 2}),
            DiscoveryPage(candidates=(candidate,), next_cursor={"page": 2}),
        ],
        calls=[],
    )
    store = InMemoryCandidateStore()
    service = CandidateService(
        providers={"signed-http-index": provider},
        store=store,
    )

    first = service.poll(_family())
    second = service.poll(_family())

    assert first == second
    assert len(store.records()) == 1
    assert provider.calls == [None, {"page": 2}]
    assert store.cursor("synthetic-runtime") == {"page": 2}


def test_moved_release_is_quarantined_and_cursor_is_not_lost() -> None:
    original = _candidate()
    moved = _candidate(commit="b" * 40)
    provider = FakeProvider(
        pages=[
            DiscoveryPage(candidates=(original,), next_cursor={"page": 2}),
            DiscoveryPage(candidates=(moved,), next_cursor={"page": 3}),
        ],
        calls=[],
    )
    store = InMemoryCandidateStore()
    service = CandidateService(providers={"signed-http-index": provider}, store=store)

    service.poll(_family())
    records = service.poll(_family())

    assert records[0].state == "quarantined"
    assert records[0].reason_code == "upstream_mutation"
    assert records[0].candidate.upstream_identity["commit"] == "b" * 40
    assert store.cursor("synthetic-runtime") == {"page": 3}


def test_discovery_failure_preserves_cursor_and_uses_stable_reason() -> None:
    class BrokenProvider:
        def discover(self, family, cursor=None):
            raise DiscoveryError(
                "discovery_unavailable", {"provider": "signed-http-index"}
            )

    store = InMemoryCandidateStore()
    store.set_cursor("synthetic-runtime", {"page": 4})
    service = CandidateService(
        providers={"signed-http-index": BrokenProvider()}, store=store
    )

    with pytest.raises(DiscoveryError) as error:
        service.poll(_family())

    assert error.value.reason_code == "discovery_unavailable"
    assert store.cursor("synthetic-runtime") == {"page": 4}
