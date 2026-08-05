from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dgx_control.presence import ManagementAddressPolicy
from dgx_control.route_runtime import (
    AcceptedEndpointEvidence,
    AtomicRouteBundlePublisher,
    RouteBundleRequest,
    RouteRuntimeError,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
NODE = "spk_" + "1" * 32
RECONCILIATION_ID = "bb7aac18-edbf-4cc1-bafd-15e282557c53"
PLAN_DIGEST = "a" * 64
EVIDENCE_SET_DIGEST = "b" * 64


def _routes() -> dict[str, object]:
    return {
        "chat": {
            "workload_id": "model",
            "nodes": [NODE],
            "entrypoint_node_id": NODE,
            "scheme": "http",
            "port": 8000,
            "path": "/v1",
            "quota": {
                "requests_per_minute": 30,
                "tokens_per_minute": 10_000,
            },
            "quota_digest": hashlib.sha256(
                b'{"requests_per_minute":30,"tokens_per_minute":10000}\n'
            ).hexdigest(),
        }
    }


def _request(*, address: str = "10.0.0.42") -> RouteBundleRequest:
    return RouteBundleRequest(
        reconciliation_id=RECONCILIATION_ID,
        plan_digest=PLAN_DIGEST,
        evidence_set_digest=EVIDENCE_SET_DIGEST,
        routes=_routes(),
        endpoints={
            NODE: AcceptedEndpointEvidence(
                node_id=NODE,
                address=address,
                observed_at=NOW,
                operation_id=f"model:{NODE}:workload.verify",
                evidence_digest="c" * 64,
            )
        },
        expires_at=NOW + timedelta(seconds=150),
    )


def _publisher(tmp_path: Path, **kwargs) -> AtomicRouteBundlePublisher:
    return AtomicRouteBundlePublisher(
        tmp_path / "runtime",
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
        **kwargs,
    )


def test_bundle_stages_structured_routes_litellm_and_manifest_before_one_marker(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)

    marker = publisher.publish(_request())

    assert marker.reconciliation_id == RECONCILIATION_ID
    assert marker.plan_digest == PLAN_DIGEST
    assert marker.evidence_set_digest == EVIDENCE_SET_DIGEST
    directory = tmp_path / "runtime/generations" / marker.directory
    routes = json.loads((directory / "routes.json").read_bytes())
    config = json.loads((directory / "litellm.json").read_bytes())
    manifest = json.loads((directory / "manifest.json").read_bytes())
    activation = json.loads((tmp_path / "runtime/activation.json").read_bytes())

    assert routes == {
        "generation": 1,
        "routes": {
            "chat": {
                "address": "10.0.0.42",
                "evidence_digest": "c" * 64,
                "node_id": NODE,
                "observed_at": NOW.isoformat(),
                "operation_id": f"model:{NODE}:workload.verify",
                "path": "/v1",
                "port": 8000,
                "scheme": "http",
            }
        },
        "schema_version": 1,
        "state": "published",
    }
    assert config["model_list"][0]["litellm_params"] == {
        "api_base": "http://10.0.0.42:8000/v1",
        "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
        "model": "openai/chat",
        "rpm": 30,
        "tpm": 10_000,
    }
    assert activation == {
        **manifest,
        "directory": marker.directory,
        "manifest_sha256": marker.manifest_sha256,
    }
    assert (
        activation["routes_sha256"]
        == hashlib.sha256((directory / "routes.json").read_bytes()).hexdigest()
    )
    assert (
        activation["litellm_sha256"]
        == hashlib.sha256((directory / "litellm.json").read_bytes()).hexdigest()
    )
    assert publisher.inspect(expected=marker) == marker


def test_routes_are_derived_only_from_exact_entrypoint_and_bounded_address_evidence(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    bad_routes = _routes()
    bad_routes["chat"]["entrypoint_node_id"] = "spk_" + "2" * 32

    with pytest.raises(RouteRuntimeError, match="entrypoint"):
        publisher.publish(
            _request().__class__(**{**_request().__dict__, "routes": bad_routes})
        )
    with pytest.raises(RouteRuntimeError, match="management"):
        publisher.publish(_request(address="192.0.2.9"))
    assert not (tmp_path / "runtime/activation.json").exists()


def test_explicit_entrypoint_is_authoritative_even_when_nodes_have_another_order(
    tmp_path: Path,
) -> None:
    request = _request()
    routes = _routes()
    routes["chat"]["nodes"] = ["spk_" + "2" * 32, NODE]

    marker = _publisher(tmp_path).publish(
        request.__class__(**{**request.__dict__, "routes": routes})
    )

    assert marker.state == "published"


def test_concurrent_same_publication_is_one_idempotent_generation(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    barrier = threading.Barrier(2)
    markers = []

    def publish() -> None:
        barrier.wait(timeout=5)
        markers.append(publisher.publish(_request()))

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(markers) == 2
    assert markers[0] == markers[1]
    assert markers[0].generation == 1
    assert len(list((tmp_path / "runtime/generations").iterdir())) == 1


def test_validation_or_activation_failure_retains_previous_exact_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path)
    previous = publisher.publish(_request())
    active = tmp_path / "runtime/activation.json"
    previous_bytes = active.read_bytes()
    replacement = _request().__class__(
        **{**_request().__dict__, "evidence_set_digest": "d" * 64}
    )

    rejecting = _publisher(tmp_path, validate_litellm=lambda _content: False)
    with pytest.raises(RouteRuntimeError, match="LiteLLM validation"):
        rejecting.publish(replacement)
    assert active.read_bytes() == previous_bytes

    original_replace = __import__("os").replace

    def fail_marker(source, target):
        if Path(target) == active:
            raise OSError("crash before activation")
        original_replace(source, target)

    monkeypatch.setattr("dgx_control.route_runtime.os.replace", fail_marker)
    with pytest.raises(RouteRuntimeError, match="activation"):
        publisher.publish(replacement)
    assert active.read_bytes() == previous_bytes
    monkeypatch.undo()
    assert (
        AtomicRouteBundlePublisher(
            tmp_path / "runtime",
            management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
            clock=lambda: NOW,
        ).inspect(expected=previous)
        == previous
    )


def test_restart_inspection_rejects_tampering_wrong_expectation_and_expired_lease(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    marker = publisher.publish(_request())
    restarted = AtomicRouteBundlePublisher(
        tmp_path / "runtime",
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    )
    wrong = marker.__class__(**{**marker.__dict__, "plan_digest": "d" * 64})
    with pytest.raises(RouteRuntimeError, match="expected"):
        restarted.inspect(expected=wrong)

    config = tmp_path / "runtime/generations" / marker.directory / "litellm.json"
    config.write_bytes(b'{"model_list":[{"unsafe":true}]}\n')
    with pytest.raises(RouteRuntimeError, match="checksum"):
        restarted.inspect(expected=marker)

    clean_root = tmp_path / "clean"
    clean = AtomicRouteBundlePublisher(
        clean_root,
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    )
    clean_marker = clean.publish(_request())
    expired = AtomicRouteBundlePublisher(
        clean_root,
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW + timedelta(seconds=151),
    )
    with pytest.raises(RouteRuntimeError, match="lease"):
        expired.inspect(expected=clean_marker)


def test_withdrawal_activates_an_empty_fail_closed_bundle(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())

    marker = publisher.withdraw(
        reconciliation_id=RECONCILIATION_ID,
        plan_digest=PLAN_DIGEST,
        targets=(NODE,),
        reason="reconciliation in progress bearer-secret",
    )

    directory = tmp_path / "runtime/generations" / marker.directory
    routes = json.loads((directory / "routes.json").read_bytes())
    config = json.loads((directory / "litellm.json").read_bytes())
    assert marker.state == "maintenance"
    assert routes["state"] == "maintenance"
    assert routes["routes"] == {}
    assert routes["targets"] == [NODE]
    assert "bearer-secret" not in routes["reason"]
    assert config["model_list"] == []
