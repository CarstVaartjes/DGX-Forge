import os
import re
import subprocess
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


def workflow_step(job_name: str, step_name: str) -> str:
    lines = job(job_name).splitlines()
    step_start = lines.index(f"      - name: {step_name}")
    step_lines: list[str] = []
    for line in lines[step_start:]:
        if line.startswith("      - name: ") and step_lines:
            break
        step_lines.append(line)
    return "\n".join(step_lines)


def step_run(job_name: str, step_name: str) -> str:
    lines = workflow_step(job_name, step_name).splitlines()
    run_start = lines.index("        run: |") + 1
    run_lines: list[str] = []
    for line in lines[run_start:]:
        if line and not line.startswith("          "):
            break
        run_lines.append(line[10:] if line else "")
    return "\n".join(run_lines)


def step_block(job_name: str, step_name: str, key: str) -> list[str]:
    lines = workflow_step(job_name, step_name).splitlines()
    block_start = lines.index(f"          {key}: |") + 1
    block_lines: list[str] = []
    for line in lines[block_start:]:
        if line and not line.startswith("            "):
            break
        block_lines.append(line[12:] if line else "")
    return block_lines


def release_expressions() -> dict[str, str]:
    digest = f"sha256:{'a' * 64}"
    return {
        "${{ needs.release-metadata.outputs.version }}": "1.2.3",
        "${{ needs.release-metadata.outputs.api_image }}": "ghcr.io/example/api",
        "${{ needs.release-metadata.outputs.worker_image }}": "ghcr.io/example/worker",
        "${{ needs.release-metadata.outputs.hermes_image }}": "ghcr.io/example/hermes",
        "${{ needs.publish-images.outputs.api_digest }}": digest,
        "${{ needs.publish-images.outputs.worker_digest }}": digest,
        "${{ needs.publish-images.outputs.hermes_digest }}": digest,
    }


def rendered_step_run(job_name: str, step_name: str) -> str:
    script = step_run(job_name, step_name)
    for expression, value in release_expressions().items():
        script = script.replace(expression, value)
    validator = ROOT / "scripts/validate-container-release-digests"
    return script.replace("scripts/validate-container-release-digests", str(validator))


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
    metadata = job("release-metadata")
    publisher = job("publish-images")
    manifest = job("release-manifest")

    assert "needs: [lint, generated-clients, test, release-metadata]" in publisher
    assert "permissions:\n      contents: read\n      packages: write" in publisher
    assert "packages: write" not in metadata
    assert "packages: write" not in manifest
    assert "permissions:\n      contents: write" in manifest
    assert "contents: write" not in metadata
    assert "contents: write" not in publisher
    for read_only_job in ("lint", "generated-clients", "test"):
        assert "packages: write" not in job(read_only_job)
        assert "contents: write" not in job(read_only_job)
    assert workflow().count("packages: write") == 1
    assert workflow().count("contents: write") == 1


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


def test_public_input_scanner_runs_before_every_image_build() -> None:
    publisher = job("publish-images")
    scanner = publisher.index("scripts/verify-public-image-inputs")
    for build in (
        "Build and push API image",
        "Build and push worker image",
        "Build and push Hermes image",
    ):
        assert scanner < publisher.index(build)


def test_each_build_has_its_exact_three_tag_set() -> None:
    for step_name, image_output in (
        ("Build and push API image", "api_image"),
        ("Build and push worker image", "worker_image"),
        ("Build and push Hermes image", "hermes_image"),
    ):
        image = f"${{{{ needs.release-metadata.outputs.{image_output} }}}}"
        assert step_block("publish-images", step_name, "tags") == [
            f"{image}:${{{{ needs.release-metadata.outputs.version }}}}",
            f"{image}:${{{{ needs.release-metadata.outputs.commit_tag }}}}",
            f"{image}:latest",
        ]


def test_existing_version_guard_allows_only_known_absence(
    tmp_path: Path,
) -> None:
    publisher = job("publish-images")
    assert publisher.index("Refuse an existing release version") < publisher.index(
        "Build and push API image"
    )
    assert "scripts/refuse-existing-image-version" in workflow_step(
        "publish-images", "Refuse an existing release version"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "case $DOCKER_MODE in\n"
        "  absent) echo \"ERROR: $4: not found\" >&2; exit 1 ;;\n"
        "  existing) exit 0 ;;\n"
        "  registry-error) echo 'ERROR: registry returned 503' >&2; exit 1 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    script = rendered_step_run("publish-images", "Refuse an existing release version")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RELEASE_VERSION": "1.2.3",
        "CONTROL_API_IMAGE": "ghcr.io/example/api",
        "CONTROL_WORKER_IMAGE": "ghcr.io/example/worker",
        "HERMES_AGENT_IMAGE": "ghcr.io/example/hermes",
    }

    results = {
        mode: subprocess.run(
            ["bash", "-c", f"set -euo pipefail\n{script}"],
            cwd=ROOT,
            env={**environment, "DOCKER_MODE": mode},
            check=False,
            capture_output=True,
            text=True,
        )
        for mode in ("absent", "existing", "registry-error")
    }

    assert results["absent"].returncode == 0, results["absent"].stderr
    assert results["existing"].returncode != 0
    assert results["registry-error"].returncode != 0
    assert "registry returned 503" not in results["registry-error"].stderr


def test_manifest_receives_digests_only_through_environment() -> None:
    step = workflow_step("release-manifest", "Create digest-pinned image environment")
    run = step_run("release-manifest", "Create digest-pinned image environment")

    for name, output in (
        ("CONTROL_API_DIGEST", "api_digest"),
        ("CONTROL_WORKER_DIGEST", "worker_digest"),
        ("HERMES_AGENT_DIGEST", "hermes_digest"),
    ):
        assert f"{name}: ${{{{ needs.publish-images.outputs.{output} }}}}" in step
        assert f"needs.publish-images.outputs.{output}" not in run


def test_manifest_rejects_invalid_digests_before_creating_assets(
    tmp_path: Path,
) -> None:
    script = rendered_step_run(
        "release-manifest", "Create digest-pinned image environment"
    )
    valid = f"sha256:{'a' * 64}"
    invalid_sets = (
        ("", valid, valid),
        (valid, "sha256:abc", valid),
        (valid, valid, f"sha256:{'A' * 64}"),
    )

    for index, digests in enumerate(invalid_sets):
        target = tmp_path / str(index)
        target.mkdir()
        result = subprocess.run(
            ["bash", "-c", f"set -euo pipefail\n{script}"],
            cwd=target,
            env={
                **os.environ,
                "CONTROL_API_IMAGE": "ghcr.io/example/api:1.2.3",
                "CONTROL_WORKER_IMAGE": "ghcr.io/example/worker:1.2.3",
                "HERMES_AGENT_IMAGE": "ghcr.io/example/hermes:1.2.3",
                "CONTROL_API_DIGEST": digests[0],
                "CONTROL_WORKER_DIGEST": digests[1],
                "HERMES_AGENT_DIGEST": digests[2],
            },
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        assert not (target / "dgx-forge-images.env").exists()
        assert not (target / "dgx-forge-images.env.sha256").exists()


def test_manifest_accepts_valid_digests_and_checksums_the_asset(
    tmp_path: Path,
) -> None:
    script = rendered_step_run(
        "release-manifest", "Create digest-pinned image environment"
    )
    digest = f"sha256:{'a' * 64}"
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{script}"],
        cwd=tmp_path,
        env={
            **os.environ,
            "CONTROL_API_IMAGE": "ghcr.io/example/api:1.2.3",
            "CONTROL_WORKER_IMAGE": "ghcr.io/example/worker:1.2.3",
            "HERMES_AGENT_IMAGE": "ghcr.io/example/hermes:1.2.3",
            "CONTROL_API_DIGEST": digest,
            "CONTROL_WORKER_DIGEST": digest,
            "HERMES_AGENT_DIGEST": digest,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "dgx-forge-images.env").read_text() == (
        f"CONTROL_API_IMAGE=ghcr.io/example/api:1.2.3@{digest}\n"
        f"CONTROL_WORKER_IMAGE=ghcr.io/example/worker:1.2.3@{digest}\n"
        f"HERMES_AGENT_IMAGE=ghcr.io/example/hermes:1.2.3@{digest}\n"
    )
    checksum = subprocess.run(
        ["sha256sum", "--check", "dgx-forge-images.env.sha256"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert checksum.returncode == 0, checksum.stderr


def test_final_job_creates_checksum_protected_public_release_asset() -> None:
    text = workflow()
    assert "release-manifest:" in text
    assert "needs: [release-metadata, publish-images]" in text
    assert "dgx-forge-images.env" in text
    assert "sha256sum" in text
    assert "gh release create" in text
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text
