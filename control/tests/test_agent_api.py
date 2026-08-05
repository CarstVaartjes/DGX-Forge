from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from dgx_agent_protocol import canonical_message
from dgx_control.agent_api import (
    AgentApiServices,
    EnrollmentRateLimiter,
    _bounded_enrollment_body,
    _read_chunks,
    _sealed_snapshot,
)
from dgx_control.agent_jobs import AgentJobService
from dgx_control.api import create_app
from dgx_control.audit import MemoryAuditStore
from dgx_control.auth import Actor, TokenCodec
from dgx_control.enrollment import EnrollmentDenied, EnrollmentService
from dgx_control.metrics import MetricsRegistry, OperationalMetricsCollector
from dgx_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentNode,
    AgentOperationAttempt,
    Base,
    Job,
    Observation,
)
from dgx_control.pki import CertificateAuthority, IssuedCertificate
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
NODE_C = "spk_" + "c" * 32
CAPABILITIES = [
    "node.probe",
    "release.install",
    "workload.health",
    "workload.prepare",
    "workload.start",
    "workload.stop",
    "workload.verify",
]
PROBE_RESULT = {
    "status": "ok",
    "evidence": {
        "dgx_forge": {
            "schema_version": 1,
            "memory": {"available_bytes": 1_000},
            "storage": {"available_bytes": 2_000},
            "accelerator": {"available": True},
        },
        "nvidia": {"tools": {}},
    },
}


class Jobs:
    def list(self): return []
    def get(self, _): raise KeyError
    def enqueue(self, *_args, **_kwargs): raise AssertionError


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class ChunkedEnrollmentRequest:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.received = 0

    async def stream(self):
        for chunk in self.chunks:
            self.received += 1
            yield chunk


class CopyBoundedChunk(bytes):
    def __new__(cls, value: bytes):
        instance = super().__new__(cls, value)
        instance.largest_slice = 0
        return instance

    def __getitem__(self, key):
        if isinstance(key, slice):
            start = key.start or 0
            stop = len(self) if key.stop is None else key.stop
            self.largest_slice = max(self.largest_slice, max(0, stop - start))
        return super().__getitem__(key)

    def __radd__(self, _other):
        raise AssertionError("an incoming ASGI chunk must never be concatenated whole")


class Authority(CertificateAuthority):
    def __init__(self) -> None:
        self.fail_revoke = False

    def issue_node(self, node_id: str, public_key_pem: bytes, now: datetime) -> IssuedCertificate:
        return IssuedCertificate(node_id, b"certificate", b"chain", "issued-serial", "issued-fingerprint", now, now + timedelta(days=1))

    def renew_node(
        self,
        node_id: str,
        public_key_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        return self.issue_node(node_id, public_key_pem, now)

    def revocation_bundle(self, now: datetime) -> bytes:
        return b""

    def revoke_node(self, serial: str, now: datetime) -> None:
        if self.fail_revoke:
            raise RuntimeError("provider unavailable")


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
    app = create_app(jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=dict, now=lambda: 0, agent=services, trusted_agent_proxy_auth=b"p" * 32)
    return TestClient(app), services, codec, clock


def agent_headers(node: str, serial: str) -> dict[str, str]:
    return {
        "x-dgx-agent-node": node,
        "x-dgx-agent-serial": serial,
        "x-dgx-agent-fingerprint": f"fingerprint-{serial}",
        "x-dgx-agent-verified": "1",
        "x-dgx-agent-proxy-auth": "p" * 32,
    }


def admin_headers(codec: TokenCodec, role: str = "administrator") -> dict[str, str]:
    return {"Authorization": f"Bearer {codec.issue(Actor(role, role), ttl_seconds=100, now=0)}"}


def enrollment_grant(services: AgentApiServices) -> str:
    return services.enrollment.create(NODE_A, "administrator", 60).token


def assert_grant_consumed(services: AgentApiServices, token: str) -> None:
    with pytest.raises(EnrollmentDenied, match="consumed"):
        services.enrollment.submit(token, b"", {})


def valid_enrollment_body(token: str) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_A)]))
        .add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{NODE_A}")
        ]), critical=False)
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return json.dumps({
        "grant_token": token,
        "csr": csr.decode("ascii"),
        "evidence": {
            "node_id": NODE_A,
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "host_key_fingerprint": "host",
            "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64,
            "boot_id": "boot",
        },
    }).encode("utf-8")


def asgi_post(app, path: str, body: bytes, *, content_type: str = "application/json") -> tuple[int, bytes]:
    async def request() -> tuple[int, bytes]:
        sent: list[dict[str, object]] = []
        delivered = False

        async def receive() -> dict[str, object]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode("ascii"), "query_string": b"",
            "headers": ((b"content-type", content_type.encode("ascii")),),
            "client": ("testclient", 1234), "server": ("testserver", 80),
            "root_path": "", "state": {},
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=1)
        start = next(message for message in sent if message["type"] == "http.response.start")
        content = b"".join(
            message.get("body", b"")  # type: ignore[arg-type]
            for message in sent if message["type"] == "http.response.body"
        )
        return int(start["status"]), content

    return asyncio.run(request())


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
        audits=MemoryAuditStore(), fleet=dict,
    )

    response = TestClient(app).post("/agent/v1/claim", headers={"x-dgx-agent-node": NODE_A})

    assert response.status_code == 401


def test_unauthenticated_agent_gate_returns_without_reading_request_body() -> None:
    app = create_app(
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(), fleet=dict,
    )
    sent: list[dict[str, object]] = []
    body_reads = 0

    async def receive() -> dict[str, object]:
        nonlocal body_reads
        body_reads += 1
        return {"type": "http.request", "body": b"untrusted", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/agent/v1/claim", "raw_path": b"/agent/v1/claim",
        "query_string": b"", "headers": (), "client": ("untrusted", 1234),
        "server": ("testserver", 80), "root_path": "", "state": {},
    }

    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=0.5))

    start = next(message for message in sent if message["type"] == "http.response.start")
    headers = dict(start["headers"])  # type: ignore[arg-type]
    assert start["status"] == 401
    assert body_reads == 0
    assert headers[b"x-content-type-options"] == b"nosniff"
    uuid.UUID(headers[b"x-request-id"].decode("ascii"))


def test_agent_routes_do_not_require_human_bearer_tokens() -> None:
    app = create_app(
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(), fleet=dict,
    )

    response = TestClient(app).post("/agent/v1/claim", headers={"Authorization": "Bearer invalid"})

    assert response.status_code == 401


def test_untrusted_proxy_and_malformed_forwarded_identity_are_rejected(agent_system) -> None:
    client, _, _, _ = agent_system
    assert client.post("/agent/v1/claim").status_code == 401
    assert client.post("/agent/v1/claim", headers={**agent_headers(NODE_A, "serial-a"), "x-dgx-agent-verified": "false"}).status_code == 401

    app = create_app(jobs=Jobs(), tokens=TokenCodec(b"k" * 32), audits=MemoryAuditStore(), fleet=dict)
    assert TestClient(app).post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")).status_code == 401


def test_verified_identity_cannot_claim_other_node(agent_system) -> None:
    client, _, _, _ = agent_system
    response = client.post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a"), json={"node_id": NODE_B})
    assert response.status_code == 403


def test_authenticated_claim_records_protocol_contact_for_metrics(agent_system) -> None:
    client, services, _, clock = agent_system
    clock.now += timedelta(seconds=15)

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES,
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at.replace(tzinfo=UTC) == clock.now
        assert node.protocol_version == 1
        assert node.capabilities == CAPABILITIES
    metrics = MetricsRegistry()
    OperationalMetricsCollector(metrics, services.sessions, clock=clock).refresh()
    rendered = metrics.render()
    assert f'dgx_agent_last_seen_age_seconds{{node_id="{NODE_A}"}} 0' in rendered
    assert (
        f'dgx_agent_version_compatibility{{node_id="{NODE_A}",version_bucket="supported"}} 1'
        in rendered
    )


def test_unknown_claim_capability_is_rejected_without_contact(agent_system) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES + ["shell.exec"],
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 422
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.capabilities == []
        assert node.protocol_version is None
        assert node.last_seen_at is None


def test_authenticated_heartbeat_preserves_claim_advertised_protocol_after_exact_fence_validation(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 2},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = None
    clock.now += timedelta(seconds=5)
    progress = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {"progress": {"phase": "checking"}}

    response = client.post(
        "/agent/v1/heartbeat",
        headers=agent_headers(NODE_A, "serial-a"),
        json=progress,
    )

    assert response.status_code == 200
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at.replace(tzinfo=UTC) == clock.now
        assert node.protocol_version == 2


def test_authenticated_result_preserves_claim_advertised_protocol_after_exact_fence_validation(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 2},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = None
    clock.now += timedelta(seconds=5)
    result = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {"state": "succeeded", "result": PROBE_RESULT}

    response = client.post(
        "/agent/v1/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=result,
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at.replace(tzinfo=UTC) == clock.now
        assert node.protocol_version == 2


def test_exact_fenced_probe_success_writes_bounded_durable_health(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES,
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "wait_seconds": 0,
        },
    ).json()
    clock.now += timedelta(seconds=2)
    result = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {"state": "succeeded", "result": PROBE_RESULT}

    response = client.post(
        "/agent/v1/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=result,
    )

    assert response.status_code == 204
    with services.sessions() as session:
        observations = list(
            session.scalars(
                select(Observation).where(Observation.node_id == NODE_A)
            )
        )
        assert len(observations) == 1
        assert observations[0].kind == "health"
        assert observations[0].observed_at.replace(tzinfo=UTC) == clock.now
        assert observations[0].payload == {
            "disk_available_bytes": 2_000,
            "memory_available_bytes": 1_000,
            "status": "healthy",
        }


def test_failed_probe_result_never_writes_health_observation(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {
        "state": "failed",
        "result": {"status": "failed", "error_code": "probe_failed"},
    }

    assert client.post(
        "/agent/v1/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=result,
    ).status_code == 204
    with services.sessions() as session:
        assert session.scalar(select(Observation)) is None


def test_untrusted_and_stale_requests_do_not_record_agent_contact(agent_system) -> None:
    client, services, _, clock = agent_system
    untrusted = client.post(
        "/agent/v1/claim",
        json={
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "wait_seconds": 0,
        },
    )
    assert untrusted.status_code == 401
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at is None
        assert node.protocol_version is None

    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = None
        node.protocol_version = None
    stale = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "node_id",
            "deadline",
        )
    } | {
        "fence": str(uuid.uuid4()),
        "state": "succeeded",
        "result": {"healthy": True},
    }

    rejected = client.post(
        "/agent/v1/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=stale,
    )

    assert rejected.status_code == 409
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at is None
        assert node.protocol_version is None


def test_boolean_protocol_advertisement_is_rejected_without_recording_contact(
    agent_system,
) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": True,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 422
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at is None
        assert node.protocol_version is None


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


def test_enrollment_routes_are_admin_only_and_pending_exact_replay_is_idempotent(agent_system) -> None:
    client, _, codec, _ = agent_system
    assert client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec, "operator"), json={"node_id": NODE_A, "ttl_seconds": 60}).status_code == 403
    # Existing node is deliberately unrelated to submitting a one-use grant;
    # approval remains the point that rejects a duplicate immutable node.
    grant = client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec), json={"node_id": NODE_A, "ttl_seconds": 60}).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_A)])).add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{NODE_A}")]), critical=False).sign(key, algorithm=None).public_bytes(serialization.Encoding.PEM)
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    body = {"grant_token": grant["token"], "csr": csr.decode(), "evidence": {"node_id": NODE_A, "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(), "host_key_fingerprint": "host", "hardware_fingerprint": "hardware", "agent_digest": "a" * 64, "boot_id": "boot"}}
    first = client.post("/agent/v1/enroll", json=body)
    replay = client.post("/agent/v1/enroll", json=body)
    assert first.status_code == replay.status_code == 202
    assert first.content == replay.content == canonical_message(first.json())


def test_approved_exact_enrollment_replay_picks_up_certificate_and_mismatch_is_denied(
    agent_system,
) -> None:
    client, services, codec, _ = agent_system
    grant = services.enrollment.create(NODE_C, "administrator", 60)
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_C)]))
        .add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{NODE_C}")
        ]), critical=False)
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    body = {
        "grant_token": grant.token,
        "csr": csr.decode(),
        "evidence": {
            "node_id": NODE_C,
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "host_key_fingerprint": "host",
            "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64,
            "boot_id": "boot",
        },
    }
    pending = client.post("/agent/v1/enroll", json=body)
    enrollment_id = pending.json()["id"]
    assert client.post(
        f"/api/v1/agents/enrollments/{enrollment_id}/approve",
        headers=admin_headers(codec),
    ).status_code == 200

    pickup = client.post("/agent/v1/enroll", json=body)
    mismatch = client.post(
        "/agent/v1/enroll",
        json={**body, "evidence": {**body["evidence"], "boot_id": "different"}},
    )

    assert pickup.status_code == 200
    assert pickup.content == canonical_message(pickup.json())
    assert pickup.json()["generation"] == 1
    assert "certificate_pem" in pickup.json()
    assert mismatch.status_code == 403
    assert "certificate" not in mismatch.text.lower()


def _csr_for(node_id: str) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, node_id)]))
        .add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{node_id}")
        ]), critical=False)
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )


def _csr_fingerprint(csr_pem: bytes) -> str:
    public_key = x509.load_pem_x509_csr(csr_pem).public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(public_key).hexdigest()


def test_fresh_rotation_follower_receives_canonical_retryable_response(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    request = _csr_for(NODE_A)
    with services.sessions.begin() as session:
        session.add(AgentCertificateRotation(
            node_id=NODE_A,
            source_serial="serial-a",
            generation=2,
            csr_pem=request.decode("ascii"),
            csr_public_key_fingerprint=_csr_fingerprint(request),
            provider_request_id="r" * 43,
            state="issuing",
            created_at=clock.now,
            updated_at=clock.now,
        ))

    response = client.post(
        "/agent/v1/renew",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_A, "csr": request.decode()},
    )

    assert response.status_code == 503
    assert response.content == canonical_message(response.json())
    assert response.json() == {
        "detail": "certificate rotation issuance is in progress"
    }


def test_staged_certificate_can_only_activate_and_activation_is_idempotent_after_response_loss(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    csr = _csr_for(NODE_A)
    first = client.post(
        "/agent/v1/renew",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_A, "csr": csr.decode()},
    )
    replay = client.post(
        "/agent/v1/renew",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_A, "csr": csr.decode()},
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    issued = first.json()
    staged_headers = agent_headers(NODE_A, issued["serial"])
    staged_headers["x-dgx-agent-fingerprint"] = issued["fingerprint"]

    assert client.post("/agent/v1/claim", headers=staged_headers).status_code == 401
    assert client.post(
        "/agent/v1/heartbeat", headers=staged_headers, json={"invalid": True}
    ).status_code == 401
    assert client.post(
        "/agent/v1/result", headers=staged_headers, json={"invalid": True}
    ).status_code == 401
    assert client.post(
        "/agent/v1/renew", headers=staged_headers,
        json={"node_id": NODE_A, "csr": _csr_for(NODE_A).decode()},
    ).status_code == 401
    assert client.get(
        "/agent/v1/artifacts/" + "a" * 64,
        headers=staged_headers,
    ).status_code == 401

    activation = {"node_id": NODE_A, "generation": issued["generation"]}
    assert client.post(
        "/agent/v1/renew/activate", headers=staged_headers, json=activation
    ).status_code == 204
    assert client.post(
        "/agent/v1/renew/activate", headers=staged_headers, json=activation
    ).status_code == 204
    assert client.post(
        "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
    ).status_code == 401
    assert client.post("/agent/v1/claim", headers=staged_headers).status_code == 204
    with services.sessions() as session:
        old = session.get(AgentCertificate, "serial-a")
        new = session.get(AgentCertificate, issued["serial"])
        assert old is not None and old.state == "revoked" and old.revoked_at is not None
        assert new is not None and new.state == "active" and new.revoked_at is None


def test_failed_result_maps_stable_error_code_to_bounded_failure_reason(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {
        key: claim[key]
        for key in (
            "schema_version", "job_id", "operation_id", "attempt", "fence",
            "node_id", "deadline",
        )
    } | {
        "state": "failed",
        "result": {"status": "failed", "error_code": "probe_failed"},
    }

    response = client.post(
        "/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == 204
    with services.sessions() as session:
        attempt = session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        assert attempt.result == {"reason": "probe_failed"}


def test_agent_validation_errors_are_canonical_json(agent_system) -> None:
    client, _, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"lease_seconds": 0, "node_id": NODE_A, "wait_seconds": 0},
    )

    assert response.status_code == 422
    assert response.content == canonical_message(response.json())


def test_claim_endpoint_long_poll_wakes_when_work_is_enqueued(agent_system) -> None:
    client, services, _, clock = agent_system
    parent_job = parent(services.sessions, clock)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(
            client.post,
            "/agent/v1/claim",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"node_id": NODE_A, "lease_seconds": 30, "wait_seconds": 1},
        )
        time.sleep(0.05)
        operation = services.operations.enqueue(
            parent_job.id, NODE_A, "node.probe", "a" * 40, {}
        )
        response = waiting.result(timeout=1)

    assert response.status_code == 200
    assert response.json()["operation_id"] == operation.id
    assert time.monotonic() - started < 0.8


def test_enrollment_rate_limit_rejects_before_reading_request_body(agent_system) -> None:
    _, services, codec, _ = agent_system
    limiter = EnrollmentRateLimiter(maximum=1, window_seconds=60, clock=lambda: 0.0)
    app = create_app(
        jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=dict, agent=services,
        enrollment_rate_limiter=limiter,
    )
    assert asgi_post(app, "/agent/v1/enroll", valid_enrollment_body(enrollment_grant(services)))[0] == 202
    sent: list[dict[str, object]] = []
    reads = 0

    async def receive() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"never-read", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/agent/v1/enroll", "raw_path": b"/agent/v1/enroll", "query_string": b"",
        "headers": ((b"content-type", b"application/json"),), "client": ("testclient", 1234),
        "server": ("testserver", 80), "root_path": "", "state": {},
    }
    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=0.5))

    assert next(message for message in sent if message["type"] == "http.response.start")["status"] == 429
    assert reads == 0


def test_duplicate_enrollment_grants_consume_unicode_escaped_token_values(agent_system) -> None:
    client, services, _, _ = agent_system
    first = enrollment_grant(services)
    second = enrollment_grant(services)
    escaped_second = "".join(f"\\u{ord(character):04x}" for character in second)
    raw = (
        f'{{"grant_token":"{first}","gr\\u0061nt_token":"{escaped_second}"}}'
    ).encode("ascii")

    status_code, _ = asgi_post(client.app, "/agent/v1/enroll", raw)

    assert status_code == 422
    assert_grant_consumed(services, first)
    assert_grant_consumed(services, second)


def test_normal_enrollment_object_still_succeeds(agent_system) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, response = asgi_post(
        client.app,
        "/agent/v1/enroll",
        valid_enrollment_body(token),
    )

    assert status_code == 202
    assert json.loads(response)["state"] == "pending-approval"


def test_oversized_enrollment_preserves_split_discovery_prefix(agent_system) -> None:
    _, services, _, _ = agent_system
    token = enrollment_grant(services)
    first = b" " * 1000 + b'{"grant_to'
    second = b'ken":"' + token.encode("ascii") + b'","padding":"' + b"x" * (64 * 1024)
    request = ChunkedEnrollmentRequest(first, second, b"must-not-be-received")

    with pytest.raises(HTTPException) as denied:
        asyncio.run(_bounded_enrollment_body(request, services))  # type: ignore[arg-type]

    assert denied.value.status_code == 413
    assert request.received == 2
    assert_grant_consumed(services, token)


def test_one_huge_enrollment_chunk_is_only_copied_through_fixed_prefix(agent_system) -> None:
    _, services, _, _ = agent_system
    token = enrollment_grant(services)
    huge = CopyBoundedChunk(
        b'{"grant_token":"' + token.encode("ascii") + b'","padding":"' + b"x" * (1024 * 1024)
    )
    request = ChunkedEnrollmentRequest(huge, b"must-not-be-received")

    with pytest.raises(HTTPException) as denied:
        asyncio.run(_bounded_enrollment_body(request, services))  # type: ignore[arg-type]

    assert denied.value.status_code == 413
    assert request.received == 1
    assert huge.largest_slice <= 2048
    assert_grant_consumed(services, token)


@pytest.mark.parametrize(
    "raw",
    (b"[1]", b"[]", b'"scalar"', b"0", b"true", b"false", b"null"),
    ids=("array", "empty-array", "string", "number", "true", "false", "null"),
)
def test_enrollment_rejects_non_object_json_without_server_error(agent_system, raw: bytes) -> None:
    client, _, _, _ = agent_system

    status_code, _ = asgi_post(client.app, "/agent/v1/enroll", raw)

    assert status_code == 422


def test_non_object_enrollment_consumes_identifiable_nested_grant(agent_system) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)
    raw = f'[{{"grant_token":"{token}"}}]'.encode("ascii")

    status_code, _ = asgi_post(client.app, "/agent/v1/enroll", raw)

    assert status_code == 422
    assert_grant_consumed(services, token)


def test_service_denied_enrollment_consumes_every_discovered_grant(agent_system) -> None:
    client, services, _, _ = agent_system
    effective = enrollment_grant(services)
    nested = enrollment_grant(services)
    body = json.loads(valid_enrollment_body(effective))
    body["evidence"]["extra"] = {"grant_token": nested}

    status_code, _ = asgi_post(
        client.app,
        "/agent/v1/enroll",
        json.dumps(body).encode("utf-8"),
    )

    assert status_code == 403
    assert_grant_consumed(services, effective)
    assert_grant_consumed(services, nested)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    (
        (b'{"grant_token":"', b'",]'),
        (b'{"grant_token":"', b'","invalid-utf8":"\xff"}'),
        (b"[" * 1500 + b'{"grant_token":"', b'"}' + b"]" * 1500),
    ),
    ids=("malformed-json", "invalid-utf8", "deep-nesting"),
)
def test_invalid_enrollment_json_consumes_identifiable_grant(
    agent_system,
    prefix: bytes,
    suffix: bytes,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, _ = asgi_post(
        client.app,
        "/agent/v1/enroll",
        prefix + token.encode("ascii") + suffix,
    )

    assert status_code == 422
    assert_grant_consumed(services, token)


def test_wrong_enrollment_content_type_consumes_identifiable_grant(agent_system) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, _ = asgi_post(
        client.app,
        "/agent/v1/enroll",
        f'{{"grant_token":"{token}"}}'.encode("ascii"),
        content_type="text/plain",
    )

    assert status_code == 415
    assert_grant_consumed(services, token)


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


def test_artifact_snapshot_is_immutable_after_source_overwrite(tmp_path) -> None:
    source = tmp_path / "artifact"
    source.write_bytes(b"original")
    descriptor = os.open(source, os.O_RDONLY)
    snapshot = _sealed_snapshot(descriptor, 8, 1024, hashlib.sha256(b"original").hexdigest())
    source.write_bytes(b"replaced")
    try:
        assert snapshot.read() == b"original"
    finally:
        snapshot.close()


def test_snapshot_allocation_failure_closes_source_descriptor(tmp_path, monkeypatch) -> None:
    source = tmp_path / "artifact"
    source.write_bytes(b"original")
    descriptor = os.open(source, os.O_RDONLY)
    monkeypatch.setattr("dgx_control.agent_api.tempfile.TemporaryFile", lambda **_kwargs: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError, match="full"):
        _sealed_snapshot(descriptor, 8, 1024, hashlib.sha256(b"original").hexdigest())
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_protected_agent_routes_gate_untrusted_invalid_bodies_before_parsing(agent_system) -> None:
    client, _, _, _ = agent_system
    for path in ("/agent/v1/claim", "/agent/v1/heartbeat", "/agent/v1/result", "/agent/v1/renew"):
        assert client.post(path, content=b"{not-json", headers={"content-type": "application/json"}).status_code == 401


def test_revoked_identity_is_gated_before_invalid_json_is_parsed(agent_system) -> None:
    client, services, _, clock = agent_system
    with services.sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").revoked_at = clock.now  # type: ignore[union-attr]
    assert client.post("/agent/v1/result", headers={**agent_headers(NODE_A, "serial-a"), "content-type": "application/json"}, content=b"{not-json").status_code == 401


def test_node_revocation_has_typed_4xx_and_uncertain_remote_statuses(agent_system) -> None:
    client, services, codec, _ = agent_system
    headers = admin_headers(codec)
    assert client.post("/api/v1/agents/nodes/not-canonical/revoke", headers=headers).status_code == 422
    assert client.post(f"/api/v1/agents/nodes/{'spk_' + '1' * 32}/revoke", headers=headers).status_code == 404

    authority = services.enrollment._authority
    authority.fail_revoke = True
    response = client.post(f"/api/v1/agents/nodes/{NODE_A}/revoke", headers=headers)
    assert response.status_code == 503
    with services.sessions() as session:
        assert session.get(AgentNode, NODE_A).state == "retired"  # type: ignore[union-attr]
        assert session.get(AgentCertificate, "serial-a").revoked_at is not None  # type: ignore[union-attr]


def test_enrollment_overflow_burns_valid_grant_before_rejection(agent_system) -> None:
    client, _, codec, _ = agent_system
    grant = client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec), json={"node_id": NODE_A, "ttl_seconds": 60}).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_A)])).add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{NODE_A}")]), critical=False).sign(key, algorithm=None).public_bytes(serialization.Encoding.PEM)
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    body = {"grant_token": grant["token"], "csr": csr.decode(), "evidence": {"node_id": NODE_A, "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(), "host_key_fingerprint": "x" * 513, "hardware_fingerprint": "hardware", "agent_digest": "a" * 64, "boot_id": "boot"}}
    assert client.post("/agent/v1/enroll", json=body).status_code == 403
    assert client.post("/agent/v1/enroll", json=body).status_code == 403


def test_enrollment_unknown_top_level_field_burns_valid_grant(agent_system) -> None:
    client, _, codec, _ = agent_system
    grant = client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec), json={"node_id": NODE_A, "ttl_seconds": 60}).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_A)])).add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{NODE_A}")]), critical=False).sign(key, algorithm=None).public_bytes(serialization.Encoding.PEM)
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    body = {"grant_token": grant["token"], "csr": csr.decode(), "evidence": {"node_id": NODE_A, "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(), "host_key_fingerprint": "host", "hardware_fingerprint": "hardware", "agent_digest": "a" * 64, "boot_id": "boot"}, "unknown": "denied"}
    assert client.post("/agent/v1/enroll", json=body).status_code == 403
    assert client.post("/agent/v1/enroll", json=body).status_code == 403


def test_enrollment_listing_paginates_stably_and_can_filter_issuing(agent_system) -> None:
    client, services, codec, clock = agent_system
    with services.sessions.begin() as session:
        for index in range(101):
            grant_id = str(uuid.uuid4())
            session.add(AgentEnrollmentGrant(id=grant_id, node_id=NODE_A, token_digest=hashlib.sha256(str(index).encode()).hexdigest(), created_by="admin", created_at=clock.now, expires_at=clock.now + timedelta(seconds=60)))
            session.add(AgentEnrollment(id=str(uuid.uuid4()), grant_id=grant_id, node_id=NODE_A, state="issuing" if index == 0 else "rejected", csr_pem="csr", csr_public_key_pem="pem", csr_public_key_fingerprint="a" * 64, host_key_fingerprint="host", hardware_fingerprint="hardware", agent_digest="a" * 64, boot_id="boot", created_at=clock.now))
    first = client.get("/api/v1/agents/enrollments?limit=100", headers=admin_headers(codec)).json()
    assert len(first["enrollments"]) == 100
    assert first["next_cursor"]
    second = client.get(f"/api/v1/agents/enrollments?limit=100&cursor={first['next_cursor']}", headers=admin_headers(codec)).json()
    assert len(second["enrollments"]) == 1
    issuing = client.get("/api/v1/agents/enrollments?state=issuing", headers=admin_headers(codec)).json()
    assert [item["state"] for item in issuing["enrollments"]] == ["issuing"]
