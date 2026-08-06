from __future__ import annotations

import json
import subprocess
from pathlib import Path

from spark_profiles.platform_release import PlatformRelease
from tests.scripts.test_publish_platform_target import _bundle_descriptor, _release

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-platform-manifest"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _inputs(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    descriptor = _bundle_descriptor(b"bundle")
    release = _release(descriptor)
    del release["deployment_bundle"]
    source = tmp_path / "platform-input.json"
    descriptor_path = tmp_path / "bundle-descriptor.json"
    source.write_bytes(_canonical(release))
    descriptor_path.write_bytes(_canonical(descriptor))
    return source, descriptor_path, descriptor


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_builder_assembles_and_writes_canonical_v2_manifest(tmp_path: Path) -> None:
    source, descriptor_path, descriptor = _inputs(tmp_path)
    output = tmp_path / "platform-release.json"

    result = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--version",
        "1.2.0",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(output.read_bytes())
    assert output.read_bytes() == _canonical(document)
    assert document["deployment_bundle"] == descriptor
    assert PlatformRelease.from_bytes(output.read_bytes()).platform_version == "1.2.0"
    receipt = json.loads(result.stdout)
    assert receipt["platform_version"] == "1.2.0"
    assert receipt["target_name"].startswith("platform/releases/1.2.0/")
    assert receipt["target_sha256"] in receipt["target_name"]


def test_builder_rejects_noncanonical_input_and_version_disagreement(
    tmp_path: Path,
) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    source.write_text(json.dumps(json.loads(source.read_bytes()), indent=2))

    noncanonical = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--version",
        "1.2.0",
        "--output",
        str(tmp_path / "first.json"),
    )

    assert noncanonical.returncode == 2
    assert "canonical" in noncanonical.stderr
    source.write_bytes(_canonical(json.loads(source.read_bytes())))
    mismatch = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--version",
        "1.3.0",
        "--output",
        str(tmp_path / "second.json"),
    )
    assert mismatch.returncode == 2
    assert "version" in mismatch.stderr


def test_builder_refuses_to_overwrite_output(tmp_path: Path) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    output = tmp_path / "platform-release.json"
    output.write_text("existing\n")

    result = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--version",
        "1.2.0",
        "--output",
        str(output),
    )

    assert result.returncode == 2
    assert output.read_text() == "existing\n"


def test_builder_replaces_review_input_artifact_with_ci_evidence(tmp_path: Path) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    output = tmp_path / "platform-release.json"
    evidence_path = tmp_path / "api-evidence.json"
    evidence = {
        "artifact": {
            "name": "api",
            "provenance_sha256": "d" * 64,
            "reference": f"ghcr.io/example/api@sha256:{'e' * 64}",
            "sbom_sha256": "c" * 64,
            "sha256": "e" * 64,
            "size": 2048,
        },
        "locator": "control.images.api",
        "schema_version": 1,
    }
    evidence_path.write_bytes(_canonical(evidence))

    result = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--artifact-evidence",
        str(evidence_path),
        "--version",
        "1.2.0",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_bytes())["control"]["images"]["api"] == evidence[
        "artifact"
    ]
