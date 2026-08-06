from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest
from dgx_agent_protocol import PackageReleaseLock
from dgx_control.package_rollouts import (
    PackageDesiredStateResolver,
    PackageRolloutError,
    package_operation_payload,
)

from spark_profiles.workload_packages import WorkloadDeployment

COMMIT = "a" * 40
NODE = "spk_" + "1" * 32


def _deployment(release: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment_id": "future-stack",
        "family_id": "synthetic-family",
        "release_digest": release,
        "selector": {
            "node_count": 1,
            "required_labels": {"pool": "default"},
            "preferred_node_ids": [NODE],
        },
        "secrets": {},
        "ports": {"http": 8000},
        "arguments": [],
        "routing": {"alias": "chat", "port": "http"},
        "resources": {"memory_bytes": 1, "storage_bytes": 1, "gpu_count": 1},
    }


def _lock() -> PackageReleaseLock:
    component = {
        "name": "payload",
        "kind": "artifact",
        "media_type": "application/octet-stream",
        "sources": [{"provider": "https", "url": "https://example.invalid/a"}],
        "digest": "sha256:" + "1" * 64,
        "size": 1,
        "unpacked_size": 1,
        "platforms": ["linux/arm64"],
        "materialization": {"method": "file"},
        "evidence": [],
    }
    return PackageReleaseLock.parse(
        {
            "schema_version": 1,
            "family_id": "synthetic-family",
            "upstream_version": "1",
            "upstream_identity": {
                "provider": "git",
                "repository": "https://example.invalid/repo",
                "commit": "b" * 40,
            },
            "components": [component],
            "dependency_digests": [],
            "adapter": component
            | {
                "name": "adapter",
                "kind": "adapter",
                "materialization": {"method": "executable"},
            },
            "adapter_abi": 1,
            "compatibility": {
                "architectures": ["arm64"],
                "operating_systems": ["linux"],
                "required_capabilities": [],
                "minimum_storage_bytes": 1,
            },
            "validation": [],
            "provenance": [],
            "resolver": {"name": "resolver", "version": 1},
        }
    )


@dataclass
class _Document:
    parsed: object
    content: bytes
    sha256: str


class _Repository:
    def __init__(self, deployment: dict[str, object], lock: PackageReleaseLock):
        self.deployment = deployment
        self.lock = lock

    def read_document(self, commit: str, path: str) -> _Document:
        if path.startswith("config/workload-deployments/"):
            raw = json.dumps(self.deployment, sort_keys=True).encode()
        else:
            raw = self.lock.canonical_bytes
        parsed = json.loads(raw)
        return _Document(parsed, raw, hashlib.sha256(raw).hexdigest())


def test_package_payload_is_exact_digest_bound_protocol_message() -> None:
    release = "a" * 64
    deployment = WorkloadDeployment.load(_deployment(release))
    payload = package_operation_payload(deployment, "package.prepare")
    assert set(payload) == {
        "schema_version",
        "deployment_id",
        "release_digest",
        "deployment_digest",
    }
    assert (
        payload["deployment_digest"]
        == hashlib.sha256(deployment.canonical_bytes).hexdigest()
    )


def test_unknown_family_resolves_without_static_adapter_catalog() -> None:
    lock = _lock()
    deployment = _deployment(lock.digest)
    resolver = PackageDesiredStateResolver(
        _Repository(deployment, lock),
        trust=lambda digest, raw, commit: (
            digest == lock.digest and raw == lock.canonical_bytes
        ),
    )
    plan = resolver.resolve(
        COMMIT,
        ("future-stack",),
        ({"node_id": NODE, "healthy": True, "labels": {"pool": "default"}},),
    )
    kinds = {node.kind for node in plan.operation_graph.nodes}  # type: ignore[union-attr]
    assert {"package.prepare", "package.activate", "package.health"} <= kinds
    assert "agent.update" not in kinds
    assert all(
        payload["release_digest"] == lock.digest
        for payload in plan.operation_payloads.values()
    )


def test_unsigned_release_is_rejected_before_graph_creation() -> None:
    lock = _lock()
    resolver = PackageDesiredStateResolver(
        _Repository(_deployment(lock.digest), lock), trust=lambda *_: False
    )
    with pytest.raises(PackageRolloutError, match="TUF-authorized"):
        resolver.resolve(
            COMMIT,
            ("future-stack",),
            ({"node_id": NODE, "healthy": True, "labels": {"pool": "default"}},),
        )
