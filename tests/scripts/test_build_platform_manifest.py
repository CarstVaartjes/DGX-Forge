from __future__ import annotations

import json
import subprocess
from pathlib import Path

from spark_profiles.platform_release import PlatformRelease
from tests.scripts.test_publish_platform_target import _bundle_descriptor, _release

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-platform-manifest"
FIRST_RELEASE_INPUT = ROOT / "release/platform/0.1.0.input.json"


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


def test_builder_binds_tagged_source_and_all_arm64_platform_artifacts(
    tmp_path: Path,
) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    output = tmp_path / "platform-release.json"
    build_digest = "sha256:" + "9" * 64
    locators = {
        "agents.linux-arm64": "agent-release",
        "supervisors.linux-arm64": "supervisor-release",
        "tooling.linux-arm64": "tooling-release",
    }
    evidence_paths: list[Path] = []
    for index, (locator, name) in enumerate(locators.items(), start=1):
        digest = f"{index}" * 64
        evidence = {
            "artifact": {
                "name": name,
                "provenance_sha256": "d" * 64,
                "reference": f"ghcr.io/example/{name}@sha256:{digest}",
                "sbom_sha256": "c" * 64,
                "sha256": digest,
                "size": 2048 + index,
            },
            "locator": locator,
            "payload": {
                "name": f"{name}-payload",
                "sha256": f"{index + 3}" * 64,
                "size": 4096 + index,
            },
            "schema_version": 1,
        }
        path = tmp_path / f"{name}-evidence.json"
        path.write_bytes(_canonical(evidence))
        evidence_paths.append(path)

    arguments = [
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--build-digest",
        build_digest,
    ]
    for path in evidence_paths:
        arguments.extend(("--artifact-evidence", str(path)))
    arguments.extend(("--version", "1.2.0", "--output", str(output)))
    result = _run(*arguments)

    assert result.returncode == 0, result.stderr
    document = json.loads(output.read_bytes())
    assert document["build_digest"] == build_digest
    for collection, name in (
        ("agents", "agent-release"),
        ("supervisors", "supervisor-release"),
        ("tooling", "tooling-release"),
    ):
        selected = document[collection][0]
        assert selected["architecture"] == "linux-arm64"
        assert selected["artifact"]["name"] == name
        assert selected["payload"] == {
            "name": f"{name}-payload",
            "sha256": f"{list(locators.values()).index(name) + 4}" * 64,
            "size": 4097 + list(locators.values()).index(name),
        }


def test_builder_rejects_duplicate_or_unknown_architecture_evidence(
    tmp_path: Path,
) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    evidence = {
        "artifact": {
            "name": "agent-release",
            "provenance_sha256": "d" * 64,
            "reference": f"ghcr.io/example/agent@sha256:{'e' * 64}",
            "sbom_sha256": "c" * 64,
            "sha256": "e" * 64,
            "size": 2048,
        },
        "locator": "agents.linux-x86_64",
        "payload": {
            "name": "dgx-agent",
            "sha256": "f" * 64,
            "size": 4096,
        },
        "schema_version": 1,
    }
    evidence_path = tmp_path / "agent-evidence.json"
    evidence_path.write_bytes(_canonical(evidence))

    unknown = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--artifact-evidence",
        str(evidence_path),
        "--version",
        "1.2.0",
        "--output",
        str(tmp_path / "unknown.json"),
    )
    assert unknown.returncode == 2
    assert "locator is unknown" in unknown.stderr

    evidence["locator"] = "agents.linux-arm64"
    evidence_path.write_bytes(_canonical(evidence))
    duplicate = _run(
        "--input",
        str(source),
        "--bundle-descriptor",
        str(descriptor_path),
        "--artifact-evidence",
        str(evidence_path),
        "--artifact-evidence",
        str(evidence_path),
        "--version",
        "1.2.0",
        "--output",
        str(tmp_path / "duplicate.json"),
    )
    assert duplicate.returncode == 2
    assert "overlap" in duplicate.stderr


def test_builder_requires_payload_evidence_for_architecture_artifact(
    tmp_path: Path,
) -> None:
    source, descriptor_path, _ = _inputs(tmp_path)
    evidence = {
        "artifact": {
            "name": "agent-release",
            "provenance_sha256": "d" * 64,
            "reference": f"ghcr.io/example/agent@sha256:{'e' * 64}",
            "sbom_sha256": "c" * 64,
            "sha256": "e" * 64,
            "size": 2048,
        },
        "locator": "agents.linux-arm64",
        "schema_version": 1,
    }
    evidence_path = tmp_path / "agent-evidence.json"
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
        str(tmp_path / "release.json"),
    )

    assert result.returncode == 2
    assert "payload evidence" in result.stderr


def test_first_release_input_is_canonical_complete_and_unbound() -> None:
    raw = FIRST_RELEASE_INPUT.read_bytes()
    document = json.loads(raw)

    assert raw == _canonical(document)
    assert document["schema_version"] == 2
    assert document["platform_version"] == "0.1.0"
    assert document["build_digest"] == "sha256:" + "0" * 64
    assert document["host_updater_abi"] == {"minimum": 2, "maximum": 2}
    assert document["control"]["config_version"] == 1
    assert document["control"]["protocol"] == {"minimum": 1, "maximum": 1}
    assert document["database"] == {
        "contract_revision": None,
        "expand_revision": "0014_package_action_plans",
        "predecessor_compatible": True,
    }
    assert document["rollback"] == {"predecessors": []}
    for collection, payload_name in (
        ("agents", "dgx-agent"),
        ("supervisors", "dgx-agent-supervisor"),
        ("tooling", "dgx-forge-tooling"),
    ):
        assert len(document[collection]) == 1
        selected = document[collection][0]
        assert selected["architecture"] == "linux-arm64"
        assert selected["payload"] == {
            "name": payload_name,
            "sha256": "0" * 64,
            "size": 64,
        }
    assert document["agents"][0]["protocol"] == {"minimum": 1, "maximum": 1}
    generated_artifacts = [
        document["control"]["images"]["api"],
        document["control"]["images"]["worker"],
        document["control"]["assets"][0],
        document["agents"][0]["artifact"],
        document["supervisors"][0]["artifact"],
        document["tooling"][0]["artifact"],
    ]
    assert all(item["sha256"] == "0" * 64 for item in generated_artifacts)


def test_builder_rejects_unbound_first_release_sentinels(tmp_path: Path) -> None:
    descriptor_path = tmp_path / "bundle-descriptor.json"
    descriptor_path.write_bytes(_canonical(_bundle_descriptor(b"bundle")))

    result = _run(
        "--input",
        str(FIRST_RELEASE_INPUT),
        "--bundle-descriptor",
        str(descriptor_path),
        "--version",
        "0.1.0",
        "--output",
        str(tmp_path / "release.json"),
    )

    assert result.returncode == 2
    assert "unbound release sentinel" in result.stderr
    assert not (tmp_path / "release.json").exists()


def test_first_release_input_assembles_with_complete_generated_evidence(
    tmp_path: Path,
) -> None:
    descriptor_path = tmp_path / "bundle-descriptor.json"
    descriptor_path.write_bytes(_canonical(_bundle_descriptor(b"bundle")))
    evidence_definitions = (
        ("control.images.api", "api", None),
        ("control.images.worker", "worker", None),
        ("control.assets.hermes", "hermes", None),
        ("agents.linux-arm64", "agent-linux-arm64", "dgx-agent"),
        (
            "supervisors.linux-arm64",
            "supervisor-linux-arm64",
            "dgx-agent-supervisor",
        ),
        ("tooling.linux-arm64", "tooling-linux-arm64", "dgx-forge-tooling"),
    )
    evidence_paths: list[Path] = []
    for index, (locator, name, payload_name) in enumerate(
        evidence_definitions, start=1
    ):
        digest = f"{index}" * 64
        evidence: dict[str, object] = {
            "artifact": {
                "name": name,
                "provenance_sha256": "e" * 64,
                "reference": f"ghcr.io/example/{name}@sha256:{digest}",
                "sbom_sha256": "d" * 64,
                "sha256": digest,
                "size": 1024 + index,
            },
            "locator": locator,
            "schema_version": 1,
        }
        if payload_name is not None:
            evidence["payload"] = {
                "name": payload_name,
                "sha256": f"{index + 3}" * 64,
                "size": 4096 + index,
            }
        evidence_path = tmp_path / f"{name}.json"
        evidence_path.write_bytes(_canonical(evidence))
        evidence_paths.append(evidence_path)

    output = tmp_path / "platform-release.json"
    arguments = [
        "--input",
        str(FIRST_RELEASE_INPUT),
        "--bundle-descriptor",
        str(descriptor_path),
        "--build-digest",
        "sha256:" + "9" * 64,
    ]
    for evidence_path in evidence_paths:
        arguments.extend(("--artifact-evidence", str(evidence_path)))
    arguments.extend(("--version", "0.1.0", "--output", str(output)))

    result = _run(*arguments)

    assert result.returncode == 0, result.stderr
    release = PlatformRelease.from_bytes(output.read_bytes())
    assert release.platform_version == "0.1.0"
    assert release.agent_for("linux-arm64").payload_name == "dgx-agent"
    assert release.predecessors == ()
