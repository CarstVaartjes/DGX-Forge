from __future__ import annotations

from pathlib import Path

import pytest

from spark_profiles.fleet.legacy import LegacyFleetError, load_legacy_cluster


@pytest.fixture
def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _write_legacy(
    path: Path,
    *,
    first_ip: str = "10.0.0.10",
    first_alias: str = "lab-one",
    second_alias: str = "lab-two",
) -> Path:
    path.write_text(
        f"""
[hosts.first]
role = "head"
hostname = "spark-one"
ssh_alias = "{first_alias}"
lan_ip = "{first_ip}"

[hosts.second]
role = "worker"
hostname = "spark-two"
ssh_alias = "{second_alias}"
lan_ip = "10.0.0.11"

[fabric]
topology = "direct"
default_route = false
""".lstrip()
    )
    return path


def test_current_cluster_loads_without_rewrite(repository_root: Path) -> None:
    path = repository_root / "inventory" / "cluster.toml"
    before = path.read_bytes()

    fleet = load_legacy_cluster(path)

    assert len(fleet.nodes) == 2
    assert {node.management.host for node in fleet.nodes.values()} == {
        "dgx-spark-1",
        "dgx-spark-2",
    }
    assert {node.labels["legacy_role"] for node in fleet.nodes.values()} == {
        "head",
        "worker",
    }
    assert path.read_bytes() == before


def test_legacy_identity_does_not_change_with_address_or_hostname(
    tmp_path: Path,
) -> None:
    path = _write_legacy(tmp_path / "cluster.toml")
    first = load_legacy_cluster(path)
    path.write_text(
        path.read_text()
        .replace("10.0.0.10", "10.0.0.99")
        .replace("spark-one", "renamed-host")
    )

    second = load_legacy_cluster(path)

    assert tuple(first.nodes) == tuple(second.nodes)


def test_different_legacy_keys_get_different_generated_ids(tmp_path: Path) -> None:
    fleet = load_legacy_cluster(_write_legacy(tmp_path / "cluster.toml"))

    ids = tuple(fleet.nodes)
    assert len(set(ids)) == 2
    assert all(node_id.value.startswith("spk_") for node_id in ids)
    assert all(len(node_id.value) == 36 for node_id in ids)


def test_legacy_reader_rejects_duplicate_ssh_aliases(tmp_path: Path) -> None:
    path = _write_legacy(
        tmp_path / "cluster.toml",
        first_alias="same-alias",
        second_alias="same-alias",
    )

    with pytest.raises(LegacyFleetError, match="SSH aliases must be unique"):
        load_legacy_cluster(path)


def test_legacy_reader_rejects_empty_host_map(tmp_path: Path) -> None:
    path = tmp_path / "cluster.toml"
    path.write_text("[hosts]\n[fabric]\ntopology = 'direct'\n")

    with pytest.raises(LegacyFleetError, match="at least one host"):
        load_legacy_cluster(path)


def test_legacy_error_does_not_echo_unknown_values(tmp_path: Path) -> None:
    path = tmp_path / "cluster.toml"
    path.write_text(
        """
[hosts.first]
role = "head"
hostname = "spark-one"
ssh_alias = ["sensitive-value"]
""".lstrip()
    )

    with pytest.raises(LegacyFleetError) as caught:
        load_legacy_cluster(path)

    assert "sensitive-value" not in str(caught.value)
