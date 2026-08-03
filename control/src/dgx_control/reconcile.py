"""Adapters that submit reviewed changes and enqueue reconciliations."""

from __future__ import annotations

from dataclasses import asdict

from .git_policy import GitPolicy
from .jobs import JobService
from .proposals import ProposalService


class ChangeService:
    def __init__(self, proposals: ProposalService, policy: GitPolicy) -> None:
        self._proposals = proposals
        self._policy = policy

    def submit(self, digest: str, actor: str, request_id: str) -> dict[str, object]:
        preview = self._proposals.apply(digest)
        return asdict(self._policy.submit(preview, actor=actor, request_id=request_id))


class ReconciliationService:
    def __init__(self, proposals: ProposalService, jobs: JobService) -> None:
        self._proposals = proposals
        self._jobs = jobs

    def enqueue(self, digest: str, actor: str, request_id: str) -> dict[str, object]:
        preview = self._proposals.apply(digest)
        job = self._jobs.enqueue(
            "reconcile",
            actor,
            preview.base_commit,
            list(preview.affected_documents),
            {"proposal_digest": digest},
            request_id=request_id,
        )
        return {"job_id": job.id, "state": job.state, "base_commit": preview.base_commit}
