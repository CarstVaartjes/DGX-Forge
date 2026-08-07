"""Legacy workload projections are backed by generic package documents."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent_protocol" / "src"))

from vonk_agent_protocol import PackageReleaseLock

from cluster_profiles.catalog import Catalog
from cluster_profiles.workload_packages import PackageFamily


def _toml(path: Path) -> dict[str, object]:
    with path.open("rb") as source:
        return tomllib.load(source)


def test_legacy_mia_and_ds4_definitions_project_to_generic_releases() -> None:
    # Removing the migration reader or replacing it with a model-name branch
    # would leave the public definitions without their generic deployment.
    catalog = Catalog.load(ROOT)

    expected = {
        "deepseek-agent-dual": "mia-deepseek-dual",
        "deepseek-agent-single": "ds4-deepseek-single",
    }
    assert set(expected).issubset(catalog.legacy_workload_deployments)
    assert {
        legacy_id: catalog.legacy_workload_deployments[legacy_id].deployment_id
        for legacy_id in expected
    } == expected
    default_profile = catalog.resolve_profile("default")
    assert {
        workload_id: catalog.legacy_workload_deployments[workload_id].deployment_id
        for workload_id in default_profile.endpoints.values()
    } == {"deepseek-agent-dual": "mia-deepseek-dual"}

    for legacy_id, deployment_id in expected.items():
        legacy = catalog.definitions[legacy_id]
        deployment = catalog.legacy_workload_deployments[legacy_id]
        family = catalog.package_families[deployment.family_id]
        lock_path = (
            ROOT
            / "manifests/workload-releases"
            / deployment.family_id
            / f"{deployment.release_digest}.json"
        )
        lock = PackageReleaseLock.parse(lock_path.read_bytes())

        assert deployment_id in catalog.workload_deployments
        assert deployment.routing["alias"] == legacy_id
        assert deployment.selector["node_count"] == len(legacy.nodes)
        assert deployment.ports[deployment.routing["port"]] == legacy.endpoint.port
        assert (
            deployment.resources["memory_bytes"]
            == legacy.resources.minimum_free_memory_bytes
        )
        assert (
            deployment.resources["storage_bytes"]
            == legacy.resources.minimum_free_disk_bytes
        )
        assert lock.family_id == family.family_id == deployment.family_id
        assert lock.digest == deployment.release_digest
        assert lock.adapter_abi == family.execution["adapter_abi"]
        assert catalog.maturity[legacy_id] in {"verified", "accepted"}


def test_release_locks_preserve_immutable_legacy_pins_without_model_engine_branches() -> (
    None
):
    catalog = Catalog.load(ROOT)

    for legacy_id, deployment in catalog.legacy_workload_deployments.items():
        legacy = catalog.definitions[legacy_id]
        lock_path = (
            ROOT
            / "manifests/workload-releases"
            / deployment.family_id
            / f"{deployment.release_digest}.json"
        )
        lock = json.loads(lock_path.read_text(encoding="utf-8"))

        sources = [
            source
            for component in [*lock["components"], lock["adapter"]]
            for source in component["sources"]
        ]
        assert {
            "provider": "git",
            "repository": legacy.source.repository,
            "commit": legacy.source.commit,
        } in sources
        assert {
            "provider": "huggingface",
            "repository": legacy.checkpoint.repository,
            "revision": legacy.checkpoint.revision,
        } in sources
        assert {"provider": "oci", "reference": legacy.image.reference} in sources
        assert legacy.runtime_release is not None
        assert any(
            component["digest"] == f"sha256:{legacy.runtime_release.sha256}"
            for component in [*lock["components"], lock["adapter"]]
        )

    package_engine = ROOT / "agent/src/vonk_agent/packages"
    forbidden = (
        "mia",
        "ds4",
        "deepseek",
        "deepseek-agent-dual",
        "deepseek-agent-single",
    )
    assert all(
        token not in source.read_text(encoding="utf-8").lower()
        for source in package_engine.rglob("*.py")
        for token in forbidden
    )


def test_unknown_family_uses_the_same_generic_contract_without_catalog_changes() -> (
    None
):
    document = _toml(ROOT / "config/package-families/mia-deepseek.toml")
    document["family_id"] = "unknown-after-release"
    document["source"] = {
        "provider": "signed-http-index",
        "locator": "https://packages.example.invalid/unknown/index.json",
        "policy_refs": ["policy://origins/unknown"],
    }

    family = PackageFamily.load(document)

    assert family.family_id == "unknown-after-release"
    assert (
        family.repository_path.as_posix()
        == "config/package-families/unknown-after-release.toml"
    )
