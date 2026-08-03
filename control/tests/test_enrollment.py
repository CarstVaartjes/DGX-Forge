from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import shutil
import subprocess
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from dgx_control.enrollment import EnrollmentDenied, EnrollmentService
from dgx_control.models import AgentCertificate, AgentEnrollment, AgentEnrollmentGrant, AgentNode, Base
from dgx_control.pki import CertificateAuthority, IssuedCertificate


NODE_ID = "spk_0123456789abcdef0123456789abcdef"
OTHER_NODE_ID = "spk_fedcba9876543210fedcba9876543210"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingAuthority(CertificateAuthority):
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, datetime]] = []
        self._serial = 0

    def issue_node(self, node_id: str, public_key_pem: bytes, now: datetime) -> IssuedCertificate:
        self.calls.append((node_id, public_key_pem, now))
        self._serial += 1
        return IssuedCertificate(
            node_id=node_id,
            certificate_pem=f"certificate-{self._serial}".encode(),
            chain_pem=b"intermediate-chain",
            serial=f"serial-{self._serial}",
            fingerprint=f"fingerprint-{self._serial}",
            not_before=now,
            not_after=now + timedelta(hours=24),
        )

    def renew_node(self, node_id: str, public_key_pem: bytes, now: datetime) -> IssuedCertificate:
        return self.issue_node(node_id, public_key_pem, now)

    def revocation_bundle(self, now: datetime) -> bytes:
        return b"revocation-bundle"


def csr() -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "untrusted")]))
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )


def invalid_signature_csr() -> bytes:
    request = x509.load_pem_x509_csr(csr())
    der = bytearray(request.public_bytes(serialization.Encoding.DER))
    der[-1] ^= 1
    encoded = base64.b64encode(der)
    return b"-----BEGIN CERTIFICATE REQUEST-----\n" + encoded + b"\n-----END CERTIFICATE REQUEST-----\n"


def rsa_csr() -> bytes:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "untrusted")]))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


def public_key_fingerprint(csr_pem: bytes) -> str:
    request = x509.load_pem_x509_csr(csr_pem)
    public_key = request.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(public_key).hexdigest()


def evidence(csr_pem: bytes, *, node_id: str = NODE_ID, **overrides: str) -> dict[str, str]:
    result = {
        "node_id": node_id,
        "csr_public_key_fingerprint": public_key_fingerprint(csr_pem),
        "host_key_fingerprint": "SHA256:host-key",
        "hardware_fingerprint": "hardware-fingerprint",
        "agent_digest": "a" * 64,
        "boot_id": "b9e9b12a-63e4-4cb5-83f3-4d963d321ec8",
    }
    result.update(overrides)
    return result


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'enrollment.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    authority = RecordingAuthority()
    sessions = sessionmaker(engine, expire_on_commit=False)
    return EnrollmentService(sessions, authority, clock=clock), sessions, clock, authority


def enroll(service: EnrollmentService, *, node_id: str = NODE_ID, request: bytes | None = None):
    request = request or csr()
    grant = service.create(node_id, "admin", 600)
    return service.submit(grant.token, request, evidence(request, node_id=node_id))


def test_grant_is_single_use_and_requires_administrator_approval(service) -> None:
    enrollment, sessions, _, authority = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)

    assert len(base64.urlsafe_b64decode(grant.token + "=")) == 32
    assert "token" not in repr(grant)
    pending = enrollment.submit(grant.token, request, evidence(request))
    assert pending.state == "pending-approval"
    with pytest.raises(EnrollmentDenied, match="consumed"):
        enrollment.submit(grant.token, request, evidence(request))

    issued = enrollment.approve(pending.id, "admin")

    assert issued.node_id == NODE_ID
    assert len(authority.calls) == 1
    with sessions() as session:
        stored_grant = session.scalar(select(AgentEnrollmentGrant))
        stored = session.get(AgentEnrollment, pending.id)
        certificate = session.get(AgentCertificate, issued.serial)
        node = session.get(AgentNode, NODE_ID)
        assert stored_grant is not None and stored_grant.token_digest == hashlib.sha256(
            base64.urlsafe_b64decode(grant.token + "=")
        ).hexdigest()
        assert not hasattr(stored_grant, "token")
        assert stored is not None and stored.decision_actor == "admin"
        assert stored.csr_public_key_fingerprint == public_key_fingerprint(request)
        assert certificate is not None and certificate.node_id == NODE_ID
        assert node is not None and node.state == "active"


def test_submit_rejects_expired_malformed_and_evidence_mismatched_grants_without_leaking_token(service) -> None:
    enrollment, _, clock, _ = service
    request = csr()
    expired = enrollment.create(NODE_ID, "admin", 1)
    clock.advance(seconds=1)
    with pytest.raises(EnrollmentDenied, match="expired"):
        enrollment.submit(expired.token, request, evidence(request))
    with pytest.raises(EnrollmentDenied, match="invalid enrollment grant") as malformed:
        enrollment.submit("not a token", request, evidence(request))
    assert expired.token not in str(malformed.value)

    mismatched = enrollment.create(NODE_ID, "admin", 600)
    with pytest.raises(EnrollmentDenied, match="evidence"):
        enrollment.submit(mismatched.token, request, evidence(request, node_id=OTHER_NODE_ID))
    with pytest.raises(EnrollmentDenied, match="consumed"):
        enrollment.submit(mismatched.token, request, evidence(request))


def test_submit_rejects_malformed_csr_and_csr_fingerprint_mismatch(service) -> None:
    enrollment, _, _, _ = service
    grant = enrollment.create(NODE_ID, "admin", 600)
    with pytest.raises(EnrollmentDenied, match="CSR"):
        enrollment.submit(grant.token, b"not a csr", {})

    request = csr()
    mismatch = enrollment.create(NODE_ID, "admin", 600)
    with pytest.raises(EnrollmentDenied, match="CSR public-key fingerprint"):
        enrollment.submit(mismatch.token, request, evidence(request, csr_public_key_fingerprint="0" * 64))


@pytest.mark.parametrize(
    ("invalid_request", "message"),
    ((b"not a csr", "CSR must be valid PEM"), (invalid_signature_csr(), "CSR signature is invalid"), (rsa_csr(), "CSR public key must be Ed25519")),
    ids=("malformed", "invalid-signature", "unsupported-key"),
)
def test_identifiable_grant_is_consumed_when_csr_validation_fails(service, invalid_request: bytes, message: str) -> None:
    enrollment, sessions, _, _ = service
    grant = enrollment.create(NODE_ID, "admin", 600)

    with pytest.raises(EnrollmentDenied, match=message):
        enrollment.submit(grant.token, invalid_request, {})
    with pytest.raises(EnrollmentDenied, match="consumed"):
        enrollment.submit(grant.token, csr(), evidence(csr()))
    with sessions() as session:
        stored_grant = session.get(AgentEnrollmentGrant, grant.id)
        assert stored_grant is not None and stored_grant.consumed_at is not None
        assert session.scalar(select(func.count()).select_from(AgentEnrollment)) == 0


def test_sqlite_simultaneous_replay_has_exactly_one_submit_winner(service) -> None:
    enrollment, _, _, _ = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    barrier = threading.Barrier(4)

    def submit() -> object:
        barrier.wait()
        try:
            return enrollment.submit(grant.token, request, evidence(request))
        except EnrollmentDenied as error:
            return error

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(), range(4)))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, EnrollmentDenied) and "consumed" in str(result) for result in results) == 3


def test_approval_and_rejection_are_attributed_idempotent_and_conflicting(service) -> None:
    enrollment, sessions, _, authority = service
    pending = enroll(enrollment)
    first = enrollment.approve(pending.id, "admin-a")
    second = enrollment.approve(pending.id, "admin-b")

    assert second == first
    assert len(authority.calls) == 1
    with sessions() as session:
        stored = session.get(AgentEnrollment, pending.id)
        assert stored is not None and stored.decision_actor == "admin-a"
    with pytest.raises(EnrollmentDenied, match="already approved"):
        enrollment.reject(pending.id, "admin-b", "operator changed mind")

    rejected = enroll(enrollment, node_id=OTHER_NODE_ID)
    assert enrollment.reject(rejected.id, "admin-c", "not authorized").state == "rejected"
    assert enrollment.reject(rejected.id, "admin-d", "different reason").state == "rejected"
    with sessions() as session:
        stored = session.get(AgentEnrollment, rejected.id)
        assert stored is not None and stored.decision_actor == "admin-c" and stored.rejection_reason == "not authorized"
    with pytest.raises(EnrollmentDenied, match="rejected"):
        enrollment.approve(rejected.id, "admin-c")


def test_renewal_rotates_certificate_for_same_node_and_rejects_bad_serial_csr_or_retired_identity(service) -> None:
    enrollment, sessions, clock, authority = service
    pending = enroll(enrollment)
    issued = enrollment.approve(pending.id, "admin")
    renewed_csr = csr()

    renewed = enrollment.renew(NODE_ID, issued.serial, renewed_csr)

    assert renewed.node_id == NODE_ID
    assert renewed.serial != issued.serial
    assert authority.calls[-1][1] == x509.load_pem_x509_csr(renewed_csr).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with sessions() as session:
        original = session.get(AgentCertificate, issued.serial)
        assert original is not None and original.revoked_at is not None
    with pytest.raises(EnrollmentDenied, match="serial"):
        enrollment.renew(OTHER_NODE_ID, renewed.serial, csr())
    with pytest.raises(EnrollmentDenied, match="CSR"):
        enrollment.renew(NODE_ID, renewed.serial, b"not a csr")
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        assert node is not None
        node.state = "retired"
    with pytest.raises(EnrollmentDenied, match="retired|revoked"):
        enrollment.renew(NODE_ID, renewed.serial, csr())


def test_revoked_identity_denies_renewal_immediately(service) -> None:
    enrollment, sessions, clock, _ = service
    issued = enrollment.approve(enroll(enrollment).id, "admin")
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, issued.serial)
        assert certificate is not None
        certificate.revoked_at = clock.now

    with pytest.raises(EnrollmentDenied, match="retired|revoked"):
        enrollment.renew(NODE_ID, issued.serial, csr())


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL locking integration tests")
    try:
        container = subprocess.check_output([
            "docker", "run", "--rm", "-d", "-e", "POSTGRES_PASSWORD=postgres",
            "-p", "127.0.0.1::5432", "postgres:16",
        ], text=True).strip()
    except subprocess.CalledProcessError as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
    try:
        port = subprocess.check_output([
            "docker", "inspect", "-f",
            "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}", container,
        ], text=True).strip()
        engine = create_engine(f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres")
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.skip("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(["docker", "stop", container], check=False, capture_output=True)


def test_postgres_separate_services_have_exactly_one_replay_winner(postgres_engine: Engine) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    authority = RecordingAuthority()
    first = EnrollmentService(sessions, authority, clock=clock)
    second = EnrollmentService(sessions, authority, clock=clock)
    request = csr()
    grant = first.create(NODE_ID, "admin", 600)
    barrier = threading.Barrier(2)

    def submit(service: EnrollmentService) -> object:
        barrier.wait()
        try:
            return service.submit(grant.token, request, evidence(request))
        except EnrollmentDenied as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (first, second)))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, EnrollmentDenied) and "consumed" in str(result) for result in results) == 1


def test_postgres_approval_persists_node_before_certificate(postgres_engine: Engine) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    authority = RecordingAuthority()
    enrollment = EnrollmentService(sessions, authority, clock=clock)
    pending = enroll(enrollment)

    issued = enrollment.approve(pending.id, "admin")

    with sessions() as session:
        assert session.get(AgentNode, NODE_ID) is not None
        assert session.get(AgentCertificate, issued.serial) is not None
        stored = session.get(AgentEnrollment, pending.id)
        assert stored is not None and stored.state == "approved" and stored.certificate_serial == issued.serial


class PausingAuthority(RecordingAuthority):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def issue_node(self, node_id: str, public_key_pem: bytes, now: datetime) -> IssuedCertificate:
        with self._lock:
            self.calls.append((node_id, public_key_pem, now))
        self.entered.set()
        assert self.release.wait(timeout=5)
        with self._lock:
            self._serial += 1
            serial = self._serial
        return IssuedCertificate(
            node_id=node_id,
            certificate_pem=f"certificate-{serial}".encode(),
            chain_pem=b"intermediate-chain",
            serial=f"serial-{serial}",
            fingerprint=f"fingerprint-{serial}",
            not_before=now,
            not_after=now + timedelta(hours=24),
        )


def test_postgres_same_node_approval_race_issues_exactly_once(postgres_engine: Engine) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    authority = PausingAuthority()
    first = EnrollmentService(sessions, authority, clock=clock)
    second = EnrollmentService(sessions, authority, clock=clock)
    first_pending = enroll(first)
    second_pending = enroll(second)
    results: list[object] = []

    def approve(service: EnrollmentService, enrollment_id: str) -> None:
        try:
            results.append(service.approve(enrollment_id, "admin"))
        except EnrollmentDenied as error:
            results.append(error)

    first_thread = threading.Thread(target=approve, args=(first, first_pending.id))
    second_thread = threading.Thread(target=approve, args=(second, second_pending.id))
    first_thread.start()
    assert authority.entered.wait(timeout=5)
    second_thread.start()
    time.sleep(0.25)
    authority.release.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert len(authority.calls) == 1
    assert sum(isinstance(result, IssuedCertificate) for result in results) == 1
    assert sum(isinstance(result, EnrollmentDenied) for result in results) == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentNode)) == 1
        assert session.scalar(select(func.count()).select_from(AgentCertificate)) == 1
        assert session.scalar(select(func.count()).select_from(AgentEnrollment).where(
            AgentEnrollment.state == "approved"
        )) == 1


def test_approval_persistence_failure_stays_recoverable_without_reissuing(service) -> None:
    enrollment, sessions, clock, authority = service
    with sessions.begin() as session:
        session.add(AgentNode(node_id=OTHER_NODE_ID, state="active", capabilities=[]))
        session.add(AgentCertificate(
            serial="serial-1",
            node_id=OTHER_NODE_ID,
            not_before=clock.now,
            not_after=clock.now + timedelta(hours=1),
            fingerprint="existing-fingerprint",
        ))
    pending = enroll(enrollment)

    with pytest.raises(EnrollmentDenied, match="manual recovery"):
        enrollment.approve(pending.id, "admin")
    with pytest.raises(EnrollmentDenied, match="manual recovery"):
        enrollment.approve(pending.id, "admin")

    assert len(authority.calls) == 1
    with sessions() as session:
        stored = session.get(AgentEnrollment, pending.id)
        assert stored is not None and stored.state == "issuing"
        assert session.get(AgentNode, NODE_ID) is None
