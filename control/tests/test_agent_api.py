from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
    app = create_app(jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=lambda: {}, now=lambda: 0, agent=services, trusted_agent_proxy_auth=b"p" * 32)
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
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "node")]))
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
        audits=MemoryAuditStore(), fleet=lambda: {},
    )

    response = TestClient(app).post("/agent/v1/claim", headers={"x-dgx-agent-node": NODE_A})

    assert response.status_code == 401


def test_unauthenticated_agent_gate_returns_without_reading_request_body() -> None:
    app = create_app(
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(), fleet=lambda: {},
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


def test_enrollment_rate_limit_rejects_before_reading_request_body(agent_system) -> None:
    _, services, codec, _ = agent_system
    limiter = EnrollmentRateLimiter(maximum=1, window_seconds=60, clock=lambda: 0.0)
    app = create_app(
        jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=lambda: {}, agent=services,
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


def test_enrollment_overflow_burns_valid_grant_before_rejection(agent_system) -> None:
    client, _, codec, _ = agent_system
    grant = client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec), json={"node_id": NODE_A, "ttl_seconds": 60}).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "node")])).sign(key, algorithm=None).public_bytes(serialization.Encoding.PEM)
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    body = {"grant_token": grant["token"], "csr": csr.decode(), "evidence": {"node_id": NODE_A, "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(), "host_key_fingerprint": "x" * 513, "hardware_fingerprint": "hardware", "agent_digest": "a" * 64, "boot_id": "boot"}}
    assert client.post("/agent/v1/enroll", json=body).status_code == 403
    assert client.post("/agent/v1/enroll", json=body).status_code == 403


def test_enrollment_unknown_top_level_field_burns_valid_grant(agent_system) -> None:
    client, _, codec, _ = agent_system
    grant = client.post("/api/v1/agents/enrollments/grants", headers=admin_headers(codec), json={"node_id": NODE_A, "ttl_seconds": 60}).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "node")])).sign(key, algorithm=None).public_bytes(serialization.Encoding.PEM)
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
            session.add(AgentEnrollment(id=str(uuid.uuid4()), grant_id=grant_id, node_id=NODE_A, state="issuing" if index == 0 else "rejected", csr_public_key_pem="pem", csr_public_key_fingerprint="a" * 64, host_key_fingerprint="host", hardware_fingerprint="hardware", agent_digest="a" * 64, boot_id="boot", created_at=clock.now))
    first = client.get("/api/v1/agents/enrollments?limit=100", headers=admin_headers(codec)).json()
    assert len(first["enrollments"]) == 100
    assert first["next_cursor"]
    second = client.get(f"/api/v1/agents/enrollments?limit=100&cursor={first['next_cursor']}", headers=admin_headers(codec)).json()
    assert len(second["enrollments"]) == 1
    issuing = client.get("/api/v1/agents/enrollments?state=issuing", headers=admin_headers(codec)).json()
    assert [item["state"] for item in issuing["enrollments"]] == ["issuing"]
