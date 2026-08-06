"""Exact metadata-only artifact sizing boundary for install admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


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
