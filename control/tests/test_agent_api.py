from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import os
import uuid

from fastapi.testclient import TestClient
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dgx_control.agent_api import AgentApiServices, _read_chunks
from dgx_control.agent_jobs import AgentJobService
from dgx_control.api import create_app
from dgx_control.audit import MemoryAuditStore
from dgx_control.auth import Actor, TokenCodec
from dgx_control.enrollment import EnrollmentService
from dgx_control.models import AgentCertificate, AgentEnrollment, AgentEnrollmentGrant, AgentNode, Base, Job
from dgx_control.pki import CertificateAuthority, IssuedCertificate


NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32


class Jobs:
    def list(self): return []
    def get(self, _): raise KeyError
    def enqueue(self, *_args, **_kwargs): raise AssertionError


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Authority(CertificateAuthority):
    def issue_node(self, node_id: str, public_key_pem: bytes, now: datetime) -> IssuedCertificate:
        return IssuedCertificate(node_id, b"certificate", b"chain", "issued-serial", "issued-fingerprint", now, now + timedelta(days=1))

    def renew_node(self, node_id: str, public_key_pem: bytes, now: datetime) -> IssuedCertificate:
        return self.issue_node(node_id, public_key_pem, now)

    def revocation_bundle(self, now: datetime) -> bytes:
        return b""


@pytest.fixture
def agent_system(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-api.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    with sessions.begin() as session:
        for node, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentNode(node_id=node, state="active", capabilities=[]))
            session.add(AgentCertificate(serial=serial, node_id=node, fingerprint=f"fingerprint-{serial}", not_before=clock.now - timedelta(seconds=1), not_after=clock.now + timedelta(hours=1)))
    services = AgentApiServices(
        enrollment=EnrollmentService(sessions, Authority(), clock=clock),
        operations=AgentJobService(sessions, clock=clock), sessions=sessions, clock=clock,
        artifact_root=tmp_path / "artifacts",
    )
    services.artifact_root.mkdir()
    codec = TokenCodec(b"k" * 32)
    app = create_app(jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=lambda: {}, now=lambda: 0, agent=services, trusted_agent_proxy_sources=frozenset({"testclient"}))
    return TestClient(app), services, codec, clock


def agent_headers(node: str, serial: str) -> dict[str, str]:
    return {
        "x-dgx-agent-node": node,
        "x-dgx-agent-serial": serial,
        "x-dgx-agent-fingerprint": f"fingerprint-{serial}",
        "x-dgx-agent-verified": "1",
    }


def admin_headers(codec: TokenCodec, role: str = "administrator") -> dict[str, str]:
    return {"Authorization": f"Bearer {codec.issue(Actor(role, role), ttl_seconds=100, now=0)}"}


def parent(sessions, clock: Clock) -> Job:
    job = Job(
        request_id=str(uuid.uuid4()), kind="agent.operations", state="queued", actor="administrator",
        base_commit="a" * 40, targets=[NODE_A], payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={}, current_attempt=0, created_at=clock.now, updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job


def test_spoofed_agent_header_is_rejected() -> None:
    app = create_app(
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(), fleet=lambda: {},
    )

    response = TestClient(app).post("/agent/v1/claim", headers={"x-dgx-agent-node": NODE_A})

    assert response.status_code == 401


def test_agent_routes_do_not_require_human_bearer_tokens() -> None:
    app = create_app(
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(), fleet=lambda: {},
    )

    response = TestClient(app).post("/agent/v1/claim", headers={"Authorization": "Bearer invalid"})

    assert response.status_code == 401


def test_untrusted_proxy_and_malformed_forwarded_identity_are_rejected(agent_system) -> None:
    client, _, _, _ = agent_system
    assert client.post("/agent/v1/claim").status_code == 401
    assert client.post("/agent/v1/claim", headers={**agent_headers(NODE_A, "serial-a"), "x-dgx-agent-verified": "false"}).status_code == 401

    app = create_app(jobs=Jobs(), tokens=TokenCodec(b"k" * 32), audits=MemoryAuditStore(), fleet=lambda: {})
    assert TestClient(app).post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")).status_code == 401


def test_verified_identity_cannot_claim_other_node(agent_system) -> None:
    client, _, _, _ = agent_system
    response = client.post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a"), json={"node_id": NODE_B})
    assert response.status_code == 403


@pytest.mark.parametrize("mutation", ("revoked", "retired", "expired", "fingerprint"))
def test_persisted_certificate_state_is_checked_on_every_agent_request(agent_system, mutation: str) -> None:
    client, services, _, clock = agent_system
    with services.sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        node = session.get(AgentNode, NODE_A)
        assert certificate is not None and node is not None
        if mutation == "revoked":
            certificate.revoked_at = clock.now
        elif mutation == "retired":
            node.state = "retired"
        elif mutation == "expired":
            certificate.not_after = clock.now
        else:
            certificate.fingerprint = "different"
    assert client.post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")).status_code == 401


def test_fence_and_cross_node_result_updates_are_denied(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {})
    claim = client.post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")).json()
    result = {key: claim[key] for key in ("schema_version", "job_id", "operation_id", "attempt", "fence", "node_id", "deadline")}
    foreign = {**result, "node_id": NODE_B, "state": "succeeded", "result": {"healthy": True}}
    assert client.post("/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=foreign).status_code == 403
    stale = {**result, "fence": str(uuid.uuid4()), "state": "succeeded", "result": {"healthy": True}}
    assert client.post("/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=stale).status_code == 409


def test_enrollment_routes_are_admin_only_and_replay_is_rejected(agent_system) -> None:
    client, _, codec, _ = agent_system
    assert client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec, "operator"), json={"node_id": NODE_A, "ttl_seconds": 60}).status_code == 403
    # Existing node is deliberately unrelated to submitting a one-use grant;
    # approval remains the point that rejects a duplicate immutable node.
    grant = client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec), json={"node_id": NODE_A, "ttl_seconds": 60}).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "node")])).sign(key, algorithm=None).public_bytes(serialization.Encoding.PEM)
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    body = {"grant_token": grant["token"], "csr": csr.decode(), "evidence": {"node_id": NODE_A, "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(), "host_key_fingerprint": "host", "hardware_fingerprint": "hardware", "agent_digest": "a" * 64, "boot_id": "boot"}}
    assert client.post("/agent/v1/enroll", json=body).status_code == 202
    assert client.post("/agent/v1/enroll", json=body).status_code == 403


def test_enrollment_evidence_has_a_fixed_bounded_schema(agent_system) -> None:
    client, _, _, _ = agent_system
    response = client.post("/agent/v1/enroll", json={
        "grant_token": "a" * 43, "csr": "x",
        "evidence": {
            "node_id": NODE_A, "csr_public_key_fingerprint": "a" * 64,
            "host_key_fingerprint": "host", "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64, "boot_id": "boot", "unexpected": "x",
        },
    })
    assert response.status_code == 403


def test_artifact_access_is_owned_content_addressed_and_range_bounded(agent_system) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"artifact").hexdigest()
    (services.artifact_root / digest).write_bytes(b"artifact")
    services.operations.enqueue(parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {"artifact_digest": digest})
    response = client.get(f"/agent/v1/artifacts/{digest}", headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=1-3"})
    assert (response.status_code, response.content, response.headers["content-range"]) == (206, b"rti", "bytes 1-3/8")
    assert client.get(f"/agent/v1/artifacts/{digest}", headers=agent_headers(NODE_B, "serial-b")).status_code == 404
    assert client.get("/agent/v1/artifacts/../secret", headers=agent_headers(NODE_A, "serial-a")).status_code == 404
    assert client.get(f"/agent/v1/artifacts/{digest}", headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=0-99999999"}).status_code == 416


def test_artifact_symlink_is_never_served(agent_system, tmp_path) -> None:
    client, services, _, clock = agent_system
    digest = "a" * 64
    (services.artifact_root / digest).symlink_to(tmp_path / "outside")
    services.operations.enqueue(parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {"artifact_digest": digest})
    assert client.get(f"/agent/v1/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")).status_code == 404


def test_artifact_digest_is_verified_from_open_descriptor(agent_system) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"expected").hexdigest()
    (services.artifact_root / digest).write_bytes(b"tampered")
    services.operations.enqueue(parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {"artifact_digest": digest})
    assert client.get(f"/agent/v1/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")).status_code == 404


def test_invalid_ranges_do_not_leak_artifact_descriptors(agent_system) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"artifact").hexdigest()
    (services.artifact_root / digest).write_bytes(b"artifact")
    services.operations.enqueue(parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {"artifact_digest": digest})
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(25):
        assert client.get(f"/agent/v1/artifacts/{digest}", headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=" + "9" * 5000 + "-1"}).status_code == 416
    assert len(os.listdir("/proc/self/fd")) <= before + 1


def test_artifact_stream_close_releases_its_descriptor(tmp_path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"artifact")
    descriptor = os.open(artifact, os.O_RDONLY)
    stream = _read_chunks(descriptor, 0, 8)
    assert next(stream) == b"artifact"
    stream.close()
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_protected_agent_routes_gate_untrusted_invalid_bodies_before_parsing(agent_system) -> None:
    client, _, _, _ = agent_system
    for path in ("/agent/v1/claim", "/agent/v1/heartbeat", "/agent/v1/result", "/agent/v1/renew"):
        assert client.post(path, content=b"{not-json", headers={"content-type": "application/json"}).status_code == 401


def test_enrollment_overflow_burns_valid_grant_before_rejection(agent_system) -> None:
    client, _, codec, _ = agent_system
    grant = client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec), json={"node_id": NODE_A, "ttl_seconds": 60}).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "node")])).sign(key, algorithm=None).public_bytes(serialization.Encoding.PEM)
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    body = {"grant_token": grant["token"], "csr": csr.decode(), "evidence": {"node_id": NODE_A, "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(), "host_key_fingerprint": "x" * 513, "hardware_fingerprint": "hardware", "agent_digest": "a" * 64, "boot_id": "boot"}}
    assert client.post("/agent/v1/enroll", json=body).status_code == 403
    assert client.post("/agent/v1/enroll", json=body).status_code == 403


def test_enrollment_listing_paginates_stably_and_can_filter_issuing(agent_system) -> None:
    client, services, codec, clock = agent_system
    with services.sessions.begin() as session:
        for index in range(101):
            grant_id = str(uuid.uuid4())
            session.add(AgentEnrollmentGrant(id=grant_id, node_id=NODE_A, token_digest=hashlib.sha256(str(index).encode()).hexdigest(), created_by="admin", created_at=clock.now, expires_at=clock.now + timedelta(seconds=60)))
            session.add(AgentEnrollment(id=str(uuid.uuid4()), grant_id=grant_id, node_id=NODE_A, state="issuing" if index == 0 else "rejected", csr_public_key_pem="pem", csr_public_key_fingerprint="a" * 64, host_key_fingerprint="host", hardware_fingerprint="hardware", agent_digest="a" * 64, boot_id="boot", created_at=clock.now))
    first = client.get("/api/v1/agents/enrollments?limit=100", headers=admin_headers(codec)).json()
    assert len(first["enrollments"]) == 100
    assert first["next_cursor"]
    second = client.get(f"/api/v1/agents/enrollments?limit=100&cursor={first['next_cursor']}", headers=admin_headers(codec)).json()
    assert len(second["enrollments"]) == 1
    issuing = client.get("/api/v1/agents/enrollments?state=issuing", headers=admin_headers(codec)).json()
    assert [item["state"] for item in issuing["enrollments"]] == ["issuing"]
