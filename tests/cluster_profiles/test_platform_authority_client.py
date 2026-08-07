from __future__ import annotations

import hashlib
import json

import pytest

from cluster_profiles.platform_authority_client import (
    AuthorityError,
    DelegatedPlatformAuthority,
    HttpResponse,
)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class Transport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> HttpResponse:
        self.requests.append((method, url, headers, body))
        return self.responses.pop(0)


def _authority(transport: Transport) -> DelegatedPlatformAuthority:
    return DelegatedPlatformAuthority(
        "https://authority.example.invalid/api",
        token=lambda: "oidc-token",
        transport=transport,
    )


def test_target_publication_uses_oidc_idempotency_and_exact_request() -> None:
    target_name = f"platform/releases/1.2.0/{'a' * 64}.json"
    manifest = b"manifest"
    target_sha256 = hashlib.sha256(manifest).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    receipt = {
        "retained_targets": [f"platform/releases/1.1.0/{'b' * 64}.json"],
        "target_name": target_name,
        "target_sha256": target_sha256,
        "targets_version": 19,
    }
    transport = Transport([HttpResponse(201, {}, _canonical(receipt))])

    result = _authority(transport).publish_target(
        target_name, target_sha256, manifest, receipt["retained_targets"]
    )

    assert result == receipt
    method, url, headers, body = transport.requests[0]
    assert method == "PUT"
    assert url.endswith("/v1/platform/targets/platform%2Freleases%2F1.2.0%2F" + target_sha256 + ".json")
    assert headers["Authorization"] == "Bearer oidc-token"
    assert headers["Idempotency-Key"] == target_sha256
    assert headers["If-None-Match"] == "*"
    assert json.loads(body or b"") == {
        "manifest_base64": __import__("base64").b64encode(manifest).decode(),
        "retained_targets": receipt["retained_targets"],
        "schema_version": 1,
        "target_name": target_name,
        "target_sha256": target_sha256,
    }


def test_target_conflict_accepts_only_exact_idempotent_receipt() -> None:
    manifest = b"manifest"
    target_sha256 = hashlib.sha256(manifest).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    receipt = {
        "retained_targets": [],
        "target_name": target_name,
        "target_sha256": target_sha256,
        "targets_version": 19,
    }
    transport = Transport(
        [
            HttpResponse(412, {}, b""),
            HttpResponse(200, {}, _canonical(receipt)),
        ]
    )

    assert _authority(transport).publish_target(
        target_name, target_sha256, manifest, []
    ) == receipt
    assert [request[0] for request in transport.requests] == ["PUT", "GET"]

    wrong = receipt | {"target_sha256": "b" * 64}
    rejecting = Transport(
        [HttpResponse(409, {}, b""), HttpResponse(200, {}, _canonical(wrong))]
    )
    with pytest.raises(AuthorityError, match="receipt"):
        _authority(rejecting).publish_target(
            target_name, target_sha256, manifest, []
        )


def test_channel_exact_replay_is_idempotent_without_put() -> None:
    document = _canonical(
        {
            "channel": "stable",
            "discovery_only": True,
            "schema_version": 1,
            "target_name": f"platform/releases/1.2.0/{'a' * 64}.json",
            "target_sha256": "a" * 64,
            "tuf_targets_version": 19,
        }
    )
    transport = Transport([HttpResponse(200, {"ETag": '"channel-19"'}, document)])

    receipt = _authority(transport).publish_channel("stable", document)

    assert receipt["document_sha256"] == hashlib.sha256(document).hexdigest()
    assert [request[0] for request in transport.requests] == ["GET"]


def test_channel_cas_advances_only_to_a_higher_targets_version() -> None:
    current = {
        "channel": "stable",
        "discovery_only": True,
        "schema_version": 1,
        "target_name": f"platform/releases/1.2.0/{'a' * 64}.json",
        "target_sha256": "a" * 64,
        "tuf_targets_version": 19,
    }
    next_document = current | {
        "target_name": f"platform/releases/1.3.0/{'b' * 64}.json",
        "target_sha256": "b" * 64,
        "tuf_targets_version": 20,
    }
    next_raw = _canonical(next_document)
    receipt = {
        "channel": "stable",
        "document_sha256": hashlib.sha256(next_raw).hexdigest(),
        "target_name": next_document["target_name"],
        "target_sha256": "b" * 64,
    }
    transport = Transport(
        [
            HttpResponse(200, {"ETag": '"channel-19"'}, _canonical(current)),
            HttpResponse(200, {}, _canonical(receipt)),
        ]
    )

    assert _authority(transport).publish_channel("stable", next_raw) == receipt
    method, _, headers, body = transport.requests[1]
    assert method == "PUT"
    assert headers["If-Match"] == '"channel-19"'
    assert body == next_raw

    equal_other = current | {"target_sha256": "c" * 64}
    rejecting = Transport(
        [HttpResponse(200, {"ETag": '"channel-19"'}, _canonical(current))]
    )
    with pytest.raises(AuthorityError, match="advance"):
        _authority(rejecting).publish_channel("stable", _canonical(equal_other))


def test_channel_create_uses_if_none_match_cas() -> None:
    document = {
        "channel": "stable",
        "discovery_only": True,
        "schema_version": 1,
        "target_name": f"platform/releases/1.2.0/{'a' * 64}.json",
        "target_sha256": "a" * 64,
        "tuf_targets_version": 1,
    }
    raw = _canonical(document)
    receipt = {
        "channel": "stable",
        "document_sha256": hashlib.sha256(raw).hexdigest(),
        "target_name": document["target_name"],
        "target_sha256": document["target_sha256"],
    }
    transport = Transport(
        [HttpResponse(404, {}, b""), HttpResponse(201, {}, _canonical(receipt))]
    )

    _authority(transport).publish_channel("stable", raw)

    assert transport.requests[1][2]["If-None-Match"] == "*"
