from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from vonk_control.package_publication import (
    PackagePublicationService,
    PublicationError,
)

COMMIT = "a" * 40


def _lock_bytes() -> bytes:
    component = {
        "name": "payload",
        "kind": "artifact",
        "media_type": "application/octet-stream",
        "sources": [{"provider": "https", "url": "https://example.invalid/a"}],
        "digest": "sha256:" + "1" * 64,
        "size": 1,
        "unpacked_size": 1,
        "platforms": ["linux/arm64"],
        "materialization": {"method": "file"},
        "evidence": [],
    }
    document = {
        "schema_version": 1,
        "family_id": "future-stack",
        "upstream_version": "1",
        "upstream_identity": {
            "provider": "git",
            "repository": "https://example.invalid/repo",
            "commit": "b" * 40,
        },
        "components": [component],
        "dependency_digests": [],
        "adapter": component | {"name": "adapter", "kind": "adapter", "materialization": {"method": "executable"}},
        "adapter_abi": 1,
        "compatibility": {
            "architectures": ["arm64"],
            "operating_systems": ["linux"],
            "required_capabilities": [],
            "minimum_storage_bytes": 1,
        },
        "validation": [],
        "provenance": [],
        "resolver": {"name": "resolver", "version": 1},
        "resource_envelope": {
            "schema_version": 1,
            "per_node": {field: 1 for field in (
                "download_bytes", "installed_bytes", "transient_bytes", "output_bytes",
                "host_memory_bytes", "resident_memory_bytes", "auxiliary_memory_bytes",
                "activation_memory_bytes", "workspace_memory_bytes", "gpu_memory_bytes",
                "gpu_count", "cpu_millicores", "kv_cache_base_bytes", "kv_cache_per_token_bytes",
            )} | {"auxiliary_memory_bytes": 0, "activation_memory_bytes": 0, "workspace_memory_bytes": 0},
            "aggregate": {field: 1 for field in (
                "download_bytes", "installed_bytes", "transient_bytes", "output_bytes",
                "host_memory_bytes", "resident_memory_bytes", "auxiliary_memory_bytes",
                "activation_memory_bytes", "workspace_memory_bytes", "gpu_memory_bytes",
                "gpu_count", "cpu_millicores", "kv_cache_base_bytes", "kv_cache_per_token_bytes",
            )} | {"auxiliary_memory_bytes": 0, "activation_memory_bytes": 0, "workspace_memory_bytes": 0},
            "required_sparks": 1,
            "topology": "single",
            "world_size": 1,
            "ranks": [{"rank": 0, "role": "primary"}],
            "fabric": {"kind": "none", "min_bandwidth_mbps": 0},
            "measurement": "declared",
            "evidence": [{"kind": "capacity", "digest": "sha256:" + "c" * 64}],
        },
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


LOCK = _lock_bytes()


def _candidate() -> dict[str, object]:
    return {
        "id": "candidate-1",
        "state": "resolved",
        "family_id": "future-stack",
        "release_digest": hashlib.sha256(LOCK).hexdigest(),
        "lock_bytes": LOCK,
        "policy": {"mode": "manual"},
        "validation_state": "passed",
        "evidence": {
            "lock_digest": hashlib.sha256(LOCK).hexdigest(),
            "provenance_digest": "b" * 64,
            "sbom_digest": "c" * 64,
            "schema_version": 1,
        },
        "evidence_digests": ("b" * 64, "c" * 64),
    }


def test_preview_rejects_release_without_resource_envelope() -> None:
    document = json.loads(LOCK)
    document.pop("resource_envelope")
    legacy_lock = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    candidate = _candidate()
    candidate.update(
        {
            "lock_bytes": legacy_lock,
            "release_digest": hashlib.sha256(legacy_lock).hexdigest(),
        }
    )
    service = PackagePublicationService(
        candidate_loader=lambda candidate_id: candidate,
        head=lambda: COMMIT,
        commit_eligible=lambda commit: commit == COMMIT,
        validation_loader=lambda candidate_id: {"state": "passed", "digest": "d" * 64},
        clock=lambda: datetime(2026, 8, 6, tzinfo=UTC),
    )

    with pytest.raises(PublicationError, match="resource envelope"):
        service.preview("candidate-1", COMMIT)


def test_preview_binds_candidate_commit_policy_and_evidence() -> None:
    service = PackagePublicationService(
        candidate_loader=lambda candidate_id: _candidate(),
        head=lambda: COMMIT,
        commit_eligible=lambda commit: commit == COMMIT,
        validation_loader=lambda candidate_id: {"state": "passed", "digest": "d" * 64},
        clock=lambda: datetime(2026, 8, 6, tzinfo=UTC),
    )

    preview = service.preview("candidate-1", COMMIT)

    assert preview.release_digest == hashlib.sha256(LOCK).hexdigest()
    assert preview.base_commit == COMMIT
    assert preview.evidence_digests == ("b" * 64, "c" * 64)
    assert preview.digest == hashlib.sha256(preview.canonical_bytes).hexdigest()


def test_manual_promotion_requires_operator_and_publishes_once() -> None:
    published: list[tuple[bytes, str]] = []
    service = PackagePublicationService(
        candidate_loader=lambda candidate_id: _candidate(),
        head=lambda: COMMIT,
        publisher=lambda lock, commit, evidence: published.append((lock, commit))
        or {"digest": hashlib.sha256(lock).hexdigest(), "length": len(lock)},
        validation_loader=lambda candidate_id: {"state": "passed", "digest": "d" * 64},
        clock=lambda: datetime(2026, 8, 6, tzinfo=UTC),
    )
    preview = service.preview("candidate-1", COMMIT)
    result = service.promote(preview.digest, "admin@example.invalid")
    assert result["digest"] == hashlib.sha256(LOCK).hexdigest()
    assert published == [(LOCK, COMMIT)]
    assert service.promote(preview.digest, "admin@example.invalid") == result
    assert len(published) == 1


def test_promotion_rejects_stale_base_changed_lock_builder_and_automation() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    candidate = _candidate()
    head = [COMMIT]
    service = PackagePublicationService(
        candidate_loader=lambda candidate_id: candidate,
        head=lambda: head[0],
        publisher=lambda *_args: {"digest": "a" * 64},
        validation_loader=lambda candidate_id: {"state": "passed", "digest": "d" * 64},
        clock=lambda: now,
    )
    preview = service.preview("candidate-1", COMMIT)
    head[0] = "b" * 40
    with pytest.raises(PublicationError, match="stale"):
        service.promote(preview.digest, "admin@example.invalid")

    head[0] = COMMIT
    candidate["lock_bytes"] = b'{"changed":true}'
    with pytest.raises(PublicationError, match="changed"):
        service.promote(preview.digest, "admin@example.invalid")

    candidate.update({"lock_bytes": LOCK, "builder_identity": "builder://ci"})
    preview = service.preview("candidate-1", COMMIT)
    with pytest.raises(PublicationError, match="builder"):
        service.promote(preview.digest, "builder://ci")

    candidate["policy"] = {"mode": "manual"}
    with pytest.raises(PublicationError, match="manual"):
        service.promote(preview.digest, "automation://release-bot")
