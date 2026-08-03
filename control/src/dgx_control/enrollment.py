"""Durable, administrator-approved enrollment for immutable Spark agent identities."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets
import threading
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import AgentCertificate, AgentEnrollment, AgentEnrollmentGrant, AgentNode
from .pki import CertificateAuthority, IssuedCertificate


_NODE_ID = re.compile(r"spk_[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_MAX_GRANT_TTL_SECONDS = 600
_MAX_CSR_BYTES = 16 * 1024
_EVIDENCE_FIELDS = (
    "node_id",
    "csr_public_key_fingerprint",
    "host_key_fingerprint",
    "hardware_fingerprint",
    "agent_digest",
    "boot_id",
)
_EVIDENCE_LIMITS = {
    "node_id": 36,
    "csr_public_key_fingerprint": 64,
    "host_key_fingerprint": 512,
    "hardware_fingerprint": 512,
    "agent_digest": 128,
    "boot_id": 128,
}
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


class EnrollmentDenied(RuntimeError):
    """Enrollment input or state does not authorize the requested operation."""


@dataclass(frozen=True)
class EnrollmentGrant:
    id: str
    node_id: str
    expires_at: datetime
    token: str = field(repr=False)


@dataclass(frozen=True)
class PendingEnrollment:
    id: str
    node_id: str
    state: str


@dataclass(frozen=True)
class _IssuanceClaim:
    node_id: str
    public_key_pem: bytes


class EnrollmentService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        authority: CertificateAuthority,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._authority = authority
        self._clock = clock
        # SQLite ignores row locks. PostgreSQL correctness comes from the
        # locked grant row; this preserves the same behavior in local tests.
        self._submit_lock = threading.RLock()
        # PostgreSQL uses a durable advisory lock for cross-service claims.
        # This makes SQLite's same-process behavior match that safety rule.
        self._issuance_lock = threading.RLock()

    def create(self, node_id: str, actor: str, ttl_seconds: int) -> EnrollmentGrant:
        _validate_node_id(node_id)
        _validate_actor(actor)
        if not 0 < ttl_seconds <= _MAX_GRANT_TTL_SECONDS:
            raise ValueError("enrollment grant TTL must be between one and 600 seconds")
        now = _utc(self._clock())
        token_bytes = secrets.token_bytes(32)
        token = base64.urlsafe_b64encode(token_bytes).rstrip(b"=").decode("ascii")
        grant = AgentEnrollmentGrant(
            id=str(uuid.uuid4()),
            node_id=node_id,
            token_digest=_digest(token_bytes),
            created_by=actor,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        with self._sessions.begin() as session:
            session.add(grant)
        return EnrollmentGrant(id=grant.id, node_id=node_id, expires_at=grant.expires_at, token=token)

    def submit(self, token: str, csr: bytes, evidence: Mapping[str, object]) -> PendingEnrollment:
        token_bytes = _decode_token(token)
        now = _utc(self._clock())
        failure: str | None = None
        pending: PendingEnrollment | None = None
        with self._submit_lock, self._sessions.begin() as session:
            grant = session.scalar(
                select(AgentEnrollmentGrant)
                .where(AgentEnrollmentGrant.token_digest == _digest(token_bytes))
                .with_for_update(of=AgentEnrollmentGrant)
            )
            if grant is None:
                failure = "invalid enrollment grant"
            elif grant.consumed_at is not None:
                failure = "enrollment grant is consumed"
            elif _stored_utc(grant.expires_at) <= now:
                grant.consumed_at = now
                failure = "enrollment grant is expired"
            else:
                try:
                    if not isinstance(csr, bytes) or len(csr) > _MAX_CSR_BYTES:
                        raise EnrollmentDenied("CSR is too large")
                    public_key_pem, public_key_fingerprint = _load_csr(csr)
                except EnrollmentDenied as error:
                    failure = str(error)
                else:
                    values, failure = _validate_evidence(evidence, grant.node_id, public_key_fingerprint)
                if failure is None:
                    enrollment = AgentEnrollment(
                        id=str(uuid.uuid4()),
                        grant_id=grant.id,
                        node_id=grant.node_id,
                        state="pending-approval",
                        csr_public_key_pem=public_key_pem.decode("ascii"),
                        csr_public_key_fingerprint=public_key_fingerprint,
                        host_key_fingerprint=values["host_key_fingerprint"],
                        hardware_fingerprint=values["hardware_fingerprint"],
                        agent_digest=values["agent_digest"],
                        boot_id=values["boot_id"],
                        created_at=now,
                    )
                    grant.consumed_at = now
                    session.add(enrollment)
                    pending = _pending(enrollment)
                else:
                    # A valid bearer token is one-use even when its holder
                    # supplies evidence that cannot be accepted.
                    grant.consumed_at = now
        if failure is not None:
            raise EnrollmentDenied(failure)
        assert pending is not None
        return pending

    def approve(self, enrollment_id: str, actor: str) -> IssuedCertificate:
        _validate_actor(actor)
        now = _utc(self._clock())
        with self._issuance_lock:
            claim = self._claim_issuance(enrollment_id, actor, now)
            if isinstance(claim, IssuedCertificate):
                return claim
            issued = self._authority.issue_node(claim.node_id, claim.public_key_pem, now)
            if issued.node_id != claim.node_id:
                raise EnrollmentDenied("certificate authority returned a mismatched node identity")
            try:
                with self._sessions.begin() as session:
                    enrollment = _locked_enrollment(session, enrollment_id)
                    if enrollment.state != "issuing":
                        raise EnrollmentDenied("certificate issuance state changed; manual recovery required")
                    if session.get(AgentNode, enrollment.node_id) is not None:
                        raise EnrollmentDenied("node identity already exists")
                    _persist_issued_enrollment(session, enrollment, issued, actor, now)
            except IntegrityError as error:
                # The durable issuing state was committed before the provider
                # call.  Never retry automatically after an uncertain write:
                # the provider may already have created this certificate.
                raise EnrollmentDenied("certificate persistence failed; manual recovery required") from error
            return issued

    def reject(self, enrollment_id: str, actor: str, reason: str) -> PendingEnrollment:
        _validate_actor(actor)
        if not reason.strip():
            raise ValueError("rejection reason is required")
        now = _utc(self._clock())
        with self._sessions.begin() as session:
            enrollment = _locked_enrollment(session, enrollment_id)
            if enrollment.state == "approved":
                raise EnrollmentDenied("enrollment already approved")
            if enrollment.state == "rejected":
                return _pending(enrollment)
            if enrollment.state != "pending-approval":
                raise EnrollmentDenied("enrollment state cannot be rejected")
            enrollment.state = "rejected"
            enrollment.decision_actor = actor
            enrollment.decided_at = now
            enrollment.rejection_reason = reason
            return _pending(enrollment)

    def renew(self, node_id: str, serial: str, csr: bytes) -> IssuedCertificate:
        _validate_node_id(node_id)
        if not serial.strip():
            raise ValueError("certificate serial is required")
        public_key_pem, _ = _load_csr(csr)
        now = _utc(self._clock())
        with self._sessions.begin() as session:
            node = session.scalar(
                select(AgentNode).where(AgentNode.node_id == node_id).with_for_update(of=AgentNode)
            )
            if node is None:
                raise EnrollmentDenied("certificate serial does not identify node")
            certificate = session.scalar(
                select(AgentCertificate)
                .where(AgentCertificate.serial == serial, AgentCertificate.node_id == node_id)
                .with_for_update(of=AgentCertificate)
            )
            if certificate is None:
                raise EnrollmentDenied("certificate serial does not identify node")
            if node.state != "active" or node.revoked_at is not None or certificate.revoked_at is not None:
                raise EnrollmentDenied("node identity is retired or revoked")
            if _stored_utc(certificate.not_before) > now or _stored_utc(certificate.not_after) <= now:
                raise EnrollmentDenied("certificate is not currently valid")
            issued = self._authority.renew_node(node_id, public_key_pem, now)
            if issued.node_id != node_id:
                raise EnrollmentDenied("certificate authority returned a mismatched node identity")
            if issued.serial == serial:
                raise EnrollmentDenied("certificate authority reused renewal serial")
            certificate.revoked_at = now
            session.add(AgentCertificate(
                serial=issued.serial,
                node_id=node_id,
                not_before=issued.not_before,
                not_after=issued.not_after,
                fingerprint=issued.fingerprint,
            ))
            return issued

    def _claim_issuance(self, enrollment_id: str, actor: str, now: datetime) -> _IssuanceClaim | IssuedCertificate:
        with self._sessions.begin() as session:
            enrollment = _locked_enrollment(session, enrollment_id)
            if enrollment.state == "approved":
                return _issued(enrollment)
            if enrollment.state == "rejected":
                raise EnrollmentDenied("enrollment was rejected")
            if enrollment.state == "issuing":
                raise EnrollmentDenied("certificate issuance is in progress; manual recovery required")
            if enrollment.state != "pending-approval":
                raise EnrollmentDenied("enrollment state cannot be approved")
            _lock_node_issuance(session, enrollment.node_id)
            if session.get(AgentNode, enrollment.node_id) is not None:
                raise EnrollmentDenied("node identity already exists")
            competing = session.scalar(
                select(AgentEnrollment.id)
                .where(
                    AgentEnrollment.node_id == enrollment.node_id,
                    AgentEnrollment.id != enrollment.id,
                    AgentEnrollment.state == "issuing",
                )
                .with_for_update(of=AgentEnrollment)
                .limit(1)
            )
            if competing is not None:
                raise EnrollmentDenied("node enrollment issuance is in progress")
            enrollment.state = "issuing"
            enrollment.decision_actor = actor
            enrollment.decided_at = now
            return _IssuanceClaim(enrollment.node_id, enrollment.csr_public_key_pem.encode("ascii"))


def _persist_issued_enrollment(
    session: Session,
    enrollment: AgentEnrollment,
    issued: IssuedCertificate,
    actor: str,
    now: datetime,
) -> None:
    try:
        certificate_pem = issued.certificate_pem.decode("ascii")
        chain_pem = issued.chain_pem.decode("ascii")
    except UnicodeDecodeError as error:
        raise EnrollmentDenied("certificate authority returned non-PEM certificate material") from error
    node = AgentNode(node_id=enrollment.node_id, state="active", capabilities=[])
    session.add(node)
    # There is no ORM relationship between these operational rows. Flush the
    # FK parent explicitly before adding its certificate child; PostgreSQL does
    # not infer the ordering that SQLite happened to tolerate.
    session.flush([node])
    session.add(AgentCertificate(
        serial=issued.serial,
        node_id=enrollment.node_id,
        not_before=issued.not_before,
        not_after=issued.not_after,
        fingerprint=issued.fingerprint,
    ))
    enrollment.state = "approved"
    enrollment.decision_actor = actor
    enrollment.decided_at = now
    enrollment.certificate_pem = certificate_pem
    enrollment.chain_pem = chain_pem
    enrollment.certificate_serial = issued.serial
    enrollment.certificate_fingerprint = issued.fingerprint
    enrollment.certificate_not_before = issued.not_before
    enrollment.certificate_not_after = issued.not_after


def _locked_enrollment(session: Session, enrollment_id: str) -> AgentEnrollment:
    enrollment = session.scalar(
        select(AgentEnrollment).where(AgentEnrollment.id == enrollment_id).with_for_update(of=AgentEnrollment)
    )
    if enrollment is None:
        raise EnrollmentDenied("unknown enrollment")
    return enrollment


def _lock_node_issuance(session: Session, node_id: str) -> None:
    """Serialize claims for an absent node identity across PostgreSQL services."""
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(node_id.encode("ascii")).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _issued(enrollment: AgentEnrollment) -> IssuedCertificate:
    if any(value is None for value in (
        enrollment.certificate_pem,
        enrollment.chain_pem,
        enrollment.certificate_serial,
        enrollment.certificate_fingerprint,
        enrollment.certificate_not_before,
        enrollment.certificate_not_after,
    )):
        raise RuntimeError("approved enrollment is missing certificate metadata")
    return IssuedCertificate(
        node_id=enrollment.node_id,
        certificate_pem=enrollment.certificate_pem.encode("ascii"),  # type: ignore[union-attr]
        chain_pem=enrollment.chain_pem.encode("ascii"),  # type: ignore[union-attr]
        serial=enrollment.certificate_serial,  # type: ignore[arg-type]
        fingerprint=enrollment.certificate_fingerprint,  # type: ignore[arg-type]
        not_before=_stored_utc(enrollment.certificate_not_before),  # type: ignore[arg-type]
        not_after=_stored_utc(enrollment.certificate_not_after),  # type: ignore[arg-type]
    )


def _pending(enrollment: AgentEnrollment) -> PendingEnrollment:
    return PendingEnrollment(id=enrollment.id, node_id=enrollment.node_id, state=enrollment.state)


def _load_csr(csr: bytes) -> tuple[bytes, str]:
    try:
        request = x509.load_pem_x509_csr(csr)
    except (TypeError, ValueError) as error:
        raise EnrollmentDenied("CSR must be valid PEM") from error
    if not request.is_signature_valid:
        raise EnrollmentDenied("CSR signature is invalid")
    public_key = request.public_key()
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise EnrollmentDenied("CSR public key must be Ed25519")
    public_key_pem = public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    public_key_der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return public_key_pem, _digest(public_key_der)


def _validate_evidence(
    evidence: Mapping[str, object], node_id: str, public_key_fingerprint: str
) -> tuple[dict[str, str], str | None]:
    values: dict[str, str] = {}
    if set(evidence) != set(_EVIDENCE_FIELDS):
        return values, "evidence fields are invalid"
    for field in _EVIDENCE_FIELDS:
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > _EVIDENCE_LIMITS[field]:
            return values, f"evidence {field} is required"
        values[field] = value
    if values["node_id"] != node_id:
        return values, "evidence node ID does not match enrollment grant"
    if values["csr_public_key_fingerprint"] != public_key_fingerprint:
        return values, "evidence CSR public-key fingerprint does not match CSR"
    if _HEX_64.fullmatch(values["csr_public_key_fingerprint"]) is None:
        return values, "evidence CSR public-key fingerprint is invalid"
    if _HEX_64.fullmatch(values["agent_digest"]) is None:
        return values, "evidence agent digest is invalid"
    return values, None


def _decode_token(token: str) -> bytes:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise EnrollmentDenied("invalid enrollment grant")
    try:
        value = base64.b64decode((token + "=").encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise EnrollmentDenied("invalid enrollment grant") from error
    if len(value) != 32:
        raise EnrollmentDenied("invalid enrollment grant")
    return value


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_node_id(node_id: str) -> None:
    if _NODE_ID.fullmatch(node_id) is None:
        raise ValueError("node ID must be a canonical spk_<32 lowercase hex characters> value")


def _validate_actor(actor: str) -> None:
    if not actor.strip():
        raise ValueError("administrator actor is required")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Normalize database timestamps; SQLite does not round-trip tzinfo."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
