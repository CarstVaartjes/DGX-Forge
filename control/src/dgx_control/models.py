"""Operational database models; Git remains definition authority."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    targets: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    status_reason: Mapped[str | None] = mapped_column(Text)
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reconciliation_id: Mapped[str | None] = mapped_column(
        ForeignKey("reconciliations.id"), unique=True, index=True
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fence: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    lease_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    base_commit: Mapped[str | None] = mapped_column(String(128))
    targets: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    graph: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {
            "base_commit": "",
            "nodes": [],
            "schema_version": 1,
            "targets": [],
        },
        server_default='{"base_commit":"","nodes":[],"schema_version":1,"targets":[]}',
    )
    graph_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="5c061eb8dfce0a3f2bcbfbf06cb71d695c33e8f4269e17bfe5cd1cda0054cdc5",
        server_default="5c061eb8dfce0a3f2bcbfbf06cb71d695c33e8f4269e17bfe5cd1cda0054cdc5",
    )
    plan_digest: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )
    resolved_plan: Mapped[dict[str, object] | None] = mapped_column(JSON)
    current_phase: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy", server_default="legacy"
    )
    route_withdrawal_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    completion_generation: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ReconciliationCompletionGeneration(Base):
    __tablename__ = "reconciliation_completion_generation"
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ReconciliationOperation(Base):
    __tablename__ = "reconciliation_operations"
    __table_args__ = (
        UniqueConstraint(
            "reconciliation_id",
            "graph_operation_id",
            "role",
            name="uq_reconciliation_operation_graph_role",
        ),
        CheckConstraint(
            "length(graph_operation_id) BETWEEN 1 AND 128",
            name="ck_reconciliation_operations_graph_operation_id_length",
        ),
        CheckConstraint(
            "role IN ('primary', 'compensation')",
            name="ck_reconciliation_operations_role",
        ),
        CheckConstraint(
            "state IN ('planned', 'queued', 'running', 'succeeded', "
            "'accepted', 'failed', 'waiting-for-operator', 'compensating', "
            "'compensated', 'uncertain')",
            name="ck_reconciliation_operations_state",
        ),
        CheckConstraint(
            "length(expected_payload_digest) = 64",
            name="ck_reconciliation_operations_expected_payload_digest_length",
        ),
        CheckConstraint(
            "result_digest IS NULL OR length(result_digest) = 64",
            name="ck_reconciliation_operations_result_digest_length",
        ),
        CheckConstraint(
            "evidence_digest IS NULL OR length(evidence_digest) = 64",
            name="ck_reconciliation_operations_evidence_digest_length",
        ),
        CheckConstraint(
            "compensated_graph_operation_id IS NULL OR "
            "length(compensated_graph_operation_id) BETWEEN 1 AND 128",
            name="ck_reconciliation_operations_compensated_id_length",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    graph_operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_operations.id"), unique=True, index=True
    )
    expected_payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compensated_graph_operation_id: Mapped[str | None] = mapped_column(String(128))


class ReconciliationCancellation(Base):
    """Durable operator intent advanced independently of process lifetime."""

    __tablename__ = "reconciliation_cancellations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('requested', 'withdrawal-pending', 'withdrawn', "
            "'processing', 'compensating', 'completed', "
            "'waiting-for-operator')",
            name="ck_reconciliation_cancellations_state",
        ),
        CheckConstraint(
            "length(reason) BETWEEN 1 AND 1024",
            name="ck_reconciliation_cancellations_reason_length",
        ),
    )
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RoutePublication(Base):
    __tablename__ = "route_publications"
    __table_args__ = (
        CheckConstraint(
            "state IN ('withdrawal-pending', 'routes-withdrawn', "
            "'publication-pending', 'completed', 'failed')",
            name="ck_route_publications_state",
        ),
        CheckConstraint(
            "generation IS NULL OR generation >= 0",
            name="ck_route_publications_generation",
        ),
        CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_route_publications_plan_digest_length",
        ),
        CheckConstraint(
            "evidence_digest IS NULL OR length(evidence_digest) = 64",
            name="ck_route_publications_evidence_digest_length",
        ),
        CheckConstraint(
            "route_digest IS NULL OR length(route_digest) = 64",
            name="ck_route_publications_route_digest_length",
        ),
        CheckConstraint(
            "litellm_digest IS NULL OR length(litellm_digest) = 64",
            name="ck_route_publications_litellm_digest_length",
        ),
        CheckConstraint(
            "bundle_digest IS NULL OR length(bundle_digest) = 64",
            name="ck_route_publications_bundle_digest_length",
        ),
        CheckConstraint(
            "activation_marker_digest IS NULL OR "
            "length(activation_marker_digest) = 64",
            name="ck_route_publications_activation_marker_digest_length",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR "
            "(lease_issued_at IS NOT NULL AND lease_expires_at > lease_issued_at)",
            name="ck_route_publications_lease_window",
        ),
    )
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    generation: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, index=True
    )
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    route_digest: Mapped[str | None] = mapped_column(String(64))
    litellm_digest: Mapped[str | None] = mapped_column(String(64))
    bundle_digest: Mapped[str | None] = mapped_column(String(64))
    activation_marker: Mapped[dict[str, object] | None] = mapped_column(JSON)
    activation_marker_digest: Mapped[str | None] = mapped_column(String(64))
    lease_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoutePublicationOwner(Base):
    """Singleton authority for the one global LiteLLM activation marker."""

    __tablename__ = "route_publication_owner"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1",
            name="ck_route_publication_owner_singleton",
        ),
        CheckConstraint(
            "owner_generation >= 0",
            name="ck_route_publication_owner_generation",
        ),
    )
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reconciliation_id: Mapped[str | None] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="SET NULL"),
        unique=True,
    )
    owner_generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subject: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentNode(Base):
    __tablename__ = "agent_nodes"
    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    platform_version: Mapped[str | None] = mapped_column(String(32))
    build_digest: Mapped[str | None] = mapped_column(String(71))
    active_slot: Mapped[str | None] = mapped_column(String(1))
    agent_sha256: Mapped[str | None] = mapped_column(String(64))
    supervisor_generation: Mapped[int | None] = mapped_column(Integer)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentCertificate(Base):
    __tablename__ = "agent_certificates"
    __table_args__ = (UniqueConstraint("node_id", "generation"),)
    serial: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("agent_nodes.node_id"), nullable=False, index=True)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    certificate_pem: Mapped[str | None] = mapped_column(Text)
    chain_pem: Mapped[str | None] = mapped_column(Text)
    csr_public_key_fingerprint: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ca_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentPresence(Base):
    """Latest authenticated management address for one active agent node."""

    __tablename__ = "agent_presence"
    __table_args__ = (
        CheckConstraint(
            "length(management_address) BETWEEN 2 AND 45",
            name="ck_agent_presence_management_address_length",
        ),
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    certificate_serial: Mapped[str] = mapped_column(
        ForeignKey("agent_certificates.serial"), nullable=False, index=True
    )
    certificate_fingerprint: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    management_address: Mapped[str] = mapped_column(String(45), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AgentCertificateRotation(Base):
    __tablename__ = "agent_certificate_rotations"
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    source_serial: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    csr_pem: Mapped[str] = mapped_column(Text, nullable=False)
    csr_public_key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentIssuedCertificateRevocation(Base):
    """Node-independent recovery evidence for a post-issuance CA revocation."""

    __tablename__ = "agent_issued_certificate_revocations"
    serial: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider_request_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ca_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEnrollmentGrant(Base):
    __tablename__ = "agent_enrollment_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEnrollment(Base):
    __tablename__ = "agent_enrollments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    grant_id: Mapped[str] = mapped_column(
        ForeignKey("agent_enrollment_grants.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    csr_pem: Mapped[str] = mapped_column(Text, nullable=False)
    csr_public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    csr_public_key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    host_key_fingerprint: Mapped[str] = mapped_column(String(512), nullable=False)
    hardware_fingerprint: Mapped[str] = mapped_column(String(512), nullable=False)
    agent_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decision_actor: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    certificate_pem: Mapped[str | None] = mapped_column(Text)
    chain_pem: Mapped[str | None] = mapped_column(Text)
    certificate_serial: Mapped[str | None] = mapped_column(String(128), unique=True)
    certificate_fingerprint: Mapped[str | None] = mapped_column(String(128), unique=True)
    certificate_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    certificate_not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentOperation(Base):
    __tablename__ = "agent_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    parent_job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("agent_nodes.node_id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_disposition: Mapped[str | None] = mapped_column(String(32))
    retry_disposition_attempt: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentOperationAttempt(Base):
    __tablename__ = "agent_operation_attempts"
    __table_args__ = (UniqueConstraint("operation_id", "attempt"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_id: Mapped[str] = mapped_column(ForeignKey("agent_operations.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fence: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    lease_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    agent_certificate_serial: Mapped[str] = mapped_column(ForeignKey("agent_certificates.serial"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
