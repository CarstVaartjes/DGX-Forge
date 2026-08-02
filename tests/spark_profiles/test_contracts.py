from importlib import resources
from pathlib import Path

import pytest

from spark_profiles.contracts import (
    ProfileValidationError,
    load_cluster_profile,
    load_workload,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def write_toml(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_cluster_profile_requires_both_nodes(tmp_path: Path) -> None:
    path = write_toml(
        tmp_path,
        "missing-worker.toml",
        """
id = "missing-worker"
accepted_evidence = "inventory/reports/missing-worker.json"

[placements]
spark1 = ["deepseek-agent-dual"]

[endpoints]
agent = "deepseek-agent-dual"
""",
    )

    with pytest.raises(ProfileValidationError, match="spark2"):
        load_cluster_profile(path)


def test_cluster_profile_rejects_unknown_keys(tmp_path: Path) -> None:
    path = write_toml(
        tmp_path,
        "unknown-key.toml",
        """
id = "unknown-key"
accepted_evidence = "inventory/reports/unknown-key.json"
surprise = true

[placements]
spark1 = []
spark2 = []

[endpoints]
""",
    )

    with pytest.raises(ProfileValidationError, match="surprise"):
        load_cluster_profile(path)


def test_distributed_workload_declares_rank_order() -> None:
    workload = load_workload(
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    )

    assert workload.topology == "distributed"
    assert workload.nodes == ("spark1", "spark2")
    assert workload.start_order == ("spark2", "spark1")
    assert workload.stop_order == ("spark1", "spark2")


def test_distributed_workload_requires_worker_first_start_order(
    tmp_path: Path,
) -> None:
    source = (
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    ).read_text(encoding="utf-8")
    path = write_toml(
        tmp_path,
        "head-first-start.toml",
        source.replace(
            'start_order = ["spark2", "spark1"]',
            'start_order = ["spark1", "spark2"]',
        ),
    )

    with pytest.raises(ProfileValidationError, match="worker-first"):
        load_workload(path)


def test_distributed_workload_requires_head_first_stop_order(
    tmp_path: Path,
) -> None:
    source = (
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    ).read_text(encoding="utf-8")
    path = write_toml(
        tmp_path,
        "worker-first-stop.toml",
        source.replace(
            'stop_order = ["spark1", "spark2"]',
            'stop_order = ["spark2", "spark1"]',
        ),
    )

    with pytest.raises(ProfileValidationError, match="head-first"):
        load_workload(path)


def test_contract_schemas_are_package_resources() -> None:
    workload_schema = resources.files("spark_profiles").joinpath(
        "schemas", "workload.schema.json"
    )

    assert '"additionalProperties": false' in workload_schema.read_text(
        encoding="utf-8"
    )


def test_home_workload_uses_an_immutable_image_and_declarative_adapter_commands() -> None:
    workload = load_workload(
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    )

    assert workload.image.reference == (
        "ghcr.io/anemll/dspark-vllm-gx10"
        "@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"
    )
    assert workload.endpoint.host == "127.0.0.1"
    release_sha256 = "452cb5736f2a56061b827f211d539f113a23ce43938e769a3c6cee0bdf733c29"
    adapter = (
        "/opt/spark/model-adapters/deepseek-agent-dual/releases/"
        f"{release_sha256}/bin/mia-deepseek-dual"
    )
    assert workload.commands.verify_release == (adapter, "verify-release")
    assert workload.runtime_release is not None
    assert workload.runtime_release.sha256 == release_sha256
    assert workload.deadlines is not None
    assert workload.deadlines.prepare == 86_400
    assert workload.deadlines.start == 1_800
    assert workload.checkpoint.manifest_sha256 == (
        "82e965c1caa019b31f4d776d0b3eddb0cc0d8e076f189822b8a3bbe3fa115121"
    )


def test_mia_dual_workload_uses_the_audited_immutable_runtime_contract() -> None:
    workload = load_workload(
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    )

    assert workload.source.repository == (
        "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark"
    )
    assert workload.source.commit == "b131b2a22164675890dd1465fd8862b5cfb6ff13"
    assert workload.checkpoint.repository == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert workload.checkpoint.revision == "9e165c30e2704aec5d9d593cce3eebd58bbef1cb"
    assert workload.image.reference == (
        "ghcr.io/anemll/dspark-vllm-gx10"
        "@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"
    )
    assert workload.topology == "distributed"
    assert workload.nodes == ("spark1", "spark2")
    assert workload.start_order == ("spark2", "spark1")
    assert workload.stop_order == ("spark1", "spark2")
    assert workload.endpoint.host == "127.0.0.1"
    assert workload.endpoint.port == 8888


def test_mia_documentation_marks_the_prior_staged_lane_as_superseded() -> None:
    historical_plan = (
        REPOSITORY_ROOT / "docs/superpowers/plans/2026-08-01-deepseek-0731-runtime.md"
    ).read_text(encoding="utf-8")
    platform_design = (
        REPOSITORY_ROOT
        / "docs/superpowers/specs/2026-08-01-dual-dgx-spark-platform-design.md"
    ).read_text(encoding="utf-8")
    multi_runtime_design = (
        REPOSITORY_ROOT
        / "docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md"
    ).read_text(encoding="utf-8")

    audited_commit = "b131b2a22164675890dd1465fd8862b5cfb6ff13"
    assert "Superseded by the Mia dual-Spark implementation plan" in historical_plan
    assert "Historical, superseded staged-lane design" in platform_design
    assert "Mia-first audit selected" in multi_runtime_design
    assert audited_commit in historical_plan
    assert audited_commit in platform_design
    assert audited_commit in multi_runtime_design
    assert "still planned and not accepted" in multi_runtime_design


def test_profile_overview_keeps_ds4_flash_mxfp4_deferred() -> None:
    overview = (REPOSITORY_ROOT / "docs/model-profile-overview.md").read_text(
        encoding="utf-8"
    )

    assert "DS4 Flash 0731 MXFP4 candidate, unaudited" in overview
    assert "`deepseek`" in overview


def test_workload_exposes_a_valid_manifest_digest_when_declared(
    tmp_path: Path,
) -> None:
    source = (
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    ).read_text(encoding="utf-8")
    path = write_toml(
        tmp_path,
        "manifest-digest.toml",
        source.replace(
            'manifest_sha256 = "82e965c1caa019b31f4d776d0b3eddb0cc0d8e076f189822b8a3bbe3fa115121"',
            'manifest_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
        ),
    )

    workload = load_workload(path)

    assert workload.checkpoint.manifest_sha256 == "a" * 64


def test_workload_exposes_complete_optional_runtime_contracts() -> None:
    workload = load_workload(
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    )

    assert workload.runtime_release is not None
    assert workload.runtime_release.manifest == Path(
        "adapters/deepseek/mia-vllm/runtime-manifest.json"
    )
    assert workload.runtime_release.sha256 == (
        "452cb5736f2a56061b827f211d539f113a23ce43938e769a3c6cee0bdf733c29"
    )
    assert workload.deadlines is not None
    assert workload.deadlines.start == 1800
    assert workload.deadlines.verify_release == 300


@pytest.mark.parametrize(
    ("old", "new", "error"),
    (
        (
            'sha256 = "452cb5736f2a56061b827f211d539f113a23ce43938e769a3c6cee0bdf733c29"',
            "",
            "sha256",
        ),
        (
            "prepare = 86400",
            "",
            "prepare",
        ),
        (
            "prepare = 86400",
            "prepare = 0",
            "minimum",
        ),
    ),
)
def test_workload_rejects_incomplete_or_invalid_optional_runtime_contracts(
    tmp_path: Path, old: str, new: str, error: str
) -> None:
    source = (
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    ).read_text(encoding="utf-8")
    path = write_toml(
        tmp_path, "invalid-runtime-contract.toml", source.replace(old, new)
    )

    with pytest.raises(ProfileValidationError, match=error):
        load_workload(path)


def test_workload_rejects_malformed_manifest_digest(tmp_path: Path) -> None:
    source = (
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    ).read_text(encoding="utf-8")
    path = write_toml(
        tmp_path,
        "malformed-manifest-digest.toml",
        source.replace(
            'manifest_sha256 = "82e965c1caa019b31f4d776d0b3eddb0cc0d8e076f189822b8a3bbe3fa115121"',
            'manifest_sha256 = "not-a-sha256"',
        ),
    )

    with pytest.raises(ProfileValidationError, match="does not match"):
        load_workload(path)


def test_workload_requires_every_adapter_operation(tmp_path: Path) -> None:
    path = write_toml(
        tmp_path,
        "missing-operation.toml",
        """
id = "missing-operation"
adapter = "test"
topology = "single"
placement_class = "single-exclusive"
nodes = ["spark1"]
start_order = ["spark1"]
stop_order = ["spark1"]
conflicts = []
co_location = "exclusive"
accepted_evidence = "inventory/reports/missing-operation.json"

[source]
repository = "https://example.invalid/source"
commit = "0123456789abcdef0123456789abcdef01234567"

[checkpoint]
repository = "example/model"
revision = "0123456789abcdef0123456789abcdef01234567"
manifest = "/srv/models/manifests/example.json"

[image]
reference = "example.invalid/runtime@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[paths]
cache = "/srv/models/snapshots/example"
scratch = "/srv/models/runtime-cache/example"
output = "/srv/models/outputs/example"

[endpoint]
host = "127.0.0.1"
port = 9000

[commands]
prepare = ["profile-prepare", "missing-operation"]
verify = ["profile-verify", "missing-operation"]
start = ["profile-start", "missing-operation"]
health = ["profile-health", "missing-operation"]
infer = ["profile-infer", "missing-operation"]
stop = ["profile-stop", "missing-operation"]

[resources]
minimum_free_memory_bytes = 1
minimum_free_disk_bytes = 1
stop_memory_tolerance_bytes = 1
""",
    )

    with pytest.raises(ProfileValidationError, match="verify-release"):
        load_workload(path)


def test_workload_endpoint_must_be_loopback(tmp_path: Path) -> None:
    source = (
        REPOSITORY_ROOT / "config/workloads/deepseek-agent-dual.toml"
    ).read_text(encoding="utf-8")
    path = write_toml(
        tmp_path,
        "lan-endpoint.toml",
        source.replace('host = "127.0.0.1"', 'host = "0.0.0.0"'),
    )

    with pytest.raises(ProfileValidationError, match="loopback"):
        load_workload(path)


def test_home_profile_uses_canonical_id_and_deepseek_alias() -> None:
    profile = load_cluster_profile(
        REPOSITORY_ROOT / "config/cluster-profiles/agent-full-dual.toml"
    )

    assert profile.id == "agent-full-dual"
    assert profile.placements == {
        "spark1": ("deepseek-agent-dual",),
        "spark2": ("deepseek-agent-dual",),
    }
    assert profile.endpoints == {"deepseek": "deepseek-agent-dual"}
    assert not hasattr(profile, "restore_home")


def test_cluster_profile_rejects_restoration_policy(tmp_path: Path) -> None:
    path = write_toml(
        tmp_path,
        "restoration-policy.toml",
        """
id = "restoration-policy"
restore_home = false
accepted_evidence = "inventory/reports/restoration-policy.json"

[placements]
spark1 = []
spark2 = []

[endpoints]
""",
    )

    with pytest.raises(ProfileValidationError, match="restore_home"):
        load_cluster_profile(path)


def test_cluster_profile_collections_are_immutable() -> None:
    profile = load_cluster_profile(
        REPOSITORY_ROOT / "config/cluster-profiles/agent-full-dual.toml"
    )

    with pytest.raises(TypeError):
        profile.placements["spark1"] = ()
