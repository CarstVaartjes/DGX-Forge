from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
import uuid

import pytest
from vonk_control.workload_signer import (
    UnixWorkloadSignerClient,
    WorkloadPublicationSignerPolicy,
    WorkloadSignerConnectionHandler,
    WorkloadSignerProtocolError,
    _canonical,
    _document,
    _read_message,
)
from vonk_control.workload_trust import TrustedWorkloadTarget

LOCK = b'{"family_id":"future-stack","schema_version":1}'
EVIDENCE = {
    "lock_digest": hashlib.sha256(LOCK).hexdigest(),
    "provenance_digest": "b" * 64,
    "sbom_digest": "c" * 64,
    "schema_version": 1,
}
COMMIT = "a" * 40


class FakePublisher:
    def publish(self, lock: bytes, commit: str, evidence: dict[str, object]) -> TrustedWorkloadTarget:
        assert (lock, commit, evidence) == (LOCK, COMMIT, EVIDENCE)
        return TrustedWorkloadTarget(hashlib.sha256(lock).hexdigest(), len(lock), commit, 4)


def _connection_pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def test_workload_signer_client_and_handler_bind_exact_publish_request() -> None:
    left, right = _connection_pair()
    handler = WorkloadSignerConnectionHandler(FakePublisher(), allowed_peer_uid=os.geteuid())
    request = {
        "schema_version": 1,
        "intent_id": str(uuid.uuid4()),
        "action": "workload.publish",
        "lock": base64.b64encode(LOCK).decode("ascii"),
        "git_commit": COMMIT,
        "evidence": EVIDENCE,
    }

    def serve() -> None:
        with right:
            handler.handle(right)

    thread = threading.Thread(target=serve)
    thread.start()
    raw = _canonical(request)
    left.sendall(raw)
    left.shutdown(socket.SHUT_WR)
    response = UnixWorkloadSignerClient._decode_response(
        _document(_read_message(left)), request, raw
    )
    thread.join(timeout=2)
    assert response.digest == hashlib.sha256(LOCK).hexdigest()
    assert response.tuf_snapshot_version == 4


def test_workload_signer_rejects_wrong_action_and_peer() -> None:
    left, right = _connection_pair()
    handler = WorkloadSignerConnectionHandler(FakePublisher(), allowed_peer_uid=os.geteuid())
    request = {
        "schema_version": 1,
        "intent_id": str(uuid.uuid4()),
        "action": "workload.invalid",
        "lock": base64.b64encode(LOCK).decode("ascii"),
        "git_commit": COMMIT,
        "evidence": EVIDENCE,
    }
    left.sendall(_canonical(request))
    left.shutdown(socket.SHUT_WR)
    with pytest.raises(WorkloadSignerProtocolError, match="identity"):
        handler.handle(right)
    left.close()
    right.close()


def test_policy_rejects_a_target_digest_mismatch() -> None:
    class Publisher:
        def publish(self, *_args: object) -> dict[str, object]:
            return {"digest": "d" * 64, "length": len(LOCK), "git_commit": COMMIT, "tuf_snapshot_version": 1}

    policy = WorkloadPublicationSignerPolicy(Publisher())
    with pytest.raises(WorkloadSignerProtocolError, match="digest"):
        policy.publish(LOCK, COMMIT, EVIDENCE)
