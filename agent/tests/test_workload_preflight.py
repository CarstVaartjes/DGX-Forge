from __future__ import annotations

from types import SimpleNamespace
import hashlib
from dgx_agent_protocol import AgentOperation, PackageOperationRequest, canonical_message

import pytest
from dgx_agent import main


def _lock(*operating_systems: str) -> SimpleNamespace:
    return SimpleNamespace(
        compatibility={
            "architectures": ("arm64",),
            "operating_systems": operating_systems,
            "minimum_storage_bytes": 1,
            "required_capabilities": ("package-abi-v1",),
            "backends": ("native",),
        },
        adapter_abi=1,
    )


def test_workload_preflight_accepts_concrete_linux_distribution_identity() -> None:
    main._validate_workload_compatibility(
        _lock("ubuntu-24.04"),
        "linux-arm64",
        available_storage_bytes=1,
        operating_systems=("ubuntu", "ubuntu-24.04", "linux"),
    )


def test_workload_preflight_rejects_distribution_not_present_on_node() -> None:
    with pytest.raises(ValueError, match="operating system"):
        main._validate_workload_compatibility(
            _lock("ubuntu-24.04"),
            "linux-arm64",
            available_storage_bytes=1,
            operating_systems=("linux",),
        )


def test_backend_invocation_uses_signed_deployment_policy_without_catalog_defaults() -> None:
    deployment = {
        "schema_version": 1,
        "deployment_id": "future-stack",
        "family_id": "future-family",
        "release_digest": "a" * 64,
        "selector": {"node_count": 1, "required_labels": {}, "preferred_node_ids": []},
        "secrets": {},
        "ports": {"http": 8080},
        "arguments": ["serve", "--port", "8080"],
        "routing": {"alias": "future", "port": "http"},
        "resources": {"memory_bytes": 4096, "storage_bytes": 8192, "gpu_count": 1},
    }
    digest = hashlib.sha256(canonical_message(deployment) + b"\n").hexdigest()
    request = PackageOperationRequest.parse(
        AgentOperation.PACKAGE_PREPARE,
        {
            "schema_version": 1,
            "deployment_id": "future-stack",
            "release_digest": "a" * 64,
            "deployment_digest": digest,
            "deployment": deployment,
            "deployment_config_digest": digest,
        },
    )
    lock = SimpleNamespace(
        adapter=SimpleNamespace(name="future-adapter"),
        adapter_abi=1,
        compatibility={"backends": ("native",)},
    )

    invocation = main._backend_invocation_for_workload(
        lock, request, release_digest="a" * 64, generation="gen-future"
    )

    assert invocation.arguments == ("serve", "--port", "8080")
    assert invocation.resources.memory_bytes == 4096
    assert invocation.entrypoint == "components/future-adapter/future-adapter"


def test_backend_invocation_rejects_missing_deployment_projection() -> None:
    lock = SimpleNamespace(
        adapter=SimpleNamespace(name="future-adapter"),
        adapter_abi=1,
        compatibility={"backends": ("native",)},
    )
    with pytest.raises(RuntimeError, match="deployment projection"):
        main._backend_invocation_for_workload(
            lock, None, release_digest="a" * 64, generation="gen-future"
        )
