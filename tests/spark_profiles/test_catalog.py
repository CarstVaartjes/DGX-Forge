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
        "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md",
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


def _read_report(catalog_root: Path, name: str) -> dict:
    with (catalog_root / "inventory/reports" / name).open(encoding="utf-8") as source:
        return json.load(source)


def _write_report(catalog_root: Path, name: str, report: dict) -> None:
    (catalog_root / "inventory/reports" / name).write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )


def _transition(
    state: str,
    timestamp: str,
    *,
    rejection_reason: str | None = None,
) -> dict:
    return {
        "state": state,
        "timestamp": timestamp,
        "evidence_refs": [
            "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
        ],
        "rejection_reason": rejection_reason,
    }


def _profile_workload_ids(profile) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                identifier
                for identifiers in profile.placements.values()
                for identifier in identifiers
            }
        )
    )


def test_definition_evidence_requires_transition_history(catalog_root: Path) -> None:
    """Dropping auditable history from a maturity record must fail closed."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0].pop("history", None)
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="history.*required"):
        Catalog.load(catalog_root)


def test_definition_evidence_rejects_illegal_maturity_progression(
    catalog_root: Path,
) -> None:
    """Skipping preparation and verification must not produce accepted evidence."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "accepted"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z"),
        _transition("accepted", "2026-08-02T08:01:00Z"),
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="illegal maturity transition.*planned.*accepted"):
        Catalog.load(catalog_root)


def test_definition_current_maturity_must_match_history(catalog_root: Path) -> None:
    """A stale current-state field must not disagree with its audit trail."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "prepared"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z")
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="current maturity does not match history"):
        Catalog.load(catalog_root)


@pytest.mark.parametrize(
    ("state", "rejection_reason"),
    (("rejected", None), ("verified", "runtime output regressed")),
)
def test_rejection_reason_is_present_only_for_rejected_transitions(
    catalog_root: Path,
    state: str,
    rejection_reason: str | None,
) -> None:
    """A missing or misplaced rejection reason must invalidate evidence."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = state
    report["definitions"][0]["history"] = [
        _transition(
            state,
            "2026-08-02T08:00:00Z",
            rejection_reason=rejection_reason,
        )
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="rejection_reason"):
        Catalog.load(catalog_root)


def test_rejected_definition_cannot_be_silently_corrected(catalog_root: Path) -> None:
    """Removing the terminal rejection rule must make this regression fail."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "verified"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:00:00Z"),
        _transition("prepared", "2026-08-02T08:01:00Z"),
        _transition("verified", "2026-08-02T08:02:00Z"),
        _transition(
            "rejected",
            "2026-08-02T08:03:00Z",
            rejection_reason="runtime output regressed",
        ),
        _transition("verified", "2026-08-02T08:04:00Z"),
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="rejected maturity is terminal"):
        Catalog.load(catalog_root)


def test_maturity_history_timestamps_must_increase(catalog_root: Path) -> None:
    """Reordered audit entries must not be accepted as a valid history."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["maturity"] = "prepared"
    report["definitions"][0]["history"] = [
        _transition("planned", "2026-08-02T08:01:00Z"),
        _transition("prepared", "2026-08-02T08:00:00Z"),
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="timestamps must increase"):
        Catalog.load(catalog_root)


def test_maturity_history_evidence_reference_must_exist(catalog_root: Path) -> None:
    """A plausible-looking but absent evidence path must not be auditable."""
    report = _read_report(catalog_root, "model-definitions.json")
    report["definitions"][0]["history"][0]["evidence_refs"] = [
        "docs/audits/not-checked-in.md"
    ]
    _write_report(catalog_root, "model-definitions.json", report)

    with pytest.raises(CatalogError, match="evidence reference does not exist"):
        Catalog.load(catalog_root)


def test_accepted_profile_evidence_requires_audit_metadata(catalog_root: Path) -> None:
    """An otherwise exact profile hash must not be accepted without provenance."""
    catalog = Catalog.load(catalog_root)
    profile = catalog.resolve_profile("default")
    report = {
        "profiles": [
            {
                "profile_sha256": catalog.profile_fingerprints[profile.id],
                "definition_sha256": sorted(
                    catalog.definition_fingerprints[identifier]
                    for identifier in _profile_workload_ids(profile)
                ),
            }
        ]
    }
    _write_report(catalog_root, "accepted-cluster-profiles.json", report)

    with pytest.raises(CatalogError, match="accepted_at.*required"):
        Catalog.load(catalog_root)


def test_accepted_profile_evidence_preserves_public_hash_mapping(
    catalog_root: Path,
) -> None:
    """Audit metadata must not change the accepted_profiles public mapping."""
    catalog = Catalog.load(catalog_root)
    profile = catalog.resolve_profile("default")
    profile_hash = catalog.profile_fingerprints[profile.id]
    definition_hashes = tuple(
        sorted(
            catalog.definition_fingerprints[identifier]
            for identifier in _profile_workload_ids(profile)
        )
    )
    report = {
        "profiles": [
            {
                "profile_sha256": profile_hash,
                "definition_sha256": list(definition_hashes),
                "accepted_at": "2026-08-02T08:00:00Z",
                "evidence_refs": [
                    "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
                ],
            }
        ]
    }
    _write_report(catalog_root, "accepted-cluster-profiles.json", report)

    loaded = Catalog.load(catalog_root)

    assert loaded.accepted_profiles == {profile_hash: definition_hashes}


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("accepted_at", "not-a-timestamp", "invalid timestamp"),
        ("evidence_refs", ["docs/audits/not-checked-in.md"], "evidence reference does not exist"),
    ),
)
def test_accepted_profile_audit_metadata_is_semantically_validated(
    catalog_root: Path,
    field: str,
    value: str | list[str],
    error: str,
) -> None:
    """Removing timestamp or evidence-path validation must fail this check."""
    catalog = Catalog.load(catalog_root)
    profile = catalog.resolve_profile("default")
    report = {
        "profiles": [
            {
                "profile_sha256": catalog.profile_fingerprints[profile.id],
                "definition_sha256": sorted(
                    catalog.definition_fingerprints[identifier]
                    for identifier in _profile_workload_ids(profile)
                ),
                "accepted_at": "2026-08-02T08:00:00Z",
                "evidence_refs": [
                    "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
                ],
            }
        ]
    }
    report["profiles"][0][field] = value
    _write_report(catalog_root, "accepted-cluster-profiles.json", report)

    with pytest.raises(CatalogError, match=error):
        Catalog.load(catalog_root)
