from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from spark_profiles.catalog import Catalog, CatalogError, validate_evidence_indexes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def catalog_root(tmp_path: Path) -> Path:
    for relative in (
        "config/workloads",
        "config/cluster-profiles",
        "config/profile-selectors.toml",
        "locks/model-definitions.toml",
        "inventory/reports/model-definitions.json",
        "inventory/reports/accepted-cluster-profiles.json",
    ):
        source = REPOSITORY_ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    return tmp_path


def test_default_selector_resolves_to_canonical_home(catalog_root: Path) -> None:
    catalog = Catalog.load(catalog_root)

    assert catalog.resolve_profile("default").id == "agent-full-dual"


def test_definition_change_invalidates_lock(catalog_root: Path) -> None:
    workload = catalog_root / "config/workloads/deepseek-agent-dual.toml"
    workload.write_text(
        workload.read_text(encoding="utf-8").replace(
            "minimum_free_memory_bytes = 120000000000",
            "minimum_free_memory_bytes = 120000000001",
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="lock fingerprint"):
        Catalog.load(catalog_root)


def test_toml_comments_do_not_change_definition_fingerprint(catalog_root: Path) -> None:
    catalog = Catalog.load(catalog_root)
    fingerprint = catalog.definition_fingerprints["deepseek-agent-dual"]
    workload = catalog_root / "config/workloads/deepseek-agent-dual.toml"
    workload.write_text(
        "# A comment is not part of a declarative definition.\n"
        + workload.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    reloaded = Catalog.load(catalog_root)

    assert reloaded.definition_fingerprints["deepseek-agent-dual"] == fingerprint


def test_evidence_indexes_satisfy_packaged_schemas(catalog_root: Path) -> None:
    validate_evidence_indexes(catalog_root)


def test_evidence_indexes_are_json_objects(catalog_root: Path) -> None:
    """The fixture itself remains legible before the catalog validates it."""
    for name in ("model-definitions.json", "accepted-cluster-profiles.json"):
        with (catalog_root / "inventory/reports" / name).open(encoding="utf-8") as file:
            assert isinstance(json.load(file), dict)
