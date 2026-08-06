"""Preview and promotion gates for independently versioned workload releases."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from .workload_trust import TrustedWorkloadTarget, WorkloadTrustError


class PublicationError(ValueError):
    """A workload publication crossed an authority, freshness, or policy gate."""


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


def _lock_bytes(candidate: object) -> bytes:
    value = _get(candidate, "lock_bytes", "release_lock_bytes", "lock")
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return value
    if value is None:
        value = _get(candidate, "release_lock")
    canonical = _get(value, "canonical_bytes") if value is not None else None
    if callable(canonical):
        canonical = canonical()
    if isinstance(canonical, str):
        canonical = canonical.encode()
    if isinstance(canonical, bytes):
        return canonical
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()
    raise PublicationError("candidate has no canonical release lock")


def _digest(value: object) -> str:
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _now(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class PublicationPreview:
    digest: str
    candidate_id: str
    release_digest: str
    base_commit: str
    policy_digest: str
    evidence_digests: tuple[str, ...]
    expires_at: datetime
    validation_digest: str
    lock_bytes: bytes
    canonical_bytes: bytes
    proposal_digest: str | None = None


class PackagePublicationService:
    """Bind a validated candidate to Git and authorize it in workload TUF.

    The service only asks the publisher to sign a lock after all preview fields
    have been reloaded.  ``publisher`` may be ``WorkloadTrustPublisher`` or a
    narrow callable used by tests/embedding applications.
    """

    def __init__(
        self,
        candidate_loader: Callable[[str], object],
        *,
        head: Callable[[], str] | None = None,
        commit_eligible: Callable[[str], bool] | None = None,
        publisher: object | Callable[[bytes, str, Mapping[str, object]], object] | None = None,
        proposal_service: object | None = None,
        validation_loader: Callable[[str], object] | None = None,
        policy_loader: Callable[[str], object] | None = None,
        evidence_loader: Callable[[str], Mapping[str, object]] | None = None,
        failure_count_loader: Callable[[str], int] | None = None,
        clock: Callable[[], datetime] | None = None,
        preview_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        if not callable(candidate_loader):
            raise TypeError("candidate loader is required")
        if preview_ttl <= timedelta(0) or preview_ttl > timedelta(hours=1):
            raise ValueError("publication preview TTL is invalid")
        self._candidate_loader = candidate_loader
        self._head = head or (lambda: "")
        self._commit_eligible = commit_eligible or (lambda _commit: True)
        self._publisher = publisher
        self._proposal_service = proposal_service
        self._validation_loader = validation_loader
        self._policy_loader = policy_loader
        self._evidence_loader = evidence_loader
        self._failure_count_loader = failure_count_loader or (lambda _family: 0)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._preview_ttl = preview_ttl
        self._previews: dict[str, PublicationPreview] = {}
        self._published: dict[str, object] = {}

    def _candidate(self, candidate_id: str) -> object:
        candidate = self._candidate_loader(candidate_id)
        if candidate is None:
            raise PublicationError("candidate is unavailable")
        if _get(candidate, "state", default="resolved") not in {
            "resolved",
            "validated",
            "promotion-ready",
        }:
            raise PublicationError("candidate is not resolved")
        return candidate

    def _policy(self, candidate_id: str, candidate: object) -> Mapping[str, object]:
        policy = _get(candidate, "policy")
        if policy is None and self._policy_loader is not None:
            policy = self._policy_loader(candidate_id)
        return policy if isinstance(policy, Mapping) else MappingProxyType({"mode": "manual"})

    def _validation(self, candidate_id: str, candidate: object) -> tuple[str, str]:
        value = _get(candidate, "validation", "validation_state", default=None)
        if value is None and self._validation_loader is not None:
            value = self._validation_loader(candidate_id)
        state = _get(value, "state", default=value if isinstance(value, str) else None)
        if state != "passed":
            raise PublicationError("validation evidence is incomplete")
        digest = _get(value, "digest", "evidence_digest", default=None)
        if not isinstance(digest, str) or len(digest) != 64:
            digest = _digest(value)
        return "passed", digest

    def _evidence(self, candidate_id: str, candidate: object, lock_digest: str) -> tuple[dict[str, object], tuple[str, ...]]:
        raw = _get(candidate, "evidence", default=None)
        if raw is None and self._evidence_loader is not None:
            raw = self._evidence_loader(candidate_id)
        if not isinstance(raw, Mapping):
            raise PublicationError("publication evidence is incomplete")
        evidence = dict(raw)
        lock_evidence_digest = evidence.get("lock_digest")
        if lock_evidence_digest is not None and lock_evidence_digest != lock_digest:
            raise PublicationError("publication evidence lock digest changed")
        evidence["lock_digest"] = lock_digest
        if evidence.get("schema_version") != 1:
            raise PublicationError("publication evidence schema is invalid")
        for name in ("provenance_digest", "sbom_digest"):
            value = evidence.get(name)
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise PublicationError("publication evidence is incomplete")
        digests = _get(candidate, "evidence_digests", default=None)
        if isinstance(digests, str):
            digest_values = (digests,)
        elif isinstance(digests, Sequence):
            digest_values = tuple(str(item) for item in digests)
        else:
            digest_values = tuple(
                str(value)
                for key, value in sorted(evidence.items())
                if key.endswith("_digest") and key != "lock_digest" and isinstance(value, str)
            )
        return evidence, tuple(sorted(set(digest_values)))

    def preview(self, candidate_id: str, commit: str) -> PublicationPreview:
        if not isinstance(commit, str) or len(commit) not in {40, 64}:
            raise PublicationError("workload Git commit is invalid")
        candidate = self._candidate(candidate_id)
        lock = _lock_bytes(candidate)
        release_digest = hashlib.sha256(lock).hexdigest()
        candidate_digest = _get(candidate, "release_digest")
        if isinstance(candidate_digest, str) and candidate_digest != release_digest:
            raise PublicationError("candidate lock bytes changed")
        if not self._commit_eligible(commit):
            raise PublicationError("workload Git commit is not eligible")
        _, validation_digest = self._validation(candidate_id, candidate)
        _evidence_value, evidence_digests = self._evidence(candidate_id, candidate, release_digest)
        policy = self._policy(candidate_id, candidate)
        policy_digest = _digest(policy)
        proposal_digest: str | None = None
        proposal = self._proposal_service
        if proposal is not None and hasattr(proposal, "preview"):
            # ProposalService accepts typed DocumentChange objects; callers that
            # supply it may provide their own desired-state document callback.
            changes = _get(candidate, "proposal_changes", default=())
            try:
                result = proposal.preview("workload-publisher", commit, changes)
            except (TypeError, ValueError) as error:
                raise PublicationError("workload proposal preview is invalid") from error
            proposal_digest = _get(result, "digest")
        now = _now(self._clock())
        expires_at = now + self._preview_ttl
        value = {
            "base_commit": commit,
            "candidate_id": candidate_id,
            "evidence_digests": list(evidence_digests),
            "expires_at": expires_at.isoformat(),
            "policy_digest": policy_digest,
            "proposal_digest": proposal_digest,
            "release_digest": release_digest,
            "validation_digest": validation_digest,
        }
        canonical = _canonical(value)
        preview = PublicationPreview(
            digest=hashlib.sha256(canonical).hexdigest(),
            candidate_id=candidate_id,
            release_digest=release_digest,
            base_commit=commit,
            policy_digest=policy_digest,
            evidence_digests=evidence_digests,
            expires_at=expires_at,
            validation_digest=validation_digest,
            lock_bytes=lock,
            canonical_bytes=canonical,
            proposal_digest=proposal_digest,
        )
        self._previews[preview.digest] = preview
        return preview

    def _publish(self, lock: bytes, commit: str, evidence: Mapping[str, object]) -> object:
        if self._publisher is None:
            return TrustedWorkloadTarget(
                digest=hashlib.sha256(lock).hexdigest(),
                length=len(lock),
                git_commit=commit,
                tuf_snapshot_version=0,
            )
        try:
            if callable(self._publisher):
                return self._publisher(lock, commit, evidence)
            return self._publisher.publish(lock, commit, evidence)
        except (PublicationError, WorkloadTrustError):
            raise
        except Exception as error:
            raise PublicationError("workload TUF publication failed") from error

    def promote(self, preview_digest: str, actor: str) -> object:
        try:
            preview = self._previews[preview_digest]
        except KeyError:
            raise PublicationError("publication preview is unknown") from None
        if preview_digest in self._published:
            return self._published[preview_digest]
        now = _now(self._clock())
        if now >= preview.expires_at:
            raise PublicationError("publication preview expired")
        candidate = self._candidate(preview.candidate_id)
        lock = _lock_bytes(candidate)
        if hashlib.sha256(lock).hexdigest() != preview.release_digest or lock != preview.lock_bytes:
            raise PublicationError("candidate lock bytes changed")
        if self._head() != preview.base_commit:
            raise PublicationError("publication preview is stale")
        if not self._commit_eligible(preview.base_commit):
            raise PublicationError("workload Git commit is not eligible")
        if (
            preview.proposal_digest is not None
            and self._proposal_service is not None
            and hasattr(self._proposal_service, "apply")
        ):
            try:
                self._proposal_service.apply(preview.proposal_digest)
            except Exception as error:
                raise PublicationError("workload proposal preview is stale") from error
        policy = self._policy(preview.candidate_id, candidate)
        mode = policy.get("mode", "manual")
        if not isinstance(actor, str) or not actor.strip():
            raise PublicationError("publication actor is required")
        if actor.startswith("automation://"):
            if mode != "automatic":
                raise PublicationError("manual promotion requires operator approval")
            if actor != policy.get("automation_identity"):
                raise PublicationError("automation identity is not authorized")
            family_id = str(_get(candidate, "family_id", default=""))
            budget = policy.get("failure_budget")
            if isinstance(budget, int) and self._failure_count_loader(family_id) >= budget:
                raise PublicationError("automation failure budget is exhausted")
        builder = _get(candidate, "builder_identity", "builder_id")
        if isinstance(builder, str) and builder == actor:
            raise PublicationError("builder identity cannot publish workload targets")
        self._validation(preview.candidate_id, candidate)
        evidence, _ = self._evidence(preview.candidate_id, candidate, preview.release_digest)
        result = self._publish(lock, preview.base_commit, evidence)
        self._published[preview_digest] = result
        return result

    def rollback(self, family_id: str, release_digest: str, actor: str) -> object:
        """Select an exact previously trusted release for operator rollback.

        Rollback does not invent a new lock or call an agent update.  The caller
        supplies a digest that has already been authorized by workload TUF;
        ``rollback_publisher`` can be provided by the deployment reconciler.
        """
        if not isinstance(family_id, str) or not family_id.strip():
            raise PublicationError("rollback family is invalid")
        if len(release_digest) != 64:
            raise PublicationError("rollback release digest is invalid")
        if not actor.strip():
            raise PublicationError("rollback actor is required")
        raise PublicationError("rollback selection requires a trusted release index")

    def select_rollback(
        self,
        family_id: str,
        release_digest: str,
        releases: Mapping[str, object],
    ) -> object:
        """Return an exact prior trusted release from an immutable release index."""

        if not isinstance(family_id, str) or not family_id.strip():
            raise PublicationError("rollback family is invalid")
        if not isinstance(release_digest, str) or len(release_digest) != 64:
            raise PublicationError("rollback release digest is invalid")
        if not isinstance(releases, Mapping):
            raise PublicationError("rollback release index is invalid")
        target = releases.get(release_digest)
        if target is None:
            raise PublicationError("rollback release is not trusted")
        target_family = _get(target, "family_id", default=family_id)
        target_digest = _get(target, "digest", "release_digest", default=release_digest)
        if target_family != family_id or target_digest != release_digest:
            raise PublicationError("rollback release identity is invalid")
        return target


__all__ = ["PackagePublicationService", "PublicationError", "PublicationPreview"]
