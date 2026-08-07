import json
from pathlib import Path

import pytest

from dgx_control.artifact_sizes import ArtifactSizeError, DeclaredArtifactSizeResolver


def recipe():
    return json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )


def test_declared_recipe_sizes_split_model_and_digest_pinned_image() -> None:
    document = recipe()
    artifacts = DeclaredArtifactSizeResolver().resolve(document)

    assert artifacts[0].source == document["runtime"]["image"]
    assert artifacts[0].digest == "a" * 64
    assert artifacts[0].size_bytes == 5_000_000_000
    assert artifacts[1].size_bytes == 61_000_000_000
    assert len(artifacts[1].digest) == 64


def test_declared_sizes_reject_mutable_images_and_inconsistent_totals() -> None:
    document = recipe()
    document["runtime"]["image"] = "ghcr.io/vonk/vllm:latest"
    with pytest.raises(ArtifactSizeError, match="digest-pinned"):
        DeclaredArtifactSizeResolver().resolve(document)

    document = recipe()
    document["resources"]["per_node"]["installed_bytes"] = 1
    with pytest.raises(ArtifactSizeError, match="smaller"):
        DeclaredArtifactSizeResolver().resolve(document)
