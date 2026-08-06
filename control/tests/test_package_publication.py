from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from dgx_control.package_publication import (
    PackagePublicationService,
    PublicationError,
)

LOCK = b'{"family_id":"future-stack","schema_version":1}'
COMMIT = "a" * 40


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
