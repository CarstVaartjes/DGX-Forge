from __future__ import annotations

from dataclasses import replace

import pytest
from dgx_agent.packages.backends import (
    Backend,
    BackendInvocation,
    BackendValidationError,
    MountPolicy,
    NetworkPolicy,
    ResourcePolicy,
)

RELEASE = "a" * 64
OBJECT = "b" * 64


def invocation_document(backend: str = "native") -> dict[str, object]:
    return {
        "schema_version": 1,
        "backend": backend,
        "release_digest": RELEASE,
        "generation": "gen-20260806-a",
        "entrypoint": "bin/future-adapter",
        "arguments": ["serve", "--port", "8080"],
        "resources": {
            "cpu_millis": 2000,
            "memory_bytes": 8 * 1024**3,
            "pids_limit": 128,
            "timeout_seconds": 60,
            "output_limit_bytes": 64 * 1024,
        },
        "mounts": [
            {
                "object_digest": OBJECT,
                "target": "models/primary",
                "read_only": True,
            }
        ],
        "devices": ["nvidia0"],
        "network": {"mode": "restricted", "egress": ["models.example:443"]},
    }


@pytest.mark.parametrize("backend", ["oci", "python-venv", "native"])
def test_backend_invocation_accepts_only_the_compiled_backend_vocabulary(
    backend: str,
) -> None:
    invocation = BackendInvocation.parse(invocation_document(backend))

    assert invocation.backend is Backend(backend)
    assert invocation.entrypoint == "bin/future-adapter"
    assert invocation.mounts == (MountPolicy(OBJECT, "models/primary", True),)
    assert invocation.network == NetworkPolicy("restricted", ("models.example:443",))


@pytest.mark.parametrize("backend", ["shell", "apt", "cuda-installer", "NATIVE"])
def test_backend_invocation_rejects_uncompiled_backends(backend: str) -> None:
    with pytest.raises(BackendValidationError, match="backend"):
        BackendInvocation.parse(invocation_document(backend))


@pytest.mark.parametrize(
    "entrypoint",
    ["/usr/bin/bash", "../bin/tool", "bin/../tool", "bin//tool", "apt", "bin/sh"],
)
def test_backend_invocation_rejects_host_or_shell_entrypoints(entrypoint: str) -> None:
    document = invocation_document()
    document["entrypoint"] = entrypoint

    with pytest.raises(BackendValidationError, match="entrypoint"):
        BackendInvocation.parse(document)


@pytest.mark.parametrize("field", ["environment", "command", "host_path", "module"])
def test_backend_invocation_rejects_privilege_shaped_fields(field: str) -> None:
    document = invocation_document()
    document[field] = "unsafe"

    with pytest.raises(BackendValidationError, match="fields"):
        BackendInvocation.parse(document)


def test_backend_invocation_rejects_unbounded_arguments_and_writable_mounts() -> None:
    arguments = invocation_document()
    arguments["arguments"] = ["x"] * 33
    writable = invocation_document()
    writable["mounts"] = [
        {"object_digest": OBJECT, "target": "model", "read_only": False}
    ]

    with pytest.raises(BackendValidationError, match="arguments"):
        BackendInvocation.parse(arguments)
    with pytest.raises(BackendValidationError, match="read-only"):
        BackendInvocation.parse(writable)


def test_resource_policy_rejects_unbounded_or_boolean_values() -> None:
    valid = ResourcePolicy(1000, 1024**3, 32, 30, 4096)

    with pytest.raises(BackendValidationError, match="CPU"):
        replace(valid, cpu_millis=True)
    with pytest.raises(BackendValidationError, match="memory"):
        replace(valid, memory_bytes=2**61)
    with pytest.raises(BackendValidationError, match="timeout"):
        replace(valid, timeout_seconds=0)


def test_backend_mapping_round_trips_without_names_or_ambient_configuration() -> None:
    invocation = BackendInvocation.parse(invocation_document("python-venv"))

    assert BackendInvocation.parse(invocation.to_mapping()) == invocation
    assert set(invocation.to_mapping()) == {
        "schema_version",
        "backend",
        "release_digest",
        "generation",
        "entrypoint",
        "arguments",
        "resources",
        "mounts",
        "devices",
        "network",
    }
