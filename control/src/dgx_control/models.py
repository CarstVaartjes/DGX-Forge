"""Operational database models; Git remains definition authority."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _lower_hex(column: str, length: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = {length} AND {column} = lower({column}) AND "
        f"length({remainder}) = 0"
    )


def _nullable_lower_hex(column: str, length: int) -> str:
    return f"{column} IS NULL OR ({_lower_hex(column, length)})"


def _uuid_shape(column: str) -> str:
    compact = f"replace({column}, '-', '')"
    return (
        f"length({column}) = 36 AND substr({column}, 9, 1) = '-' AND "
        f"substr({column}, 14, 1) = '-' AND substr({column}, 19, 1) = '-' AND "
        f"substr({column}, 24, 1) = '-' AND ({_lower_hex(compact, 32)})"
    )


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


class ControlProcessHeartbeat(Base):
    """A completed scheduler loop bound to one immutable control generation."""

    __tablename__ = "control_process_heartbeats"
    __table_args__ = (
        UniqueConstraint(
            "process_kind",
            "start_nonce",
            name="uq_control_process_heartbeats_process_start",
        ),
        CheckConstraint(
            "process_kind = 'worker'",
            name="ck_control_process_heartbeats_process_kind",
        ),
        CheckConstraint(
            "length(generation_id) BETWEEN 1 AND 128",
            name="ck_control_process_heartbeats_generation_id_length",
        ),
        CheckConstraint(
            "length(release_digest) = 71 AND "
            "substr(release_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(release_digest, 8, 64)', 64)})",
            name="ck_control_process_heartbeats_release_digest",
        ),
        CheckConstraint(
            "length(build_digest) = 71 AND "
            "substr(build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(build_digest, 8, 64)', 64)})",
            name="ck_control_process_heartbeats_build_digest",
        ),
        CheckConstraint(
            _lower_hex("start_nonce", 64),
            name="ck_control_process_heartbeats_start_nonce",
        ),
        CheckConstraint(
            "loop_sequence >= 1",
            name="ck_control_process_heartbeats_loop_sequence",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    process_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    generation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    release_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    build_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    start_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    loop_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


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
    __table_args__ = (
        CheckConstraint(
            "architecture IS NULL OR architecture IN ('linux-arm64', 'linux-x86_64')",
            name="ck_agent_nodes_architecture",
        ),
    )
    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    architecture: Mapped[str | None] = mapped_column(String(16))
    platform_version: Mapped[str | None] = mapped_column(String(32))
    build_digest: Mapped[str | None] = mapped_column(String(71))
    active_slot: Mapped[str | None] = mapped_column(String(1))
    agent_sha256: Mapped[str | None] = mapped_column(String(64))
    supervisor_generation: Mapped[int | None] = mapped_column(Integer)
    supervisor_ready_generation: Mapped[int | None] = mapped_column(Integer)
    self_test_passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    contact_certificate_serial: Mapped[str | None] = mapped_column(String(128))
    contact_observation_digest: Mapped[str | None] = mapped_column(String(64))
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NodeMutationLease(Base):
    """Exclusive durable ownership of one node's mutations and route state."""

    __tablename__ = "node_mutation_leases"
    __table_args__ = (
        CheckConstraint(
            "owner_kind IN ('update-rollout', 'reconciliation')",
            name="ck_node_mutation_leases_owner_kind",
        ),
        CheckConstraint(
            "state IN ('held', 'releasing')",
            name="ck_node_mutation_leases_state",
        ),
        CheckConstraint(
            _uuid_shape("owner_id"),
            name="ck_node_mutation_leases_owner_id_shape",
        ),
        CheckConstraint(
            _uuid_shape("fence"),
            name="ck_node_mutation_leases_fence_shape",
        ),
        CheckConstraint(
            "updated_at >= acquired_at",
            name="ck_node_mutation_leases_timestamp_order",
        ),
        Index(
            "ix_node_mutation_leases_owner",
            "owner_kind",
            "owner_id",
        ),
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), primary_key=True
    )
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    fence: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class UpdateRollout(Base):
    """Immutable platform-update plan and durable orchestration cursor."""

    __tablename__ = "update_rollouts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('planned', 'withdrawing', 'updating', 'soaking', "
            "'publishing', 'failure-publishing', 'compensating-withdrawal', "
            "'paused', 'rolling-back', "
            "'rollback-publishing', 'waiting-for-approval', 'completed', 'partial', 'failed')",
            name="ck_update_rollouts_state",
        ),
        CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_update_rollouts_plan_digest_length",
        ),
        CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_update_rollouts_release_digest_length",
        ),
        CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_update_rollouts_fleet_digest_length",
        ),
        CheckConstraint(
            _lower_hex("topology_digest", 64),
            name="ck_update_rollouts_topology_digest_length",
        ),
        CheckConstraint(
            _lower_hex("agent_input_digest", 64),
            name="ck_update_rollouts_agent_input_digest_length",
        ),
        CheckConstraint(
            "length(target_build_digest) = 71 AND "
            "substr(target_build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(target_build_digest, 8, 64)', 64)})",
            name="ck_update_rollouts_target_build_digest",
        ),
        CheckConstraint(
            "current_batch >= 0",
            name="ck_update_rollouts_current_batch",
        ),
        CheckConstraint(
            "tuf_targets_version >= 1",
            name="ck_update_rollouts_tuf_targets_version",
        ),
        CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_update_rollouts_failure_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_update_rollouts_rollback_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("approval_evidence_digest", 64),
            name="ck_update_rollouts_approval_evidence_digest_length",
        ),
        CheckConstraint(
            "(state IN ('completed', 'partial') AND completed_at IS NOT NULL) OR "
            "(state NOT IN ('completed', 'partial') AND completed_at IS NULL)",
            name="ck_update_rollouts_completion_state",
        ),
        CheckConstraint(
            "(approval_at IS NULL AND approval_actor IS NULL AND "
            "approval_request_id IS NULL AND approval_reason IS NULL AND "
            "approval_evidence_digest IS NULL) OR "
            "(approval_at IS NOT NULL AND approval_actor IS NOT NULL AND "
            "approval_request_id IS NOT NULL AND approval_reason IS NOT NULL AND "
            "approval_evidence_digest IS NOT NULL)",
            name="ck_update_rollouts_approval_complete",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), unique=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    plan_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    fleet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    topology_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_platform_version: Mapped[str] = mapped_column(String(32), nullable=False)
    target_build_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    tuf_targets_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    update_admin_grant: Mapped[dict[str, object] | None] = mapped_column(JSON)
    rollback_admin_grant: Mapped[dict[str, object] | None] = mapped_column(JSON)
    plan: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    current_batch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    soak_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    failure_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    rollback_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    approval_actor: Mapped[str | None] = mapped_column(String(200))
    approval_request_id: Mapped[str | None] = mapped_column(
        String(36), unique=True
    )
    approval_reason: Mapped[str | None] = mapped_column(Text)
    approval_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UpdateRolloutNode(Base):
    """Per-node update progress, operation bindings, and acceptance evidence."""

    __tablename__ = "update_rollout_nodes"
    __table_args__ = (
        UniqueConstraint(
            "rollout_id",
            "node_id",
            name="uq_update_rollout_nodes_rollout_node",
        ),
        UniqueConstraint(
            "rollout_id",
            "batch_index",
            "node_order",
            name="uq_update_rollout_nodes_batch_order",
        ),
        CheckConstraint(
            "state IN ('offline-pending', 'pending', 'routes-withdrawn', 'updating', 'soaking', "
            "'accepted', 'failed', 'rolling-back', 'rolled-back')",
            name="ck_update_rollout_nodes_state",
        ),
        CheckConstraint(
            "batch_index >= -1 AND node_order >= 0",
            name="ck_update_rollout_nodes_order",
        ),
        CheckConstraint(
            _lower_hex("source_identity_digest", 64),
            name="ck_update_rollout_nodes_source_identity_digest_length",
        ),
        CheckConstraint(
            _lower_hex("target_artifact_digest", 64),
            name="ck_update_rollout_nodes_target_artifact_digest_length",
        ),
        CheckConstraint(
            "observed_build_digest IS NULL OR "
            "(length(observed_build_digest) = 71 AND "
            "substr(observed_build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(observed_build_digest, 8, 64)', 64)}))",
            name="ck_update_rollout_nodes_observed_build_digest",
        ),
        CheckConstraint(
            "observed_active_slot IS NULL OR observed_active_slot IN ('A', 'B')",
            name="ck_update_rollout_nodes_observed_active_slot",
        ),
        CheckConstraint(
            _nullable_lower_hex("route_withdrawal_evidence_digest", 64),
            name="ck_update_rollout_nodes_route_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("acceptance_evidence_digest", 64),
            name="ck_update_rollout_nodes_acceptance_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_update_rollout_nodes_failure_evidence_digest_length",
        ),
        CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_update_rollout_nodes_rollback_evidence_digest_length",
        ),
        CheckConstraint(
            "(state IN ('offline-pending', 'pending', 'routes-withdrawn') AND dispatch_at IS NULL "
            "AND activation_deadline IS NULL) OR "
            "(state IN ('updating', 'soaking', 'accepted', 'failed', "
            "'rolling-back', 'rolled-back') AND dispatch_at IS NOT NULL AND "
            "activation_deadline IS NOT NULL AND activation_deadline > dispatch_at)",
            name="ck_update_rollout_nodes_dispatch_window",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    rollout_id: Mapped[str] = mapped_column(
        ForeignKey("update_rollouts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id"), nullable=False, index=True
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_canary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_operations.id"), unique=True
    )
    rollback_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_operations.id"), unique=True
    )
    operation_history: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    source_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_artifact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_platform_version: Mapped[str | None] = mapped_column(String(32))
    observed_build_digest: Mapped[str | None] = mapped_column(String(71))
    observed_protocol_version: Mapped[int | None] = mapped_column(Integer)
    observed_active_slot: Mapped[str | None] = mapped_column(String(1))
    route_withdrawal_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    acceptance_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    failure_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    rollback_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    soak_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activation_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UpdateAuthorizationIntent(Base):
    """Immutable reserve/sign/queue binding for one privileged agent operation."""

    __tablename__ = "update_authorization_intents"
    __table_args__ = (
        CheckConstraint(
            "action IN ('agent.update', 'agent.rollback')",
            name="ck_update_authorization_intents_action",
        ),
        CheckConstraint(
            "state IN ('reserved', 'signed', 'queued', 'stale')",
            name="ck_update_authorization_intents_state",
        ),
        CheckConstraint(
            "source_slot IN ('A', 'B')",
            name="ck_update_authorization_intents_source_slot",
        ),
        CheckConstraint(
            _lower_hex("payload_digest", 64),
            name="ck_update_authorization_intents_payload_digest",
        ),
        CheckConstraint(
            _lower_hex("source_sha256", 64),
            name="ck_update_authorization_intents_source_sha256",
        ),
        CheckConstraint(
            _lower_hex("request_digest", 64),
            name="ck_update_authorization_intents_request_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("response_digest", 64),
            name="ck_update_authorization_intents_response_digest",
        ),
        CheckConstraint(
            _lower_hex("admin_grant_digest", 64),
            name="ck_update_authorization_intents_admin_grant_digest",
        ),
        CheckConstraint(
            "target_release_digest IS NULL OR "
            "(length(target_release_digest) = 71 AND "
            "substr(target_release_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(target_release_digest, 8, 64)', 64)}))",
            name="ck_update_authorization_intents_target_release_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("expected_tuf_target_sha256", 64),
            name="ck_update_authorization_intents_tuf_target_sha256",
        ),
        CheckConstraint(
            "(action = 'agent.update' AND target_release_digest IS NOT NULL AND "
            "expected_tuf_target_sha256 IS NOT NULL AND "
            "expected_tuf_targets_version IS NOT NULL AND "
            "expected_tuf_targets_version >= 1) OR "
            "(action = 'agent.rollback' AND target_release_digest IS NULL AND "
            "expected_tuf_target_sha256 IS NULL AND "
            "expected_tuf_targets_version IS NULL)",
            name="ck_update_authorization_intents_tuf_binding",
        ),
        CheckConstraint(
            "(state = 'reserved' AND signed_response IS NULL AND response_digest IS NULL "
            "AND queued_at IS NULL) OR "
            "(state = 'signed' AND signed_response IS NOT NULL AND response_digest IS NOT NULL "
            "AND queued_at IS NULL) OR "
            "(state = 'queued' AND signed_response IS NOT NULL AND response_digest IS NOT NULL "
            "AND queued_at IS NOT NULL) OR state = 'stale'",
            name="ck_update_authorization_intents_state_payload",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    rollout_id: Mapped[str] = mapped_column(
        ForeignKey("update_rollouts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rollout_node_id: Mapped[str] = mapped_column(
        ForeignKey("update_rollout_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id"), nullable=False, index=True
    )
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    fence: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    unsigned_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_slot: Mapped[str] = mapped_column(String(1), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    target_release_digest: Mapped[str | None] = mapped_column(String(71))
    expected_tuf_target_sha256: Mapped[str | None] = mapped_column(String(64))
    expected_tuf_targets_version: Mapped[int | None] = mapped_column(Integer)
    admin_grant: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    admin_grant_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_response: Mapped[dict[str, object] | None] = mapped_column(JSON)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageCandidate(Base):
    """A metadata-only observation of an upstream workload release.

    Candidate rows deliberately contain identities and bounded summaries only.
    The release lock, source credentials, and package bytes remain outside the
    operational database (in Git/TUF and the provider stores respectively).
    """

    __tablename__ = "package_candidates"
    __table_args__ = (
        CheckConstraint(
            "length(family_id) BETWEEN 1 AND 128",
            name="ck_package_candidates_family_id_length",
        ),
        CheckConstraint(
            _lower_hex("upstream_identity_digest", 64),
            name="ck_package_candidates_upstream_identity_digest",
        ),
        CheckConstraint(
            _lower_hex("metadata_digest", 64),
            name="ck_package_candidates_metadata_digest",
        ),
        CheckConstraint(
            "state IN ('discovered', 'resolving', 'resolved', 'unsupported', "
            "'quarantined', 'rejected')",
            name="ck_package_candidates_state",
        ),
        CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_candidates_reason_code_length",
        ),
        CheckConstraint(
            "reason_detail IS NULL OR length(CAST(reason_detail AS TEXT)) <= 8192",
            name="ck_package_candidates_reason_detail_size",
        ),
        CheckConstraint(
            "length(source_provider) BETWEEN 1 AND 64",
            name="ck_package_candidates_source_provider_length",
        ),
        CheckConstraint(
            "length(source_reference) BETWEEN 1 AND 1024",
            name="ck_package_candidates_source_reference_length",
        ),
        UniqueConstraint(
            "family_id",
            "upstream_identity_digest",
            "metadata_digest",
            name="uq_package_candidates_identity",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    family_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    upstream_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    upstream_version: Mapped[str] = mapped_column(String(256), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(128))
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    reason_detail: Mapped[dict[str, object] | None] = mapped_column(JSON)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSON)
    discovered_by: Mapped[str] = mapped_column(String(200), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PackageResolution(Base):
    """A deterministic, retry-safe resolution projection for one candidate."""

    __tablename__ = "package_resolutions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'resolving', 'resolved', 'unsupported', "
            "'incompatible', 'quarantined', 'rejected')",
            name="ck_package_resolutions_state",
        ),
        CheckConstraint(
            "resolver_schema_version >= 1",
            name="ck_package_resolutions_schema_version",
        ),
        CheckConstraint(
            _nullable_lower_hex("release_digest", 64),
            name="ck_package_resolutions_release_digest",
        ),
        CheckConstraint(
            "(state = 'resolved' AND release_digest IS NOT NULL) OR "
            "(state <> 'resolved')",
            name="ck_package_resolutions_resolved_release_binding",
        ),
        CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_resolutions_reason_code_length",
        ),
        CheckConstraint(
            "reason_detail IS NULL OR length(CAST(reason_detail AS TEXT)) <= 8192",
            name="ck_package_resolutions_reason_detail_size",
        ),
        UniqueConstraint(
            "candidate_id",
            "resolver_id",
            "resolver_schema_version",
            name="uq_package_resolutions_candidate_resolver_schema",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("package_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resolver_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resolver_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    release_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    reason_detail: Mapped[dict[str, object] | None] = mapped_column(JSON)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSON)
    resolved_by: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PackageValidationRun(Base):
    """Durable validation evidence bound to exact package and fleet digests."""

    __tablename__ = "package_validation_runs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('planned', 'running', 'passed', 'failed', 'retryable', "
            "'rejected', 'cancelled')",
            name="ck_package_validation_runs_state",
        ),
        CheckConstraint(
            "validation_kind IN ('artifact', 'health', 'inference', 'compatibility')",
            name="ck_package_validation_runs_kind",
        ),
        CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_validation_runs_release_digest",
        ),
        CheckConstraint(
            _lower_hex("policy_digest", 64),
            name="ck_package_validation_runs_policy_digest",
        ),
        CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_package_validation_runs_fleet_digest",
        ),
        CheckConstraint(
            "attempt >= 0",
            name="ck_package_validation_runs_attempt",
        ),
        CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_validation_runs_reason_code_length",
        ),
        CheckConstraint(
            "failure_detail IS NULL OR length(CAST(failure_detail AS TEXT)) <= 8192",
            name="ck_package_validation_runs_failure_detail_size",
        ),
        CheckConstraint(
            "evidence IS NULL OR length(CAST(evidence AS TEXT)) <= 16384",
            name="ck_package_validation_runs_evidence_size",
        ),
        CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 8192",
            name="ck_package_validation_runs_progress_size",
        ),
        UniqueConstraint(
            "resolution_id",
            "validation_kind",
            "policy_digest",
            "fleet_digest",
            name="uq_package_validation_runs_binding",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("package_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resolution_id: Mapped[str] = mapped_column(
        ForeignKey("package_resolutions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    fleet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    failure_detail: Mapped[dict[str, object] | None] = mapped_column(JSON)
    evidence: Mapped[dict[str, object] | None] = mapped_column(JSON)
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageRollout(Base):
    """Immutable digest-bound desired-state rollout and orchestration cursor."""

    __tablename__ = "package_rollouts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('planned', 'preparing', 'activating', 'health-checking', "
            "'soaking', 'paused', 'rolling-back', 'completed', 'failed', "
            "'rolled-back', 'cancelled', 'running', 'partial', 'waiting-for-operator')",
            name="ck_package_rollouts_state",
        ),
        CheckConstraint(
            _lower_hex("deployment_digest", 64),
            name="ck_package_rollouts_deployment_digest",
        ),
        CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_rollouts_release_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("previous_release_digest", 64),
            name="ck_package_rollouts_previous_release_digest",
        ),
        CheckConstraint(
            _lower_hex("policy_digest", 64),
            name="ck_package_rollouts_policy_digest",
        ),
        CheckConstraint(
            _lower_hex("tuf_target_digest", 64),
            name="ck_package_rollouts_tuf_target_digest",
        ),
        CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_package_rollouts_fleet_digest",
        ),
        CheckConstraint(
            _lower_hex("topology_digest", 64),
            name="ck_package_rollouts_topology_digest",
        ),
        CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_package_rollouts_plan_digest",
        ),
        CheckConstraint(
            "length(base_commit) BETWEEN 40 AND 128",
            name="ck_package_rollouts_base_commit_length",
        ),
        CheckConstraint(
            "current_batch >= 0",
            name="ck_package_rollouts_current_batch",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) <= 1024",
            name="ck_package_rollouts_failure_reason_size",
        ),
        CheckConstraint(
            "plan IS NULL OR length(CAST(plan AS TEXT)) <= 32768",
            name="ck_package_rollouts_plan_size",
        ),
        CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 16384",
            name="ck_package_rollouts_progress_size",
        ),
        CheckConstraint(
            "failure_evidence_digest IS NULL OR "
            f"({_lower_hex('failure_evidence_digest', 64)})",
            name="ck_package_rollouts_failure_evidence_digest",
        ),
        CheckConstraint(
            "rollback_evidence_digest IS NULL OR "
            f"({_lower_hex('rollback_evidence_digest', 64)})",
            name="ck_package_rollouts_rollback_evidence_digest",
        ),
        UniqueConstraint(
            "deployment_id",
            "release_digest",
            "base_commit",
            "plan_digest",
            name="uq_package_rollouts_deployment_release_commit_plan",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), unique=True
    )
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    deployment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    previous_release_digest: Mapped[str | None] = mapped_column(String(64))
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    tuf_target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    fleet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    topology_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    plan: Mapped[dict[str, object] | None] = mapped_column(JSON)
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    current_batch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failure_reason: Mapped[str | None] = mapped_column(Text)
    failure_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    rollback_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageRolloutNode(Base):
    """Per-node package operation binding and bounded progress projection."""

    __tablename__ = "package_rollout_nodes"
    __table_args__ = (
        UniqueConstraint(
            "rollout_id", "node_id", name="uq_package_rollout_nodes_rollout_node"
        ),
        UniqueConstraint(
            "rollout_id",
            "batch_index",
            "node_order",
            name="uq_package_rollout_nodes_batch_order",
        ),
        CheckConstraint(
            "state IN ('offline-pending', 'pending', 'queued', 'running', 'preparing', 'prepared', "
            "'activating', 'health-checking', 'accepted', 'failed', "
            "'rolling-back', 'rolled-back', 'cancelled')",
            name="ck_package_rollout_nodes_state",
        ),
        CheckConstraint(
            "batch_index >= -1 AND node_order >= 0",
            name="ck_package_rollout_nodes_order",
        ),
        CheckConstraint(
            _lower_hex("expected_payload_digest", 64),
            name="ck_package_rollout_nodes_expected_payload_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("observed_release_digest", 64),
            name="ck_package_rollout_nodes_observed_release_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("evidence_digest", 64),
            name="ck_package_rollout_nodes_evidence_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_package_rollout_nodes_failure_evidence_digest",
        ),
        CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_package_rollout_nodes_rollback_evidence_digest",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) <= 1024",
            name="ck_package_rollout_nodes_failure_reason_size",
        ),
        CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 8192",
            name="ck_package_rollout_nodes_progress_size",
        ),
        CheckConstraint(
            "length(CAST(operation_history AS TEXT)) <= 16384",
            name="ck_package_rollout_nodes_operation_history_size",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    rollout_id: Mapped[str] = mapped_column(
        ForeignKey("package_rollouts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id"), nullable=False, index=True
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_canary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    operation_kind: Mapped[str | None] = mapped_column(String(80))
    graph_operation_id: Mapped[str | None] = mapped_column(String(128))
    operation_key: Mapped[str | None] = mapped_column(String(128))
    operation_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    rollback_operation_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    operation_history: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    expected_payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_release_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    failure_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    rollback_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[dict[str, object] | None] = mapped_column(JSON)
    dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activation_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PackageObservation(Base):
    """Latest bounded authenticated package state reported by an agent."""

    __tablename__ = "package_observations"
    __table_args__ = (
        CheckConstraint(
            "length(deployment_id) BETWEEN 1 AND 128",
            name="ck_package_observations_deployment_id_length",
        ),
        CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_observations_release_digest",
        ),
        CheckConstraint(
            _lower_hex("observation_digest", 64),
            name="ck_package_observations_observation_digest",
        ),
        CheckConstraint(
            "state IN ('unknown', 'prepared', 'active', 'healthy', 'stopped', "
            "'failed', 'rolling-back')",
            name="ck_package_observations_state",
        ),
        CheckConstraint(
            "summary IS NULL OR length(CAST(summary AS TEXT)) <= 8192",
            name="ck_package_observations_summary_size",
        ),
        UniqueConstraint(
            "node_id",
            "deployment_id",
            "release_digest",
            "observation_digest",
            name="uq_package_observations_identity",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
