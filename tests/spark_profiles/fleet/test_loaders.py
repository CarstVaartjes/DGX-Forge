from __future__ import annotations

from pathlib import Path

import pytest

from spark_profiles.fleet import NodeId
from spark_profiles.fleet.loaders import FleetLoadError, load_fleet


@pytest.fixture
def fixture_path() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures" / "fleet" / "generic.toml"


def test_load_fleet_uses_generated_ids_and_preserves_connection_data(
    fixture_path: Path,
) -> None:
    fleet = load_fleet(fixture_path)

    assert tuple(node.display_name for node in fleet.ready_nodes()) == (
        "alpha",
        "beta",
    )
    beta = fleet.node(NodeId.parse("spk_00000000000000000000000000000002"))
    assert beta.management.host == "spark-beta.local"
    assert beta.management.user == "operator"
    assert beta.management.port == 2222
    assert beta.management.credential_ref is None


def test_load_fleet_does_not_modify_source(fixture_path: Path) -> None:
    before = fixture_path.read_bytes()

    load_fleet(fixture_path)

    assert fixture_path.read_bytes() == before


def test_load_fleet_rejects_duplicate_display_names(tmp_path: Path) -> None:
    path = tmp_path / "fleet.toml"
    path.write_text(
        """
schema_version = 2
[nodes.spk_00000000000000000000000000000001]
display_name = "same"
hostname = "one"
lifecycle = "ready"
[nodes.spk_00000000000000000000000000000001.management]
host = "one.local"
user = "operator"
port = 22
[nodes.spk_00000000000000000000000000000001.labels]
[nodes.spk_00000000000000000000000000000002]
display_name = "same"
hostname = "two"
lifecycle = "ready"
[nodes.spk_00000000000000000000000000000002.management]
host = "two.local"
user = "operator"
port = 22
[nodes.spk_00000000000000000000000000000002.labels]
""".lstrip()
    )

    with pytest.raises(FleetLoadError, match="display names must be unique"):
        load_fleet(path)


@pytest.mark.parametrize(
    "content",
    [
        "schema_version = 3\n[nodes]\n",
        "schema_version = 2\nunknown = true\n[nodes]\n",
        "schema_version = 2\n[nodes.spark1]\n",
    ],
)
def test_load_fleet_rejects_unsupported_or_unknown_structure(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "fleet.toml"
    path.write_text(content)

    with pytest.raises(FleetLoadError):
        load_fleet(path)


def test_load_fleet_error_does_not_echo_credential_value(tmp_path: Path) -> None:
    path = tmp_path / "fleet.toml"
    path.write_text(
        """
schema_version = 2
[nodes.spk_00000000000000000000000000000001]
display_name = "alpha"
hostname = "alpha"
lifecycle = "ready"
[nodes.spk_00000000000000000000000000000001.management]
host = "alpha.local"
user = "operator"
port = "secret-value-that-must-not-leak"
[nodes.spk_00000000000000000000000000000001.labels]
""".lstrip()
    )

    with pytest.raises(FleetLoadError) as caught:
        load_fleet(path)

    assert "secret-value-that-must-not-leak" not in str(caught.value)
