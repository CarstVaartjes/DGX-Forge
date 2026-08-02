from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from spark_profiles.admission import check_admission
from spark_profiles.catalog import Catalog, fingerprint


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.load(REPOSITORY_ROOT)


@pytest.fixture
def profile(catalog: Catalog):
    return catalog.resolve_profile("default")


@pytest.fixture
def healthy_inventory() -> dict[str, dict[str, int | bool]]:
    return {
        "spark1": {
            "free_memory_bytes": 240_000_000_000,
            "free_disk_bytes": 800_000_000_000,
            "healthy": True,
        },
        "spark2": {
            "free_memory_bytes": 240_000_000_000,
            "free_disk_bytes": 800_000_000_000,
            "healthy": True,
        },
    }


def mark_definition_accepted(catalog: Catalog, identifier: str) -> None:
    definition = catalog.definitions[identifier]
    accepted_definition = replace(
        definition,
        checkpoint=replace(definition.checkpoint, manifest_sha256="a" * 64),
    )
    catalog.definitions = {identifier: accepted_definition}
    catalog.definition_fingerprints = {identifier: fingerprint(accepted_definition)}
    catalog.maturity[identifier] = "accepted"
    catalog.maturity_fingerprints = {identifier: fingerprint(accepted_definition)}


def test_unknown_workload_is_rejected(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    unknown = replace(profile, placements={"spark1": ("missing",), "spark2": ()})

    assert check_admission(unknown, catalog, healthy_inventory).ok is False


def test_colocation_requires_exact_acceptance(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    mark_definition_accepted(catalog, "deepseek-agent-dual")
    colocated = replace(
        profile,
        placements={
            "spark1": ("deepseek-agent-dual", "deepseek-agent-dual"),
            "spark2": ("deepseek-agent-dual", "deepseek-agent-dual"),
        },
    )

    report = check_admission(colocated, catalog, healthy_inventory, accepted={})

    assert report.errors == ("profile has no accepted co-location evidence",)


def test_distributed_workload_reserves_both_nodes(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    mark_definition_accepted(catalog, "deepseek-agent-dual")
    partial = replace(
        profile,
        placements={"spark1": ("deepseek-agent-dual",), "spark2": ()},
    )

    assert "distributed reservation" in check_admission(
        partial, catalog, healthy_inventory
    ).errors[0]


def test_planned_definition_blocks_production_home(
    catalog: Catalog, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    report = check_admission(catalog.resolve_profile("default"), catalog, healthy_inventory)

    assert "deepseek-agent-dual maturity is planned" in report.errors


def test_accepted_definition_requires_manifest_digest(
    catalog: Catalog, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    catalog.maturity["deepseek-agent-dual"] = "accepted"

    report = check_admission(catalog.resolve_profile("default"), catalog, healthy_inventory)

    assert "accepted definition requires manifest_sha256" in report.errors


def test_port_collision_is_rejected(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    mark_definition_accepted(catalog, "deepseek-agent-dual")
    duplicate = replace(profile, endpoints={"one": "deepseek-agent-dual", "two": "deepseek-agent-dual"})

    assert "port collision" in check_admission(
        duplicate, catalog, healthy_inventory
    ).errors[0]


def test_unhealthy_endpoint_is_not_published(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    healthy_inventory["spark1"]["healthy"] = False

    report = check_admission(profile, catalog, healthy_inventory)

    assert "unhealthy" in " ".join(report.errors)
