from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from dgx_control.litellm import LiteLlmDeployment
from dgx_control.route_runtime import PublishedRoute
from dgx_control.worker_authority import (
    HttpWorkerAuthority,
    RepositoryAuthorityService,
    WorkerAuthorityError,
    install_worker_authority_routes,
    worker_document_signature,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

COMMIT = "a" * 40
ROUTE = PublishedRoute(
    alias="chat",
    workload_id="model-a",
    api_base="http://10.0.0.10:8000/v1",
    requests_per_minute=10,
    tokens_per_minute=20,
)


def _client(*, eligible: bool = True) -> TestClient:
    service = RepositoryAuthorityService(
        current_commit=lambda: COMMIT,
        commit_eligible=lambda value: eligible and value == COMMIT,
        deployments=lambda commit, routes: (
            LiteLlmDeployment(
                model_name="hermes-agent",
                workload="model-a",
                api_base=routes[0].api_base,
                priority=1,
                requests_per_minute=10,
                tokens_per_minute=20,
            ),
        ),
        clock=lambda: 100,
    )
    app = FastAPI()
    install_worker_authority_routes(app, service, token=b"w" * 32)
    return TestClient(app)


def test_internal_worker_authority_requires_exact_service_token() -> None:
    client = _client()
    body = {
        "schema_version": 1,
        "commit": COMMIT,
        "nonce": "0" * 32,
        "routes": [],
    }

    assert client.post(
        "/internal/v1/repository/evaluate", json=body
    ).status_code == 401
    assert client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-dgx-worker-signature": worker_document_signature(
                b"x" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    ).status_code == 401
    response = client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-dgx-worker-signature": worker_document_signature(
                b"w" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["commit"] == COMMIT
    assert response.json()["nonce"] == "0" * 32
    assert response.json()["expires_at"] == 115


def test_internal_worker_authority_returns_commit_bound_hermes_deployments() -> None:
    client = _client()
    body = {
        "schema_version": 1,
        "commit": COMMIT,
        "nonce": "1" * 32,
        "routes": [
            {
                "alias": ROUTE.alias,
                "workload_id": ROUTE.workload_id,
                "api_base": ROUTE.api_base,
                "requests_per_minute": ROUTE.requests_per_minute,
                "tokens_per_minute": ROUTE.tokens_per_minute,
            }
        ],
    }
    response = client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-dgx-worker-signature": worker_document_signature(
                b"w" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["commit"] == COMMIT
    assert response.json()["nonce"] == "1" * 32
    assert response.json()["current"] is True
    assert response.json()["eligible"] is True
    assert response.json()["deployments"] == [
        {
            "model_name": "hermes-agent",
            "workload": "model-a",
            "api_base": ROUTE.api_base,
            "priority": 1,
            "requests_per_minute": 10,
            "tokens_per_minute": 20,
        }
    ]


def test_internal_worker_authority_fails_closed_before_repository_policy_output() -> None:
    client = _client(eligible=False)
    body = {
        "schema_version": 1,
        "commit": COMMIT,
        "nonce": "2" * 32,
        "routes": [],
    }
    response = client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-dgx-worker-signature": worker_document_signature(
                b"w" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["eligible"] is False
    assert response.json()["deployments"] == []


def test_worker_consumes_one_nonce_bound_evaluation_for_eligibility_and_head() -> None:
    calls: list[str] = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

    def open_request(request, *, timeout):
        assert timeout == 3
        body = json.loads(request.data)
        calls.append(request.full_url)
        response = {
            "schema_version": 1,
            "commit": COMMIT,
            "nonce": body["nonce"],
            "current": True,
            "eligible": True,
            "routes_sha256": hashlib.sha256(b"[]").hexdigest(),
            "deployments": [],
            "issued_at": 100,
            "expires_at": 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32,
            response,
            purpose="response",
        )
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        b"w" * 32,
        opener=open_request,
        clock=lambda: 100,
    )

    assert authority.eligible(COMMIT) is True
    assert authority.current_commit() == COMMIT
    assert calls == ["http://control-api:8000/internal/v1/repository/evaluate"]


def test_repository_head_change_during_policy_evaluation_fails_closed() -> None:
    heads = iter((COMMIT, "b" * 40))
    service = RepositoryAuthorityService(
        current_commit=lambda: next(heads),
        commit_eligible=lambda _commit: True,
        deployments=lambda _commit, _routes: (_ for _ in ()).throw(
            AssertionError("deployments must not be selected after a head change")
        ),
        clock=lambda: 100,
    )

    result = service.evaluate(COMMIT, (ROUTE,))

    assert result["current"] is False
    assert result["eligible"] is False
    assert result["deployments"] == []


@pytest.mark.parametrize("fault", ("signature", "nonce", "expired", "redirect", "oversized"))
def test_worker_rejects_tampered_stale_redirected_or_oversized_authority(
    fault: str,
) -> None:
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

        def geturl(self) -> str:
            if fault == "redirect":
                return "http://attacker.invalid/authority"
            return "http://control-api:8000/internal/v1/repository/evaluate"

    def open_request(request, *, timeout):
        assert timeout == 3
        if fault == "oversized":
            return Response(b"x" * 65_537)
        body = json.loads(request.data)
        response = {
            "schema_version": 1,
            "commit": COMMIT,
            "nonce": "f" * 32 if fault == "nonce" else body["nonce"],
            "current": True,
            "eligible": True,
            "routes_sha256": hashlib.sha256(b"[]").hexdigest(),
            "deployments": [],
            "issued_at": 80 if fault == "expired" else 100,
            "expires_at": 95 if fault == "expired" else 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32,
            response,
            purpose="response",
        )
        if fault == "signature":
            response["signature"] = "0" * 64
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        b"w" * 32,
        opener=open_request,
        clock=lambda: 100,
    )

    with pytest.raises(WorkerAuthorityError):
        authority.eligible(COMMIT)


def test_worker_http_client_disables_environment_proxies() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src/dgx_control/worker_authority.py"
    ).read_text()

    assert "ProxyHandler({})" in source


def test_external_caddy_listener_denies_internal_worker_routes_before_fallback() -> None:
    root = Path(__file__).resolve().parents[2]
    caddy = (root / "deploy/compose/Caddyfile").read_text()
    tailnet = caddy.split(":8080 {", 1)[1].split(
        "# Bootstrap is server-authenticated", 1
    )[0]

    assert "path /internal/*" in tailnet
    assert tailnet.index("path /internal/*") < tailnet.index("import control_proxy")
