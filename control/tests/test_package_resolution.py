from __future__ import annotations

import hashlib

from vonk_agent_protocol import PackageReleaseLock
from vonk_control.package_discovery import DiscoveryCandidate, InMemoryCandidateStore
from vonk_control.package_resolution import PackageResolver

from cluster_profiles.workload_packages import PackageFamily


def _family(*, with_dependency: bool = False) -> PackageFamily:
    return PackageFamily.load(
        {
            "schema_version": 1,
            "family_id": "synthetic-runtime",
            "source": {
                "provider": "git",
                "locator": "https://git.example/project.git",
                "policy_refs": [],
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
                "dependencies": (
                    [
                        {
                            "family_id": "shared",
                            "release_digest_binding": "dependencies.shared",
                        }
                    ]
                    if with_dependency
                    else []
                ),
            },
            "policy": {"required_evidence": ["checksum"], "license_policy_refs": []},
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


def _component(name: str = "runtime", digest: str = "1" * 64) -> dict[str, object]:
    return {
        "name": name,
        "kind": "artifact",
        "media_type": "application/octet-stream",
        "sources": [
            {"provider": "https", "url": "https://packages.example/runtime.bin"}
        ],
        "digest": "sha256:" + digest,
        "size": 1024,
        "unpacked_size": 1024,
        "platforms": ["linux/arm64"],
        "materialization": {"method": "file"},
        "evidence": [{"kind": "checksum", "digest": "sha256:" + "2" * 64}],
    }


def _candidate(
    *, components=None, dependencies=None, version="1.2.3"
) -> DiscoveryCandidate:
    metadata = {
        "release": {"version": version, "channel": "stable", "commit": "a" * 40},
        "components": components if components is not None else [_component()],
        "adapter": {
            "name": "adapter",
            "kind": "adapter",
            "media_type": "application/vnd.vonk-forge.workload-adapter.v1",
            "sources": [
                {"provider": "https", "url": "https://packages.example/adapter"}
            ],
            "digest": "sha256:" + "3" * 64,
            "size": 64,
            "unpacked_size": 64,
            "platforms": ["linux/arm64"],
            "materialization": {"method": "executable"},
            "evidence": [{"kind": "checksum", "digest": "sha256:" + "4" * 64}],
        },
        "dependencies": dependencies or {},
        "provenance": [{"kind": "slsa", "digest": "sha256:" + "5" * 64}],
    }
    return DiscoveryCandidate.create(
        family_id="synthetic-runtime",
        release_key=version,
        upstream_version=version,
        channel="stable",
        published_at="2026-08-06T10:00:00Z",
        upstream_identity={
            "provider": "git",
            "repository": "https://git.example/project.git",
            "commit": "a" * 40,
        },
        metadata=metadata,
    )


def _resolver(candidate: DiscoveryCandidate) -> tuple[PackageResolver, PackageFamily]:
    store = InMemoryCandidateStore()
    store.upsert(candidate)
    return PackageResolver(store), _family()


def test_resolver_emits_canonical_release_lock_and_is_repeat_deterministic() -> None:
    resolver, family = _resolver(_candidate())
    first = resolver.resolve(next(iter(resolver.candidates())).id, family, {})
    second = resolver.resolve(next(iter(resolver.candidates())).id, family, {})

    assert first.state == "resolved"
    assert isinstance(first.lock, PackageReleaseLock)
    assert first.lock.digest == hashlib.sha256(first.lock.canonical_bytes).hexdigest()
    assert first.lock.digest == second.lock.digest
    assert first.lock.dependency_digests == ()
    assert first.lock.compatibility["backends"] == ("native",)


def test_resolver_rejects_missing_checksum_as_structured_unsupported_reason() -> None:
    bad = _component()
    bad.pop("digest")
    resolver, family = _resolver(_candidate(components=[bad]))

    result = resolver.resolve(next(iter(resolver.candidates())).id, family, {})

    assert result.state == "unsupported"
    assert result.lock is None
    assert result.reason_code == "incomplete_checksum_metadata"
    assert len(result.detail) <= 8


def test_resolver_rejects_dependency_cycles_and_missing_dependencies() -> None:
    dependency = "a" * 64
    resolver, family = _resolver(_candidate(dependencies={"shared": dependency}))
    family = _family(with_dependency=True)
    family_doc = family
    result = resolver.resolve(
        next(iter(resolver.candidates())).id, family_doc, {"shared": dependency}
    )
    assert result.state == "unsupported"
    assert result.reason_code == "dependency_missing"


def test_resolver_orders_semver_candidates_without_guessing_opaque_versions() -> None:
    first = _candidate(version="1.9.0")
    second = _candidate(version="1.10.0")
    store = InMemoryCandidateStore()
    store.upsert(first)
    store.upsert(second)
    resolver = PackageResolver(store)

    assert [
        item.upstream_version
        for item in resolver.select_latest("synthetic-runtime", _family())
    ] == ["1.10.0", "1.9.0"]

    opaque_family = _family()
    document = opaque_family.canonical_bytes
    assert document
