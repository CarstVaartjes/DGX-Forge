from pathlib import Path

from dgx_control.import_report import ImportDisposition
from dgx_control.recipe_contract import validate_recipe
from dgx_control.sparkrun_importer import import_sparkrun
from dgx_control.sparkrun_source import parse_sparkrun_yaml

FIXTURES = Path(__file__).parent / "fixtures/sparkrun"


def test_every_source_leaf_has_exactly_one_report_item() -> None:
    source = parse_sparkrun_yaml((FIXTURES / "full-sglang.yaml").read_bytes())
    result = import_sparkrun(source)
    source_paths = set(source.leaf_paths())
    reported_source_paths = [
        item.source_path
        for item in result.report
        if not item.source_path.startswith("/@missing/")
    ]

    assert sorted(reported_source_paths) == sorted(source_paths)
    assert len(reported_source_paths) == len(set(reported_source_paths))
    assert {item.disposition for item in result.report} <= set(ImportDisposition)
    assert any(
        item.source_path.startswith("/mods/")
        and item.disposition is ImportDisposition.INCORPORATED
        for item in result.report
    )
    assert result.runnable is False


def test_container_and_mods_become_a_source_bundle() -> None:
    source = parse_sparkrun_yaml((FIXTURES / "full-sglang.yaml").read_bytes())

    result = import_sparkrun(source)

    dockerfile = result.bundle.files["Dockerfile"].decode()
    assert dockerfile.startswith("FROM ghcr.io/example/sglang@sha256:")
    assert "COPY mods/ /opt/vonk/mods/" in dockerfile
    assert result.draft_document["build"]["context"]["sha256"] == result.bundle.sha256
    validate_recipe(result.draft_document)


def test_import_is_deterministic_and_explains_missing_requirements() -> None:
    source = parse_sparkrun_yaml((FIXTURES / "minimal-vllm.yaml").read_bytes())
    first = import_sparkrun(source)
    second = import_sparkrun(source)

    assert first == second
    assert first.source_sha256 == source.source_sha256
    assert first.report_digest == second.report_digest
    assert first.draft_document["provenance"]["source_kind"] == "sparkrun"
    assert any(
        item.source_path == "/@missing/resources"
        and item.disposition is ImportDisposition.OVERLAY_REQUIRED
        for item in first.report
    )
    assert any(
        item.source_path == "/container"
        and item.disposition is ImportDisposition.RESOLUTION_REQUIRED
        for item in first.report
    )


def test_redacted_source_never_contains_secret_values() -> None:
    source = parse_sparkrun_yaml(
        b"model: Example/Model\nruntime: vllm\ncommand: vllm serve Example/Model\ncredentials:\n  password: never-store-me\n"
    )

    result = import_sparkrun(source)

    assert result.redacted_source["credentials"]["password"] == "<redacted>"
    assert "never-store-me" not in str(result.redacted_source)
