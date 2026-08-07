"""Exact metadata-only artifact sizing boundary for install admission."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_PINNED_IMAGE = re.compile(r"^.+@sha256:([0-9a-f]{64})$")


class ArtifactSizeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactSize:
    source: str
    digest: str
    size_bytes: int


class ArtifactSizeResolver(Protocol):
    def resolve(self, recipe: Mapping[str, object]) -> tuple[ArtifactSize, ...]: ...


class StaticArtifactSizeResolver:
    def __init__(self, artifacts: tuple[ArtifactSize, ...]) -> None:
        self._artifacts = artifacts

    def resolve(self, recipe: Mapping[str, object]) -> tuple[ArtifactSize, ...]:
        runtime = recipe.get("runtime"); source_artifacts = recipe.get("artifacts")
        if not isinstance(runtime, Mapping) or not isinstance(runtime.get("image"), str) or not isinstance(source_artifacts, list):
            raise ArtifactSizeError("recipe artifact identities are invalid")
        expected = {runtime["image"]}
        for item in source_artifacts:
            if not isinstance(item, Mapping): raise ArtifactSizeError("recipe artifact identity is invalid")
            expected.add(f"{item.get('repository')}@{item.get('revision')}")
        by_source = {item.source: item for item in self._artifacts}
        if set(by_source) != expected or any(len(item.digest) != 64 or item.size_bytes < 0 for item in by_source.values()):
            raise ArtifactSizeError("artifact sizes are incomplete")
        return tuple(by_source[source] for source in sorted(by_source))


class DeclaredArtifactSizeResolver:
    """Resolve sizes already frozen into a validated immutable recipe.

    Model revisions and their declared byte counts are bound into a stable
    local identity. The remaining installed footprint belongs to the exact OCI
    image. This resolver performs no network I/O and therefore preserves local
    operation when the global catalog is unavailable.
    """

    def resolve(self, recipe: Mapping[str, object]) -> tuple[ArtifactSize, ...]:
        runtime = recipe.get("runtime")
        artifacts = recipe.get("artifacts")
        resources = recipe.get("resources")
        if (
            not isinstance(runtime, Mapping)
            or not isinstance(artifacts, list)
            or not isinstance(resources, Mapping)
            or not isinstance(resources.get("per_node"), Mapping)
        ):
            raise ArtifactSizeError("recipe artifact sizes are invalid")
        image = runtime.get("image")
        match = _PINNED_IMAGE.fullmatch(image) if isinstance(image, str) else None
        if match is None:
            raise ArtifactSizeError("recipe image must be digest-pinned")
        installed = resources["per_node"].get("installed_bytes")
        if not isinstance(installed, int) or isinstance(installed, bool) or installed < 1:
            raise ArtifactSizeError("recipe installed size is invalid")
        resolved: list[ArtifactSize] = []
        model_total = 0
        seen: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise ArtifactSizeError("recipe artifact size is invalid")
            repository = artifact.get("repository")
            revision = artifact.get("revision")
            expected = artifact.get("expected_bytes")
            if (
                not isinstance(repository, str)
                or not isinstance(revision, str)
                or not isinstance(expected, int)
                or isinstance(expected, bool)
                or expected < 1
            ):
                raise ArtifactSizeError("recipe artifact size is invalid")
            source = f"{repository}@{revision}"
            if source in seen:
                raise ArtifactSizeError("recipe artifact identity is duplicated")
            seen.add(source)
            identity = hashlib.sha256(
                f"{source}\0{expected}".encode()
            ).hexdigest()
            resolved.append(ArtifactSize(source, identity, expected))
            model_total += expected
        image_size = installed - model_total
        if image_size < 1:
            raise ArtifactSizeError(
                "installed size is smaller than the declared artifact total"
            )
        return (
            ArtifactSize(image, match.group(1), image_size),
            *tuple(sorted(resolved, key=lambda item: item.source)),
        )
