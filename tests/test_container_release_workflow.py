import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def workflow() -> str:
    return WORKFLOW.read_text()


def job(job_name: str) -> str:
    lines = workflow().splitlines()
    job_start = lines.index(f"  {job_name}:") + 1
    job_lines: list[str] = []
    for line in lines[job_start:]:
        if re.fullmatch(r"  [a-zA-Z0-9_-]+:", line):
            break
        job_lines.append(line)
    return "\n".join(job_lines)


def test_release_metadata_is_tag_only_and_read_only() -> None:
    text = workflow()
    assert "release-metadata:" in text
    assert "github.ref_type == 'tag'" in text
    assert "startsWith(github.ref_name, 'v')" in text
    assert "scripts/container-release-metadata" in text


def test_release_chain_is_default_off_and_dependency_gated() -> None:
    metadata = job("release-metadata")
    publisher = job("publish-images")
    manifest = job("release-manifest")

    assert "vars.DGX_CONTAINER_RELEASES_ENABLED == 'true'" in metadata
    assert "needs: [lint, generated-clients, test, release-metadata]" in publisher
    assert "needs: [release-metadata, publish-images]" in manifest


def test_publisher_needs_every_ci_gate_and_alone_can_write_packages() -> None:
    text = workflow()
    assert "needs: [lint, generated-clients, test, release-metadata]" in text
    assert text.count("packages: write") == 1
    assert text.count("contents: write") == 1
    assert "contents: read" in text


def test_publisher_uses_pinned_docker_actions_and_exact_artifacts() -> None:
    text = workflow()
    for action in (
        "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",
        "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9",
        "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8",
    ):
        assert action in text
    assert text.count("docker/build-push-action@") == 3
    for package in (
        "dgx-forge-api",
        "dgx-forge-worker",
        "dgx-forge-hermes",
    ):
        assert package in text
    assert text.count("platforms: linux/amd64") == 3
    assert text.count("provenance: mode=max") == 3
    assert text.count("sbom: true") == 3
    assert text.count("push: true") == 3


def test_complete_summary_uses_all_three_build_digests() -> None:
    text = workflow()
    for variable in (
        "CONTROL_API_IMAGE",
        "CONTROL_WORKER_IMAGE",
        "HERMES_AGENT_IMAGE",
    ):
        assert variable in text
    for step in (
        "steps.api.outputs.digest",
        "steps.worker.outputs.digest",
        "steps.hermes.outputs.digest",
    ):
        assert step in text


def test_existing_version_tags_are_refused_before_any_build() -> None:
    text = workflow()
    refusal = text.index("Refuse an existing release version")
    assert "docker buildx imagetools inspect" in text[refusal:]
    assert refusal < text.index("Build and push API image")


def test_final_job_creates_checksum_protected_public_release_asset() -> None:
    text = workflow()
    assert "release-manifest:" in text
    assert "needs: [release-metadata, publish-images]" in text
    assert "dgx-forge-images.env" in text
    assert "sha256sum" in text
    assert "gh release create" in text
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text
