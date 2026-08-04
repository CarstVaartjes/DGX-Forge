from dataclasses import dataclass

from dgx_control.api import create_app
from dgx_control.audit import MemoryAuditStore
from dgx_control.auth import Actor, TokenCodec
from fastapi.testclient import TestClient


@dataclass
class Enqueued:
    id: str = "job-1"
    state: str = "queued"


class Jobs:
    def __init__(self) -> None:
        self.calls = []

    def enqueue(self, kind, actor, base_commit, targets, payload, *, request_id):
        self.calls.append((kind, actor, base_commit, targets, payload, request_id))
        return Enqueued()

    def get(self, job_id):
        return Enqueued(id=job_id)


def _client(role: str):
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    jobs = Jobs()
    app = create_app(jobs=jobs, tokens=codec, audits=audits, fleet=lambda: {"nodes": []}, now=lambda: 10)
    client = TestClient(app)
    token = codec.issue(Actor(role, role), ttl_seconds=1000, now=0)
    return client, {"Authorization": f"Bearer {token}"}, jobs, audits


def test_health_is_public_but_fleet_requires_authentication() -> None:
    client, _, _, _ = _client("viewer")
    assert client.get("/api/v1/healthz").status_code == 200
    assert client.get("/api/v1/fleet").status_code == 401


def test_viewer_cannot_enqueue_mutation() -> None:
    client, headers, _, _ = _client("viewer")
    response = client.post("/api/v1/jobs", headers=headers, json={
        "kind": "probe", "base_commit": "abc", "targets": ["node"], "payload": {}
    })
    assert response.status_code == 403


def test_admin_mutation_is_correlated_and_audited() -> None:
    client, headers, jobs, audits = _client("administrator")
    response = client.post("/api/v1/jobs", headers=headers, json={
        "kind": "probe", "base_commit": "abc", "targets": ["node"], "payload": {"safe": True}
    })
    assert response.status_code == 202
    request_id = response.headers["x-request-id"]
    assert jobs.calls[0][1:4] == ("administrator", "abc", ["node"])
    event = audits.for_request(request_id)
    assert (event.actor, event.base_commit, event.targets) == ("administrator", "abc", ("node",))


def test_cookie_authenticated_mutation_requires_matching_csrf() -> None:
    client, headers, _, _ = _client("operator")
    token = headers["Authorization"].removeprefix("Bearer ")
    client.cookies.set("dgx_session", token)
    assert client.post("/api/v1/jobs", json={"kind": "probe", "base_commit": "abc", "targets": [], "payload": {}}).status_code == 403
    client.cookies.set("dgx_csrf", "nonce")
    assert client.post("/api/v1/jobs", headers={"x-csrf-token": "nonce"}, json={"kind": "probe", "base_commit": "abc", "targets": [], "payload": {}}).status_code == 202
