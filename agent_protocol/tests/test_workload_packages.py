from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib.resources import files
from types import MappingProxyType

import pytest
from dgx_agent_protocol import AgentProtocolError
from dgx_agent_protocol.workload_packages import (
    ComponentDescriptor,
    PackageReleaseGraph,
    PackageReleaseLock,
)
from jsonschema import Draft202012Validator

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def component(
    name: str = "payload",
    *,
    source: dict[str, object] | None = None,
    size: int = 1024,
) -> dict[str, object]:
    return {
        "name": name,
        "kind": "artifact",
        "media_type": "application/octet-stream",
        "sources": [
            source
            or {
                "provider": "https",
                "url": f"https://packages.example.invalid/{name}.bin",
            }
        ],
        "digest": "sha256:" + SHA_A,
        "size": size,
        "unpacked_size": size,
        "platforms": ["linux/arm64"],
        "materialization": {"method": "file"},
        "evidence": [{"kind": "checksum", "digest": "sha256:" + SHA_B}],
    }


def lock_document(
    family_id: str = "future-synthetic-stack",
    *,
    dependencies: list[str] | None = None,
    components: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "family_id": family_id,
        "upstream_version": "2026.08-test",
        "upstream_identity": {
            "provider": "git",
            "repository": "https://git.example.invalid/future/synthetic.git",
            "commit": "1" * 40,
        },
        "components": components if components is not None else [component()],
        "dependency_digests": dependencies or [],
        "adapter": component("adapter")
        | {
            "kind": "adapter",
            "media_type": "application/vnd.dgx-forge.workload-adapter.v1",
            "materialization": {"method": "executable"},
        },
        "adapter_abi": 1,
        "compatibility": {
            "architectures": ["arm64"],
            "operating_systems": ["linux"],
            "required_capabilities": ["package-abi-v1"],
            "minimum_storage_bytes": 2048,
        },
        "validation": [{"kind": "component-digest", "component": "payload"}],
        "provenance": [{"kind": "slsa", "digest": "sha256:" + SHA_C}],
        "resolver": {"name": "metadata-v1", "version": 1},
    }


def reversed_maps(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: reversed_maps(item) for key, item in reversed(tuple(value.items()))
        }
    if isinstance(value, list):
        return [reversed_maps(item) for item in value]
    return value


def test_release_lock_digest_is_stable_for_reordered_maps() -> None:
    original = PackageReleaseLock.parse(lock_document())
    reordered = PackageReleaseLock.parse(reversed_maps(lock_document()))

    assert reordered.canonical_bytes == original.canonical_bytes
    assert reordered.digest == original.digest
    assert hashlib.sha256(original.canonical_bytes).hexdigest() == original.digest


def test_release_lock_rejects_duplicate_json_keys() -> None:
    raw = json.dumps(lock_document(), separators=(",", ":"))
    duplicate = raw.replace(
        '"schema_version":1,',
        '"schema_version":1,"schema_version":1,',
        1,
    )

    with pytest.raises(AgentProtocolError, match="duplicate.*schema_version"):
        PackageReleaseLock.parse(duplicate.encode())


def test_release_lock_and_components_are_deeply_immutable() -> None:
    lock = PackageReleaseLock.parse(lock_document())

    assert isinstance(lock.upstream_identity, MappingProxyType)
    assert isinstance(lock.compatibility, MappingProxyType)
    assert isinstance(lock.components[0].materialization, MappingProxyType)
    with pytest.raises(TypeError):
        lock.compatibility["architectures"] = ("amd64",)  # type: ignore[index]


def test_component_descriptor_exposes_exact_contract_fields() -> None:
    descriptor = ComponentDescriptor.parse(component())

    assert tuple(descriptor.__dataclass_fields__) == (
        "name",
        "kind",
        "media_type",
        "sources",
        "digest",
        "size",
        "unpacked_size",
        "platforms",
        "materialization",
        "evidence",
    )


@pytest.mark.parametrize("missing", ("size", "unpacked_size"))
def test_component_requires_declared_size_fields(missing: str) -> None:
    document = component()
    del document[missing]

    with pytest.raises(AgentProtocolError, match=missing):
        ComponentDescriptor.parse(document)


@pytest.mark.parametrize("size", (0, -1, True, 2**63))
def test_component_rejects_invalid_or_unbounded_sizes(size: object) -> None:
    document = component()
    document["size"] = size

    with pytest.raises(AgentProtocolError, match="size"):
        ComponentDescriptor.parse(document)


def test_component_accepts_absent_unpacked_size_as_explicit_null() -> None:
    document = component()
    document["unpacked_size"] = None

    assert ComponentDescriptor.parse(document).unpacked_size is None


def test_component_rejects_floating_oci_tag() -> None:
    document = component(
        source={
            "provider": "oci",
            "reference": "registry.example.invalid/future/runtime:latest",
        }
    )

    with pytest.raises(AgentProtocolError, match="OCI.*digest"):
        ComponentDescriptor.parse(document)


@pytest.mark.parametrize("commit", ("1" * 7, "main", "1" * 39, "A" * 40))
def test_release_lock_rejects_noncanonical_git_commit(commit: str) -> None:
    document = lock_document()
    document["upstream_identity"] = {
        "provider": "git",
        "repository": "https://git.example.invalid/future/synthetic.git",
        "commit": commit,
    }

    with pytest.raises(AgentProtocolError, match="Git commit"):
        PackageReleaseLock.parse(document)


@pytest.mark.parametrize("revision", ("main", "latest", "refs/heads/main", "1" * 12))
def test_release_lock_rejects_mutable_hugging_face_revision(revision: str) -> None:
    document = lock_document()
    document["upstream_identity"] = {
        "provider": "huggingface",
        "repository": "future/synthetic-model",
        "revision": revision,
    }

    with pytest.raises(AgentProtocolError, match="Hugging Face revision"):
        PackageReleaseLock.parse(document)


@pytest.mark.parametrize(
    ("location", "field"),
    (
        ("top", "unknown"),
        ("component", "unknown"),
        ("source", "unknown"),
        ("materialization", "unknown"),
        ("compatibility", "unknown"),
        ("validation", "unknown"),
        ("provenance", "unknown"),
        ("resolver", "unknown"),
    ),
)
def test_release_lock_rejects_unknown_fields(location: str, field: str) -> None:
    document = lock_document()
    if location == "top":
        target = document
    elif location == "component":
        target = document["components"][0]  # type: ignore[index]
    elif location == "source":
        target = document["components"][0]["sources"][0]  # type: ignore[index]
    elif location == "materialization":
        target = document["components"][0]["materialization"]  # type: ignore[index]
    elif location == "compatibility":
        target = document["compatibility"]
    elif location == "validation":
        target = document["validation"][0]  # type: ignore[index]
    elif location == "provenance":
        target = document["provenance"][0]  # type: ignore[index]
    else:
        target = document["resolver"]
    target[field] = "unexpected"  # type: ignore[index]

    with pytest.raises(AgentProtocolError, match="unknown fields"):
        PackageReleaseLock.parse(document)


@pytest.mark.parametrize("unsafe", ("command", "shell", "host_path", "api_token"))
def test_release_lock_rejects_execution_path_and_secret_shaped_fields(
    unsafe: str,
) -> None:
    document = lock_document()
    document["components"][0]["materialization"][unsafe] = "unsafe"  # type: ignore[index]

    with pytest.raises(AgentProtocolError, match="unsafe|unknown fields"):
        PackageReleaseLock.parse(document)


def test_release_lock_rejects_duplicate_dependencies_and_components() -> None:
    duplicate_dependency = lock_document(dependencies=[SHA_A, SHA_A])
    duplicate_component = lock_document(components=[component(), component()])

    with pytest.raises(AgentProtocolError, match="duplicate dependency"):
        PackageReleaseLock.parse(duplicate_dependency)
    with pytest.raises(AgentProtocolError, match="duplicate component"):
        PackageReleaseLock.parse(duplicate_component)


def test_graph_resolves_root_before_dependencies_deterministically() -> None:
    shared = PackageReleaseLock.parse(lock_document("shared-model"))
    root = PackageReleaseLock.parse(
        lock_document("synthetic-root", dependencies=[shared.digest])
    )

    graph = PackageReleaseGraph.resolve(
        root.digest,
        {shared.digest: shared, root.digest: root},
    )

    assert tuple(item.family_id for item in graph.releases) == (
        "synthetic-root",
        "shared-model",
    )


def test_graph_rejects_mapping_digest_mismatch() -> None:
    lock = PackageReleaseLock.parse(lock_document())

    with pytest.raises(AgentProtocolError, match="digest mismatch"):
        PackageReleaseGraph.resolve(SHA_B, {SHA_B: lock})


def test_graph_rejects_dependency_cycle() -> None:
    first = PackageReleaseLock.parse(lock_document("cycle-a"))
    second = PackageReleaseLock.parse(lock_document("cycle-b"))
    first_key = first.digest
    second_key = second.digest
    first = replace(first, dependency_digests=(second_key,))
    second = replace(second, dependency_digests=(first_key,))

    with pytest.raises(AgentProtocolError, match="dependency cycle"):
        PackageReleaseGraph.resolve(
            first_key,
            {first_key: first, second_key: second},
        )


def test_graph_rejects_dependency_depth_above_eight() -> None:
    releases: dict[str, PackageReleaseLock] = {}
    dependency: str | None = None
    for index in reversed(range(10)):
        lock = PackageReleaseLock.parse(
            lock_document(
                f"depth-{index}",
                dependencies=[] if dependency is None else [dependency],
            )
        )
        releases[lock.digest] = lock
        dependency = lock.digest

    assert dependency is not None
    with pytest.raises(AgentProtocolError, match="depth"):
        PackageReleaseGraph.resolve(dependency, releases)


def test_graph_rejects_more_than_256_aggregate_components() -> None:
    releases: dict[str, PackageReleaseLock] = {}
    dependencies: list[str] = []
    for index in range(128):
        lock = PackageReleaseLock.parse(lock_document(f"leaf-{index}"))
        releases[lock.digest] = lock
        dependencies.append(lock.digest)
    root = PackageReleaseLock.parse(
        lock_document("component-heavy-root", dependencies=dependencies)
    )
    releases[root.digest] = root

    with pytest.raises(AgentProtocolError, match="component count"):
        PackageReleaseGraph.resolve(root.digest, releases)


def test_schema_is_packaged_and_validates_synthetic_lock() -> None:
    schema = json.loads(
        files("dgx_agent_protocol.schemas")
        .joinpath("workload-release-lock.schema.json")
        .read_text()
    )

    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert not tuple(Draft202012Validator(schema).iter_errors(lock_document()))
    assert PackageReleaseLock.parse(lock_document()).family_id == (
        "future-synthetic-stack"
    )
