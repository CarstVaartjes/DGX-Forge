from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from spark_profiles.admission import check_admission
from spark_profiles.catalog import Catalog, fingerprint
from spark_profiles.contracts import WorkloadDefinition

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


def mark_definitions_accepted(
    catalog: Catalog, *definitions: WorkloadDefinition
) -> tuple[WorkloadDefinition, ...]:
    accepted_definitions = tuple(
        replace(
            definition,
            checkpoint=replace(definition.checkpoint, manifest_sha256="a" * 64),
        )
        for definition in definitions
    )
    catalog.definitions = {
        definition.id: definition for definition in accepted_definitions
    }
    catalog.definition_fingerprints = {
        definition.id: fingerprint(definition) for definition in accepted_definitions
    }
    catalog.maturity = {
        definition.id: "accepted" for definition in accepted_definitions
    }
    catalog.maturity_fingerprints = dict(catalog.definition_fingerprints)
    return accepted_definitions


def mark_definition_accepted(catalog: Catalog, identifier: str) -> None:
    mark_definitions_accepted(catalog, catalog.definitions[identifier])


def single_definition(
    catalog: Catalog,
    identifier: str,
    *,
    placement_class: str,
    co_location: str,
    port: int,
) -> WorkloadDefinition:
    base = catalog.definitions["deepseek-agent-dual"]
    return replace(
        base,
        id=identifier,
        topology="single",
        placement_class=placement_class,
        nodes=("spark1",),
        start_order=("spark1",),
        stop_order=("spark1",),
        co_location=co_location,
        paths=replace(
            base.paths,
            cache=Path(f"/srv/models/snapshots/{identifier}"),
            scratch=Path(f"/srv/models/runtime-cache/{identifier}"),
            output=Path(f"/srv/models/outputs/{identifier}"),
        ),
        endpoint=replace(base.endpoint, port=port),
        resources=replace(
            base.resources,
            minimum_free_memory_bytes=10_000_000_000,
            minimum_free_disk_bytes=10_000_000_000,
        ),
    )


def exact_evidence(profile, catalog: Catalog) -> dict[str, tuple[str, ...]]:
    identifiers = {
        identifier
        for node_identifiers in profile.placements.values()
        for identifier in node_identifiers
    }
    return {
        fingerprint(profile): tuple(
            sorted(
                catalog.definition_fingerprints[identifier]
                for identifier in identifiers
            )
        )
    }


def test_unknown_workload_is_rejected(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    unknown = replace(profile, placements={"spark1": ("missing",), "spark2": ()})

    assert check_admission(unknown, catalog, healthy_inventory).ok is False


def test_serving_profile_requires_exact_acceptance(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    """Removing the profile evidence gate must make this regression fail."""
    mark_definition_accepted(catalog, "deepseek-agent-dual")

    report = check_admission(profile, catalog, healthy_inventory, accepted={})

    assert report.errors == ("profile has no exact accepted evidence",)


def test_serving_profile_with_exact_acceptance_passes(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    """Using a different profile hash or definition set must fail this check."""
    mark_definition_accepted(catalog, "deepseek-agent-dual")
    evidence = {
        fingerprint(profile): (catalog.definition_fingerprints["deepseek-agent-dual"],)
    }

    report = check_admission(profile, catalog, healthy_inventory, accepted=evidence)

    assert report.ok is True


def test_colocation_requires_exact_acceptance(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    definitions = mark_definitions_accepted(
        catalog,
        single_definition(
            catalog,
            "shareable-one",
            placement_class="single-shareable",
            co_location="accepted",
            port=9001,
        ),
        single_definition(
            catalog,
            "shareable-two",
            placement_class="single-shareable",
            co_location="accepted",
            port=9002,
        ),
    )
    colocated = replace(
        profile,
        placements={
            "spark1": tuple(item.id for item in definitions),
            "spark2": (),
        },
        endpoints={},
    )

    report = check_admission(colocated, catalog, healthy_inventory, accepted={})

    assert report.errors == ("profile has no exact accepted evidence",)


def test_exact_evidence_does_not_allow_exclusive_workloads_to_co_reside(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    """Removing the definition-level exclusivity gate must make this test fail."""
    definitions = mark_definitions_accepted(
        catalog,
        single_definition(
            catalog,
            "exclusive-one",
            placement_class="single-exclusive",
            co_location="exclusive",
            port=9001,
        ),
        single_definition(
            catalog,
            "exclusive-two",
            placement_class="single-exclusive",
            co_location="exclusive",
            port=9002,
        ),
    )
    colocated = replace(
        profile,
        placements={"spark1": tuple(item.id for item in definitions), "spark2": ()},
        endpoints={},
    )

    report = check_admission(
        colocated,
        catalog,
        healthy_inventory,
        accepted=exact_evidence(colocated, catalog),
    )

    assert report.errors == (
        "incompatible co-location on spark1: exclusive-one, exclusive-two",
    )


def test_exact_evidence_allows_compatible_shareable_workloads_to_co_reside(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    definitions = mark_definitions_accepted(
        catalog,
        single_definition(
            catalog,
            "shareable-one",
            placement_class="single-shareable",
            co_location="accepted",
            port=9001,
        ),
        single_definition(
            catalog,
            "shareable-two",
            placement_class="single-shareable",
            co_location="accepted",
            port=9002,
        ),
    )
    colocated = replace(
        profile,
        placements={"spark1": tuple(item.id for item in definitions), "spark2": ()},
        endpoints={},
    )

    report = check_admission(
        colocated,
        catalog,
        healthy_inventory,
        accepted=exact_evidence(colocated, catalog),
    )

    assert report.ok is True


@pytest.mark.parametrize(
    ("topology", "placement_class", "co_location"),
    (
        ("distributed", "dual-exclusive", "accepted"),
        ("distributed", "dual-pipeline-experimental", "accepted"),
        ("distributed", "single-exclusive", "exclusive"),
        ("single", "dual-exclusive", "exclusive"),
        ("single", "dual-pipeline-experimental", "exclusive"),
        ("single", "single-exclusive", "accepted"),
        ("single", "single-shareable", "exclusive"),
    ),
)
def test_inconsistent_topology_placement_declaration_is_rejected(
    catalog: Catalog,
    profile,
    healthy_inventory: dict[str, dict[str, int | bool]],
    topology: str,
    placement_class: str,
    co_location: str,
) -> None:
    base = catalog.definitions["deepseek-agent-dual"]
    nodes = ("spark1", "spark2") if topology == "distributed" else ("spark1",)
    definition = replace(
        base,
        topology=topology,
        placement_class=placement_class,
        co_location=co_location,
        nodes=nodes,
        start_order=tuple(reversed(nodes)),
        stop_order=nodes,
    )
    (definition,) = mark_definitions_accepted(catalog, definition)
    inconsistent = replace(
        profile,
        placements={
            "spark1": (definition.id,),
            "spark2": (definition.id,) if topology == "distributed" else (),
        },
        endpoints={},
    )

    report = check_admission(
        inconsistent,
        catalog,
        healthy_inventory,
        accepted=exact_evidence(inconsistent, catalog),
    )

    assert report.errors == (
        "inconsistent workload placement declaration: deepseek-agent-dual",
    )


def test_distributed_pipeline_placement_is_structurally_valid(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    definition = replace(
        catalog.definitions["deepseek-agent-dual"],
        placement_class="dual-pipeline-experimental",
    )
    (definition,) = mark_definitions_accepted(catalog, definition)
    pipeline = replace(profile, endpoints={})

    report = check_admission(
        pipeline,
        catalog,
        healthy_inventory,
        accepted={},
    )

    assert report.errors == ("profile has no exact accepted evidence",)


def test_distributed_workload_reserves_both_nodes(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    mark_definition_accepted(catalog, "deepseek-agent-dual")
    partial = replace(
        profile,
        placements={"spark1": ("deepseek-agent-dual",), "spark2": ()},
    )

    assert (
        "distributed reservation"
        in check_admission(partial, catalog, healthy_inventory).errors[0]
    )


def test_planned_definition_blocks_production_home(
    catalog: Catalog, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    report = check_admission(
        catalog.resolve_profile("default"), catalog, healthy_inventory
    )

    assert "deepseek-agent-dual maturity is planned" in report.errors


def test_accepted_definition_requires_manifest_digest(
    catalog: Catalog, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    definition = replace(
        catalog.definitions["deepseek-agent-dual"],
        checkpoint=replace(
            catalog.definitions["deepseek-agent-dual"].checkpoint,
            manifest_sha256=None,
        ),
    )
    catalog.definitions = {definition.id: definition}
    catalog.definition_fingerprints = {definition.id: fingerprint(definition)}
    catalog.maturity = {definition.id: "accepted"}
    catalog.maturity_fingerprints = dict(catalog.definition_fingerprints)

    report = check_admission(
        catalog.resolve_profile("default"), catalog, healthy_inventory
    )

    assert "accepted definition requires manifest_sha256" in report.errors


def test_port_collision_is_rejected(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    mark_definition_accepted(catalog, "deepseek-agent-dual")
    duplicate = replace(
        profile, endpoints={"one": "deepseek-agent-dual", "two": "deepseek-agent-dual"}
    )

    assert (
        "port collision"
        in check_admission(duplicate, catalog, healthy_inventory).errors[0]
    )


def test_endpoint_target_must_be_assigned_to_the_profile(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    mark_definition_accepted(catalog, "deepseek-agent-dual")
    stopped = replace(profile, placements={"spark1": (), "spark2": ()})

    report = check_admission(stopped, catalog, healthy_inventory)

    assert report.errors == (
        "endpoint deepseek targets unassigned workload: deepseek-agent-dual",
    )


def test_unhealthy_endpoint_is_not_published(
    catalog: Catalog, profile, healthy_inventory: dict[str, dict[str, int | bool]]
) -> None:
    healthy_inventory["spark1"]["healthy"] = False

    report = check_admission(profile, catalog, healthy_inventory)

    assert "unhealthy" in " ".join(report.errors)
