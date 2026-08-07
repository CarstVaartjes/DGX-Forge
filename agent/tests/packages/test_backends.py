from __future__ import annotations

from dataclasses import replace

import pytest
from vonk_agent.packages.backends import (
    Backend,
    BackendInvocation,
    BackendValidationError,
    MountPolicy,
    NetworkPolicy,
    ResourcePolicy,
)
from vonk_agent_protocol import OciBundleMetadata

RELEASE = "a" * 64
OBJECT = "b" * 64
INTERPRETER = "c" * 64


def python_runtime_document() -> dict[str, object]:
    return {
        "environment_component": "python-environment",
        "environment_digest": OBJECT,
        "environment_tree_digest": OBJECT,
        "interpreter_component": "python-interpreter",
        "interpreter_component_digest": INTERPRETER,
        "interpreter_entrypoint": "bin/python3",
        "interpreter_digest": INTERPRETER,
    }


def invocation_document(backend: str = "native") -> dict[str, object]:
    document = {
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
    if backend == "python-venv":
        document["python_runtime"] = python_runtime_document()
    if backend == "oci":
        document["oci_bundle"] = OciBundleMetadata(
            1,
            "runtime",
            "sha256:" + "d" * 64,
            "sha256:" + "e" * 64,
            "sha256:" + "f" * 64,
            "linux-arm64",
            "runc",
            "rootfs",
            "bin/server",
        ).to_mapping()
        document["oci_bundle_digest"] = OBJECT
    return document


@pytest.mark.parametrize("backend", ["oci", "python-venv", "native"])
def test_backend_invocation_accepts_only_the_compiled_backend_vocabulary(
    backend: str,
) -> None:
    invocation = BackendInvocation.parse(invocation_document(backend))

    assert invocation.backend is Backend(backend)
    assert invocation.entrypoint == "bin/future-adapter"
    assert invocation.mounts == (MountPolicy(OBJECT, "models/primary", True),)
    assert invocation.network == NetworkPolicy("restricted", ("models.example:443",))


def test_python_venv_requires_signed_interpreter_runtime_metadata() -> None:
    document = invocation_document("python-venv")
    invocation = BackendInvocation.parse(document)

    assert invocation.python_runtime is not None
    assert invocation.python_runtime.interpreter_digest == INTERPRETER
    assert invocation.python_runtime.interpreter_entrypoint == "bin/python3"

    missing = invocation_document("python-venv")
    del missing["python_runtime"]
    with pytest.raises(BackendValidationError, match="(?i)runtime|python"):
        BackendInvocation.parse(missing)


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment_component", "../environment"),
        ("interpreter_component", "python/../../runtime"),
        ("interpreter_entrypoint", "/usr/bin/python3"),
        ("interpreter_digest", "not-a-digest"),
    ],
)
def test_python_runtime_metadata_rejects_ambient_or_untrusted_interpreter(
    field: str, value: str
) -> None:
    document = invocation_document("python-venv")
    runtime = dict(document["python_runtime"])
    runtime[field] = value
    document["python_runtime"] = runtime

    with pytest.raises(BackendValidationError, match="(?i)runtime|python"):
        BackendInvocation.parse(document)


def test_non_python_backend_cannot_smuggle_python_runtime_metadata() -> None:
    document = invocation_document("native")
    document["python_runtime"] = python_runtime_document()

    with pytest.raises(BackendValidationError, match="(?i)runtime|python"):
        BackendInvocation.parse(document)


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
        "python_runtime",
    }
