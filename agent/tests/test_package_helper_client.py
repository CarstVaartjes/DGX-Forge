from __future__ import annotations

import hashlib
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_agent import package_helper_client as helper_client_module
from vonk_agent.package_helper_client import (
    PackageHelperAdapterFactory,
    PackageHelperAuthorityVerifier,
    UnixPackageHelperClient,
)
from vonk_agent.package_helper_protocol import (
    HelperExecutionBody,
    HelperProtocolError,
    HelperRequest,
    HelperResponse,
    frame_helper_message,
    receive_helper_message,
)
from vonk_agent.packages.adapter import AdapterInvocation, AdapterOperation
from vonk_agent.packages.backends import (
    Backend,
    BackendInvocation,
    NetworkPolicy,
    ResourcePolicy,
)
from vonk_agent_protocol.workload_packages import (
    PACKAGE_HELPER_AUTHORITY,
    PackageHelperGrantClaims,
    PackageHelperOperation,
    PackageHelperSignature,
    PackageObjectReceiptClaims,
    SignedPackageHelperGrant,
    SignedPackageObjectReceipt,
    package_helper_grant_signing_bytes,
    package_object_receipt_signing_bytes,
)

NOW = datetime(2033, 5, 18, 12, 0, tzinfo=UTC)
REQUEST_ID = "10000000-0000-4000-8000-000000000001"
JOB_ID = "20000000-0000-4000-8000-000000000002"
OPERATION_ID = "30000000-0000-4000-8000-000000000003"
FENCE = "40000000-0000-4000-8000-000000000004"
NODE_ID = "spk_" + "1" * 32
RELEASE = "a" * 64
GENERATION = "gen-future-stack-001"


def private_key(seed: bytes) -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed * 32)


def key_id(private: ed25519.Ed25519PrivateKey) -> str:
    return hashlib.sha256(private.public_key().public_bytes_raw()).hexdigest()


@pytest.fixture
def grant_key() -> ed25519.Ed25519PrivateKey:
    return private_key(b"g")


@pytest.fixture
def receipt_key() -> ed25519.Ed25519PrivateKey:
    return private_key(b"r")


def signed_receipt(
    private: ed25519.Ed25519PrivateKey,
    *,
    signer: ed25519.Ed25519PrivateKey | None = None,
) -> SignedPackageObjectReceipt:
    claims = PackageObjectReceiptClaims(
        1,
        PACKAGE_HELPER_AUTHORITY,
        "e" * 64,
        4096,
        "objects/sha256/" + "e" * 64,
    )
    return SignedPackageObjectReceipt(
        claims,
        PackageHelperSignature(
            "ed25519",
            key_id(private),
            (signer or private)
            .sign(package_object_receipt_signing_bytes(claims))
            .hex(),
        ),
    )


def backend_invocation(
    *,
    release_digest: str = RELEASE,
    generation: str = GENERATION,
    entrypoint: str = "bin/future-adapter-name-unknown-to-agent",
) -> BackendInvocation:
    return BackendInvocation(
        schema_version=1,
        backend=Backend.NATIVE,
        release_digest=release_digest,
        generation=generation,
        entrypoint=entrypoint,
        arguments=("health",),
        resources=ResourcePolicy(1000, 1024**3, 64, 60, 65536),
        mounts=(),
        devices=(),
        network=NetworkPolicy("none"),
    )


def execution_body(
    receipt_private: ed25519.Ed25519PrivateKey,
    *,
    operation: PackageHelperOperation = PackageHelperOperation.HEALTH,
    invocation: BackendInvocation | None = None,
    receipt_signer: ed25519.Ed25519PrivateKey | None = None,
) -> HelperExecutionBody:
    return HelperExecutionBody(
        schema_version=1,
        request_id=REQUEST_ID,
        node_id=NODE_ID,
        job_id=JOB_ID,
        operation_id=OPERATION_ID,
        attempt=1,
        fence=FENCE,
        operation=operation,
        invocation=invocation or backend_invocation(),
        receipts=(signed_receipt(receipt_private, signer=receipt_signer),),
    )


def signed_request(
    grant_private: ed25519.Ed25519PrivateKey,
    receipt_private: ed25519.Ed25519PrivateKey,
    *,
    operation: PackageHelperOperation = PackageHelperOperation.HEALTH,
    invocation: BackendInvocation | None = None,
    receipt_signer: ed25519.Ed25519PrivateKey | None = None,
) -> HelperRequest:
    body = execution_body(
        receipt_private,
        operation=operation,
        invocation=invocation,
        receipt_signer=receipt_signer,
    )
    issued = int(NOW.timestamp())
    claims = PackageHelperGrantClaims(
        1,
        PACKAGE_HELPER_AUTHORITY,
        body.request_id,
        body.node_id,
        body.job_id,
        body.operation_id,
        body.attempt,
        body.fence,
        body.invocation.release_digest,
        body.invocation.generation,
        body.operation,
        body.digest,
        issued,
        issued + 60,
    )
    grant = SignedPackageHelperGrant(
        claims,
        PackageHelperSignature(
            "ed25519",
            key_id(grant_private),
            grant_private.sign(package_helper_grant_signing_bytes(claims)).hex(),
        ),
    )
    return HelperRequest(body, grant)


def verifier(
    grant_private: ed25519.Ed25519PrivateKey,
    receipt_private: ed25519.Ed25519PrivateKey,
    *,
    clock=lambda: NOW,
) -> PackageHelperAuthorityVerifier:
    return PackageHelperAuthorityVerifier(
        grant_private.public_key(),
        receipt_private.public_key(),
        grant_key_id=key_id(grant_private),
        receipt_key_id=key_id(receipt_private),
        clock=clock,
    )


def response_for(request: HelperRequest) -> HelperResponse:
    return HelperResponse(
        1,
        request.request_id,
        "launched",
        "c" * 64,
        request.fence,
        request.body.digest,
    )


def test_verifier_accepts_independently_signed_bound_grant_and_receipts(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(grant_key, receipt_key)

    verifier(grant_key, receipt_key).verify_request(request)


def test_verifier_rejects_grant_signature_from_the_receipt_key(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(grant_key, receipt_key)
    wrong_signature = receipt_key.sign(
        package_helper_grant_signing_bytes(request.grant.claims)
    )
    object.__setattr__(request.grant.signature, "value", wrong_signature.hex())

    with pytest.raises(HelperProtocolError, match="grant signature"):
        verifier(grant_key, receipt_key).verify_request(request)


def test_verifier_rejects_receipt_signature_from_the_grant_key(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(
        grant_key,
        receipt_key,
        receipt_signer=grant_key,
    )

    with pytest.raises(HelperProtocolError, match="receipt signature"):
        verifier(grant_key, receipt_key).verify_request(request)


@pytest.mark.parametrize(
    ("clock", "message"),
    [
        (lambda: NOW - timedelta(seconds=1), "not active"),
        (lambda: NOW + timedelta(seconds=60), "expired"),
    ],
)
def test_verifier_rejects_grants_outside_the_signed_time_window(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
    clock,
    message: str,
) -> None:
    request = signed_request(grant_key, receipt_key)

    with pytest.raises(HelperProtocolError, match=message):
        verifier(grant_key, receipt_key, clock=clock).verify_request(request)


def test_client_recomputes_unsigned_body_digest_before_connecting(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(grant_key, receipt_key)
    object.__setattr__(request.grant.claims, "request_digest", "f" * 64)
    connections: list[str] = []

    def connector(path: str, _timeout: float) -> socket.socket:
        connections.append(path)
        raise AssertionError("must not connect")

    with pytest.raises(HelperProtocolError, match="bind execution body"):
        UnixPackageHelperClient(
            verifier(grant_key, receipt_key), connector=connector
        ).submit(request)

    assert connections == []


def test_client_uses_fixed_unix_socket_root_peer_and_exact_response_binding(
    monkeypatch: pytest.MonkeyPatch,
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(grant_key, receipt_key)
    server, client_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    connected: list[tuple[object, float]] = []

    monkeypatch.setattr(helper_client_module, "_unix_peer_uid", lambda _socket: 0)

    def connector(path: str, timeout: float) -> socket.socket:
        connected.append((path, timeout))
        return client_socket

    def serve() -> None:
        try:
            raw = receive_helper_message(server, timeout_seconds=1)
            received = HelperRequest.parse(raw)
            server.sendall(frame_helper_message(response_for(received).to_bytes()))
        finally:
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        response = UnixPackageHelperClient(
            verifier(grant_key, receipt_key),
            connector=connector,
            timeout_seconds=1,
        ).submit(request)
    finally:
        thread.join(2)

    assert connected == [
        (
            "/run/vonk-forge-package-helper/package-helper.sock",
            pytest.approx(1, abs=0.05),
        )
    ]
    assert response == response_for(request)


def test_unix_peer_uid_reads_kernel_authenticated_credentials() -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert helper_client_module._unix_peer_uid(client) == os.geteuid()
    finally:
        client.close()
        server.close()


def test_client_rejects_a_non_root_unix_server_before_sending(
    monkeypatch: pytest.MonkeyPatch,
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(grant_key, receipt_key)
    server, client_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    monkeypatch.setattr(helper_client_module, "_unix_peer_uid", lambda _socket: 1000)
    try:
        client = UnixPackageHelperClient(
            verifier(grant_key, receipt_key),
            connector=lambda _path, _timeout: client_socket,
            timeout_seconds=1,
        )
        with pytest.raises(HelperProtocolError, match="server is not root"):
            client.submit(request)
        server.settimeout(0.05)
        assert server.recv(1) == b""
    finally:
        server.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "90000000-0000-4000-8000-000000000009"),
        ("fence", "90000000-0000-4000-8000-000000000009"),
        ("request_digest", "f" * 64),
    ],
)
def test_client_rejects_every_response_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
    field: str,
    value: str,
) -> None:
    request = signed_request(grant_key, receipt_key)
    server, client_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    monkeypatch.setattr(helper_client_module, "_unix_peer_uid", lambda _socket: 0)

    def serve() -> None:
        try:
            receive_helper_message(server, timeout_seconds=1)
            response = response_for(request)
            object.__setattr__(response, field, value)
            server.sendall(frame_helper_message(response.to_bytes()))
        finally:
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client = UnixPackageHelperClient(
            verifier(grant_key, receipt_key),
            connector=lambda _path, _timeout: client_socket,
            timeout_seconds=1,
        )
        with pytest.raises(HelperProtocolError, match="response binding"):
            client.submit(request)
    finally:
        thread.join(2)


def test_client_read_deadline_is_bounded_and_does_not_echo_socket_errors(
    monkeypatch: pytest.MonkeyPatch,
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(grant_key, receipt_key)
    server, client_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    monkeypatch.setattr(helper_client_module, "_unix_peer_uid", lambda _socket: 0)
    client = UnixPackageHelperClient(
        verifier(grant_key, receipt_key),
        connector=lambda _path, _timeout: client_socket,
        timeout_seconds=0.02,
    )
    try:
        with pytest.raises(HelperProtocolError) as caught:
            client.submit(request)
    finally:
        server.close()

    assert "deadline" in str(caught.value)
    assert "timed out" not in str(caught.value)


def test_client_read_deadline_is_absolute_across_trickled_chunks(
    monkeypatch: pytest.MonkeyPatch,
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(grant_key, receipt_key)
    server, client_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    monkeypatch.setattr(helper_client_module, "_unix_peer_uid", lambda _socket: 0)

    def serve() -> None:
        try:
            receive_helper_message(server, timeout_seconds=1)
            framed = frame_helper_message(response_for(request).to_bytes())
            for offset in range(0, len(framed), 16):
                time.sleep(0.008)
                try:
                    server.sendall(framed[offset : offset + 16])
                except OSError:
                    break
        finally:
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client = UnixPackageHelperClient(
            verifier(grant_key, receipt_key),
            connector=lambda _path, _timeout: client_socket,
            timeout_seconds=0.02,
        )
        with pytest.raises(HelperProtocolError, match="deadline"):
            client.submit(request)
    finally:
        thread.join(2)


def test_client_connection_failure_does_not_leak_raw_diagnostics(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
) -> None:
    request = signed_request(grant_key, receipt_key)

    def connector(_path: str, _timeout: float) -> socket.socket:
        raise OSError("token=do-not-leak /private/path")

    client = UnixPackageHelperClient(
        verifier(grant_key, receipt_key), connector=connector
    )
    with pytest.raises(HelperProtocolError) as caught:
        client.submit(request)

    assert str(caught.value) == "package helper connection failed"


@dataclass(frozen=True)
class Lock:
    digest: str


class RecordingClient:
    def __init__(self) -> None:
        self.requests: list[HelperRequest] = []
        self.deadlines: list[object] = []

    def submit(self, request: HelperRequest, *, deadline=None) -> HelperResponse:
        self.requests.append(request)
        self.deadlines.append(deadline)
        return response_for(request)


def test_adapter_factory_is_package_engine_compatible_for_unknown_adapter_names(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    client = RecordingClient()
    adapter_invocation = AdapterInvocation(
        JOB_ID, OPERATION_ID, 1, FENCE, RELEASE, GENERATION, NODE_ID
    )
    deadline = NOW + timedelta(minutes=1)

    def request_factory(
        lock,
        generation_id,
        generation_path,
        objects,
        operation,
        invocation,
        supplied_deadline,
    ) -> HelperRequest:
        assert (lock.digest, generation_id, generation_path, objects) == (
            RELEASE,
            GENERATION,
            tmp_path / RELEASE,
            {"receipt": "opaque"},
        )
        assert supplied_deadline is deadline
        return signed_request(
            grant_key,
            receipt_key,
            operation=PackageHelperOperation(operation.value),
            invocation=backend_invocation(
                release_digest=invocation.release_digest,
                generation=invocation.generation,
                entrypoint="bin/a-new-adapter-never-compiled-into-vonk-forge",
            ),
        )

    factory = PackageHelperAdapterFactory(client, request_factory)
    adapter = factory(
        Lock(RELEASE),
        GENERATION,
        tmp_path / RELEASE,
        {"receipt": "opaque"},
    )

    evidence = adapter.execute(AdapterOperation.HEALTH, adapter_invocation, deadline)

    assert evidence.operation is AdapterOperation.HEALTH
    assert evidence.status == "launched"
    assert evidence.release_digest == RELEASE
    assert evidence.generation == GENERATION
    assert evidence.fence == FENCE
    assert client.requests[0].invocation.entrypoint.endswith(
        "a-new-adapter-never-compiled-into-vonk-forge"
    )
    assert client.deadlines == [deadline]


def test_adapter_executor_rejects_a_factory_request_for_another_operation(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    client = RecordingClient()
    factory = PackageHelperAdapterFactory(
        client,
        lambda *_args: signed_request(
            grant_key,
            receipt_key,
            operation=PackageHelperOperation.START,
        ),
    )
    adapter = factory(Lock(RELEASE), GENERATION, tmp_path / RELEASE, {})
    invocation = AdapterInvocation(
        JOB_ID, OPERATION_ID, 1, FENCE, RELEASE, GENERATION, NODE_ID
    )

    with pytest.raises(HelperProtocolError, match="adapter request binding"):
        adapter.execute(AdapterOperation.HEALTH, invocation, NOW + timedelta(minutes=1))

    assert client.requests == []


def test_adapter_executor_rejects_a_factory_request_for_another_node(
    grant_key: ed25519.Ed25519PrivateKey,
    receipt_key: ed25519.Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    client = RecordingClient()
    factory = PackageHelperAdapterFactory(
        client,
        lambda *_args: signed_request(grant_key, receipt_key),
    )
    adapter = factory(Lock(RELEASE), GENERATION, tmp_path / RELEASE, {})
    invocation = AdapterInvocation(
        JOB_ID,
        OPERATION_ID,
        1,
        FENCE,
        RELEASE,
        GENERATION,
        "spk_" + "2" * 32,
    )

    with pytest.raises(HelperProtocolError, match="adapter request binding"):
        adapter.execute(AdapterOperation.HEALTH, invocation, NOW + timedelta(minutes=1))

    assert client.requests == []
