# GitHub Container Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish public, provenance-bearing DGX-Forge API, worker, and hardened Hermes images from GHCR only after a stable version tag passes the existing CI gates, then make production Compose consume their immutable digests.

**Architecture:** A small repository script owns strict tag-to-image metadata generation, while the existing CI workflow owns privileged GHCR publication after all read-only validation jobs pass. A separate least-privilege job turns the three resolved digests into a checksum-protected public GitHub Release asset, and weekly Dependabot pull requests propose reviewed upstream updates. Compose keeps its internal service names but replaces the local Hermes build with a required published image reference; supply-chain verification binds the release scripts, workflow, image manifest, updater configuration, and narrowed build contexts.

**Tech Stack:** GitHub Actions, GHCR, Docker Buildx/BuildKit, Docker Compose, Python 3.12, pytest, OCI SBOM/provenance, existing `scripts/verify-supply-chain` evidence.

## Global Constraints

- Publish only tags matching `^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`; do not publish branches, pull requests, prereleases, or build metadata.
- Publish exactly `ghcr.io/carstvaartjes/dgx-forge-api`, `ghcr.io/carstvaartjes/dgx-forge-worker`, and `ghcr.io/carstvaartjes/dgx-forge-hermes`.
- Apply version `X.Y.Z`, full `sha-<40 lowercase hexadecimal characters>`, and `latest` tags only from a successful stable version-tag release.
- Treat `latest` as evaluation-only; production always deploys the complete three-image digest set.
- Build only `linux/amd64` in this implementation.
- Keep Compose service/DNS names `control-api`, `control-worker`, and `hermes-agent` unchanged.
- Require digest-pinned `CONTROL_API_IMAGE`, `CONTROL_WORKER_IMAGE`, and `HERMES_AGENT_IMAGE` at deployment; production Compose must contain no `build:` directive.
- Use the job-scoped `GITHUB_TOKEN`; never add a PAT or pass any credential into a Docker build.
- Keep ordinary CI permissions read-only and grant `packages: write` only to the publication job.
- Grant `contents: write` only to the final release-manifest job; that job receives no package permission.
- Generate OCI SBOM and maximum-mode provenance for all three images.
- Attach `dgx-forge-images.env` and its SHA-256 checksum to the public GitHub Release only after every image digest is available.
- Configure weekly Dependabot pull requests for Dockerfiles, Docker Compose, and GitHub Actions without auto-merge or automatic publication.
- Keep runtime addresses, credentials, private keys, state, and workspaces outside public image layers.
- Pin every GitHub Action to a full commit SHA.
- Build the public Hermes wrapper with UID/GID `1100:1100`; NAS data paths and the retained `HERMES_UID`/`HERMES_GID` settings must remain exactly `1100` for official releases.

---

### Task 1: Define and test stable release metadata

**Files:**
- Create: `scripts/container-release-metadata`
- Create: `tests/scripts/test_container_release_metadata.py`

**Interfaces:**
- Consumes: positional `REF_TYPE`, `REF_NAME`, and full lowercase Git commit ID.
- Produces: newline-delimited GitHub output assignments named `version`, `commit_tag`, `api_image`, `worker_image`, and `hermes_image`; exits `64` without output for invalid input.

- [ ] **Step 1: Write failing CLI contract tests**

Create `tests/scripts/test_container_release_metadata.py` with subprocess tests that exercise the executable as an operator and as GitHub Actions will use it:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/container-release-metadata"
SHA = "0123456789abcdef0123456789abcdef01234567"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_stable_tag_emits_exact_public_package_metadata() -> None:
    result = run("tag", "v1.2.3", SHA)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "version=1.2.3",
        f"commit_tag=sha-{SHA}",
        "api_image=ghcr.io/carstvaartjes/dgx-forge-api",
        "worker_image=ghcr.io/carstvaartjes/dgx-forge-worker",
        "hermes_image=ghcr.io/carstvaartjes/dgx-forge-hermes",
    ]


@pytest.mark.parametrize(
    ("ref_type", "ref_name", "commit"),
    (
        ("branch", "v1.2.3", SHA),
        ("tag", "1.2.3", SHA),
        ("tag", "v01.2.3", SHA),
        ("tag", "v1.2", SHA),
        ("tag", "v1.2.3-rc.1", SHA),
        ("tag", "v1.2.3+build.1", SHA),
        ("tag", "v1.2.3", SHA.upper()),
        ("tag", "v1.2.3", SHA[:-1]),
    ),
)
def test_non_release_input_fails_closed(
    ref_type: str, ref_name: str, commit: str
) -> None:
    result = run(ref_type, ref_name, commit)
    assert result.returncode == 64
    assert result.stdout == ""
    assert "release metadata is invalid" in result.stderr
```

- [ ] **Step 2: Run the tests and verify the executable is absent**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 \
  pytest tests/scripts/test_container_release_metadata.py -v
```

Expected: FAIL because `scripts/container-release-metadata` does not exist.

- [ ] **Step 3: Implement the minimal deterministic metadata command**

Create `scripts/container-release-metadata`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from collections.abc import Sequence

TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
PACKAGES = (
    ("api_image", "ghcr.io/carstvaartjes/dgx-forge-api"),
    ("worker_image", "ghcr.io/carstvaartjes/dgx-forge-worker"),
    ("hermes_image", "ghcr.io/carstvaartjes/dgx-forge-hermes"),
)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        print("release metadata is invalid", file=sys.stderr)
        return 64
    ref_type, ref_name, commit = arguments
    match = TAG.fullmatch(ref_name)
    if ref_type != "tag" or match is None or COMMIT.fullmatch(commit) is None:
        print("release metadata is invalid", file=sys.stderr)
        return 64
    print(f"version={ref_name[1:]}")
    print(f"commit_tag=sha-{commit}")
    for name, image in PACKAGES:
        print(f"{name}={image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Make it executable:

```bash
chmod 0755 scripts/container-release-metadata
```

- [ ] **Step 4: Run the focused tests and direct dry run**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 \
  pytest tests/scripts/test_container_release_metadata.py -v
scripts/container-release-metadata tag v1.2.3 \
  0123456789abcdef0123456789abcdef01234567
```

Expected: pytest PASS; the command emits the exact five assignments asserted by the test and performs no registry operation.

- [ ] **Step 5: Commit the metadata contract**

```bash
git add scripts/container-release-metadata \
  tests/scripts/test_container_release_metadata.py
git commit -m "feat: define container release metadata"
```

---

### Task 2: Make all three production images explicit Compose artifacts

**Files:**
- Modify: `deploy/compose/hermes-agent/Dockerfile`
- Modify: `deploy/compose/hermes-agent/compose.yaml`
- Modify: `deploy/compose/tests/test.env`
- Modify: `deploy/compose/tests/test_hermes_agent.py`
- Modify: `deploy/compose/tests/hermes-agent-runtime.sh`
- Modify: `deploy/compose/tests/test_networking.py`
- Modify: `deploy/compose/images.lock.json`
- Modify: `scripts/verify-supply-chain`
- Modify: `tests/scripts/test_verify_supply_chain.py`
- Modify: `inventory/sbom/manifest.json`

**Interfaces:**
- Consumes: `HERMES_AGENT_IMAGE` as a complete digest-pinned OCI reference.
- Produces: a production Compose model with three required release images and no local build; an image-lock record containing package, context, Dockerfile, and target for each artifact.

- [ ] **Step 1: Add failing Compose and supply-chain assertions**

Add this value to `deploy/compose/tests/test.env`:

```dotenv
HERMES_AGENT_IMAGE=example/hermes:1@sha256:7777777777777777777777777777777777777777777777777777777777777777
```

Extend `test_compose_hermes_is_unpublished_bounded_and_segmented` in `deploy/compose/tests/test_hermes_agent.py`:

```python
assert service["image"] == (
    "example/hermes:1@sha256:"
    "7777777777777777777777777777777777777777777777777777777777777777"
)
assert "build" not in service
```

Add a supply-chain contract test to `tests/scripts/test_verify_supply_chain.py`:

```python
def test_image_lock_declares_all_three_public_release_artifacts() -> None:
    lock = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())
    assert lock["release_images"] == [
        {
            "context": ".",
            "dockerfile": "control/Dockerfile",
            "environment": "CONTROL_API_IMAGE",
            "package": "dgx-forge-api",
            "required": True,
            "target": "api",
        },
        {
            "context": ".",
            "dockerfile": "control/Dockerfile",
            "environment": "CONTROL_WORKER_IMAGE",
            "package": "dgx-forge-worker",
            "required": True,
            "target": "worker",
        },
        {
            "context": "deploy/compose/hermes-agent",
            "dockerfile": "deploy/compose/hermes-agent/Dockerfile",
            "environment": "HERMES_AGENT_IMAGE",
            "package": "dgx-forge-hermes",
            "required": True,
            "target": "managed",
        },
    ]
```

- [ ] **Step 2: Run focused tests and verify they fail against the local Hermes build**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest \
  deploy/compose/tests/test_hermes_agent.py \
  tests/scripts/test_verify_supply_chain.py -v
```

Expected: FAIL because Compose still has `build:` and the lock contains only two release artifacts.

- [ ] **Step 3: Convert the Hermes service to a required published image**

Name the derived Dockerfile stage:

```dockerfile
ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a
FROM ${HERMES_IMAGE} AS managed
```

Replace the `build:` block and local image in the `x-hermes-service` anchor with:

```yaml
image: ${HERMES_AGENT_IMAGE:?set a digest-pinned DGX-Forge Hermes image}
```

Do not change the internal service name, networks, capabilities, mounts, health checks, setup profile, or upstream-base `HERMES_IMAGE` Dockerfile argument.

- [ ] **Step 4: Make the runtime harness build locally without changing production Compose**

Before rendering Compose in `deploy/compose/tests/hermes-agent-runtime.sh`, build the harness image explicitly and export its complete test reference:

```bash
docker build \
  --file "${compose_root}/hermes-agent/Dockerfile" \
  --tag local/hermes-agent:managed \
  "${compose_root}/hermes-agent"
export HERMES_AGENT_IMAGE=local/hermes-agent:managed
```

Remove `docker compose build hermes-agent`; retain every runtime security, health, persistence, and recreation assertion.

- [ ] **Step 5: Expand the release-image lock and verifier**

Move the official Hermes reference from `images["hermes-agent"]` to
`build_bases["hermes"]`, because it is now a build input rather than a deployed
runtime image. Replace `release_images` in `deploy/compose/images.lock.json`
with the exact three records asserted in Step 1. Update `expected_releases` in
`scripts/verify-supply-chain` to the same records, require all three `${...:?`
expressions in included Compose text, verify that each context and Dockerfile
exists, and require `FROM ${HERMES_IMAGE} AS managed` in the Hermes Dockerfile.

Update `image_errors` so entries under `images` must occur in Compose, the Node
and Python build bases must occur in `control/Dockerfile`, and the Hermes build
base must occur in `deploy/compose/hermes-agent/Dockerfile`. Update
`test_image_lock_contains_only_the_pinned_hermes_runtime` to assert
`lock["build_bases"]["hermes"]` and the absence of `hermes-agent` from
`lock["images"]`. In `test_networking.py`, assert the rendered Hermes runtime
environment has exact UID/GID strings `1100` so the official release cannot be
documented with a mismatched filesystem identity.

Change the verifier failure text to:

```text
API, worker, and Hermes release images must be explicitly digest-pinned at deployment
```

- [ ] **Step 6: Regenerate evidence and run focused verification**

Run:

```bash
scripts/verify-supply-chain --generate --json
scripts/verify-supply-chain --json
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest \
  deploy/compose/tests/test_hermes_agent.py \
  deploy/compose/tests/test_networking.py \
  tests/scripts/test_verify_supply_chain.py -v
```

Expected: both verifier runs report `"ok":true`; all focused tests PASS.

- [ ] **Step 7: Commit the immutable Compose transition**

```bash
git add deploy/compose/hermes-agent/Dockerfile \
  deploy/compose/hermes-agent/compose.yaml \
  deploy/compose/tests/test.env \
  deploy/compose/tests/test_hermes_agent.py \
  deploy/compose/tests/hermes-agent-runtime.sh \
  deploy/compose/tests/test_networking.py \
  deploy/compose/images.lock.json scripts/verify-supply-chain \
  tests/scripts/test_verify_supply_chain.py inventory/sbom/manifest.json
git commit -m "feat: pull immutable Hermes release image"
```

---

### Task 3: Reject credentials from public image inputs

**Files:**
- Create: `scripts/verify-public-image-inputs`
- Create: `tests/scripts/test_verify_public_image_inputs.py`

**Interfaces:**
- Consumes: an optional repository root, defaulting to the script's parent repository.
- Produces: exit `0` with `public image inputs: PASS`, or exit `1` with only affected paths and pattern names; never prints matched credential bytes.

- [ ] **Step 1: Write failing public-context tests**

Create a temporary-tree test that copies the exact public-image inputs, verifies the real repository, injects a token into a copied control source, and verifies fail-closed output:

```python
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-public-image-inputs"


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, str(root)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_repository_public_image_inputs_are_clean() -> None:
    result = run(ROOT)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "public image inputs: PASS\n"


def test_live_token_pattern_is_rejected_without_echoing_value(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "control/src/dgx_control"
    source.mkdir(parents=True)
    value = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    (source / "leak.py").write_text(f'KEY = "{value}"\n')
    result = run(repository)
    assert result.returncode == 1
    assert "control/src/dgx_control/leak.py: github-token" in result.stderr
    assert value not in result.stderr
```

- [ ] **Step 2: Run the focused tests and verify the scanner is absent**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 \
  pytest tests/scripts/test_verify_public_image_inputs.py -v
```

Expected: FAIL because `scripts/verify-public-image-inputs` does not exist.

- [ ] **Step 3: Implement bounded scanning over only image inputs**

Create an executable Python script that recursively scans regular files under:

```python
INPUTS = (
    "control/Dockerfile",
    "control/pyproject.toml",
    "control/src",
    "control/web/package.json",
    "control/web/package-lock.json",
    "control/web/src",
    "inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl",
    "deploy/compose/hermes-agent/Dockerfile",
    "deploy/compose/hermes-agent/entrypoint.sh",
)

PATTERNS = {
    "private-key": rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "github-token": rb"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "tailscale-key": rb"tskey-[A-Za-z0-9_-]{10,}",
    "aws-access-key": rb"AKIA[0-9A-Z]{16}",
    "model-api-key": rb"sk-[A-Za-z0-9]{32,}",
}
```

Resolve the root, reject symlinks within `INPUTS`, read files as bytes, collect at most one finding per pattern and path, and print only `<relative-path>: <pattern-name>` to stderr. Missing inputs that are absent in a deliberately minimal test tree are skipped; every input exists in the real repository and remains enforced by the Dockerfiles and supply-chain verifier.

Make the script executable:

```bash
chmod 0755 scripts/verify-public-image-inputs
```

- [ ] **Step 4: Run clean, injected-secret, and key-header cases**

Extend the test module with a private-key-header case, then run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 \
  pytest tests/scripts/test_verify_public_image_inputs.py -v
scripts/verify-public-image-inputs
```

Expected: tests PASS and the direct command prints `public image inputs: PASS`.

- [ ] **Step 5: Commit the public-input gate**

```bash
git add scripts/verify-public-image-inputs \
  tests/scripts/test_verify_public_image_inputs.py
git commit -m "feat: reject secrets from public image inputs"
```

---

### Task 4: Publish the gated images from existing CI

**Files:**
- Create: `tests/test_container_release_workflow.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `scripts/verify-supply-chain`
- Modify: `tests/scripts/test_verify_supply_chain.py`
- Modify: `inventory/sbom/manifest.json`

**Interfaces:**
- Consumes: outputs from `scripts/container-release-metadata`, successful `lint`, `generated-clients`, and complete `test` matrix jobs, and separate job-scoped `GITHUB_TOKEN` permissions.
- Produces: three `linux/amd64` GHCR images with version, full commit, and `latest` tags; each build emits SBOM and provenance; the successful workflow emits all three digest-pinned Compose assignments in its summary and a checksum-protected public `dgx-forge-images.env` release asset.

- [ ] **Step 1: Write failing workflow-structure tests**

Create `tests/test_container_release_workflow.py`. Isolate job text with the same line-oriented approach used by `tests/test_ci_platform_boundaries.py`, then assert:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def workflow() -> str:
    return WORKFLOW.read_text()


def test_release_metadata_is_tag_only_and_read_only() -> None:
    text = workflow()
    assert "release-metadata:" in text
    assert "github.ref_type == 'tag'" in text
    assert "startsWith(github.ref_name, 'v')" in text
    assert "scripts/container-release-metadata" in text


def test_publisher_needs_every_ci_gate_and_alone_can_write_packages() -> None:
    text = workflow()
    assert "needs: [lint, generated-clients, test, release-metadata]" in text
    assert text.count("packages: write") == 1
    assert text.count("contents: write") == 1
    assert "contents: read" in text


def test_publisher_uses_pinned_docker_actions_and_exact_artifacts() -> None:
    text = workflow()
    for action in (
        "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",  # v3.12.0
        "docker/login-action@dbcb813823bdd20940b903addbd779551569679f",  # v4.6.0
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",  # v7.3.0
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
    for step in ("steps.api.outputs.digest", "steps.worker.outputs.digest", "steps.hermes.outputs.digest"):
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
```

- [ ] **Step 2: Run the workflow tests and verify release jobs are absent**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 \
  pytest tests/test_container_release_workflow.py -v
```

Expected: FAIL because `release-metadata`, `publish-images`, and
`release-manifest` are absent.

- [ ] **Step 3: Add the read-only metadata job**

Append a job to `.github/workflows/ci.yml` with this contract:

```yaml
  release-metadata:
    name: Validate container release tag
    if: >-
      github.event_name == 'push' &&
      github.ref_type == 'tag' &&
      startsWith(github.ref_name, 'v')
    runs-on: ubuntu-latest
    permissions:
      contents: read
    outputs:
      version: ${{ steps.release.outputs.version }}
      commit_tag: ${{ steps.release.outputs.commit_tag }}
      api_image: ${{ steps.release.outputs.api_image }}
      worker_image: ${{ steps.release.outputs.worker_image }}
      hermes_image: ${{ steps.release.outputs.hermes_image }}
    steps:
      - name: Check out tagged commit
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Validate release metadata
        id: release
        run: >-
          scripts/container-release-metadata
          "$GITHUB_REF_TYPE" "$GITHUB_REF_NAME" "$GITHUB_SHA"
          >> "$GITHUB_OUTPUT"
```

The exact parser supplies the second validation layer after the broad `v` job condition.

- [ ] **Step 4: Add the privileged publication job**

Add `publish-images` with:

```yaml
  publish-images:
    name: Publish public container images
    needs: [lint, generated-clients, test, release-metadata]
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - name: Check out tagged commit
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Verify public image inputs
        run: scripts/verify-public-image-inputs
      - name: Verify supply-chain evidence
        run: scripts/verify-supply-chain --json
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f # v3.12.0
      - name: Log in to GHCR
        uses: docker/login-action@dbcb813823bdd20940b903addbd779551569679f # v4.6.0
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
```

Immediately after login and before the first build, add a step named
`Refuse an existing release version`. For each of the three image outputs, run
`docker buildx imagetools inspect "${image}:${version}"`; if any reference
already exists, exit nonzero without pushing. A partial failed publication is
recovered by releasing a new version (or by explicit registry administration),
never by silently overwriting the existing stable version tag.

Follow with three `docker/build-push-action` steps named `api`, `worker`, and `hermes`. Each uses the exact pinned action SHA, `platforms: linux/amd64`, `push: true`, `sbom: true`, and `provenance: mode=max`. API and worker use context `.`, file `control/Dockerfile`, and targets `api`/`worker`; Hermes uses context `deploy/compose/hermes-agent`, file `deploy/compose/hermes-agent/Dockerfile`, and target `managed`.

For each image, set tags exactly as:

```yaml
tags: |
  ${{ needs.release-metadata.outputs.api_image }}:${{ needs.release-metadata.outputs.version }}
  ${{ needs.release-metadata.outputs.api_image }}:${{ needs.release-metadata.outputs.commit_tag }}
  ${{ needs.release-metadata.outputs.api_image }}:latest
```

Repeat with `worker_image` and `hermes_image`. Add OCI source, revision, and version labels. Expose `steps.api.outputs.digest`, `steps.worker.outputs.digest`, and `steps.hermes.outputs.digest` as the `api_digest`, `worker_digest`, and `hermes_digest` outputs of `publish-images`. Append one fenced `dotenv` block to `$GITHUB_STEP_SUMMARY` only after all three build steps succeed, composing each line from the corresponding image output, version output, and build digest.

- [ ] **Step 5: Create the final public release-manifest job**

Add a separate job whose only write authority is repository release content:

```yaml
  release-manifest:
    name: Publish container release manifest
    needs: [release-metadata, publish-images]
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Create digest-pinned image environment
        run: |
          printf '%s\n' \
            "CONTROL_API_IMAGE=${{ needs.release-metadata.outputs.api_image }}:${{ needs.release-metadata.outputs.version }}@${{ needs.publish-images.outputs.api_digest }}" \
            "CONTROL_WORKER_IMAGE=${{ needs.release-metadata.outputs.worker_image }}:${{ needs.release-metadata.outputs.version }}@${{ needs.publish-images.outputs.worker_digest }}" \
            "HERMES_AGENT_IMAGE=${{ needs.release-metadata.outputs.hermes_image }}:${{ needs.release-metadata.outputs.version }}@${{ needs.publish-images.outputs.hermes_digest }}" \
            > dgx-forge-images.env
          sha256sum dgx-forge-images.env > dgx-forge-images.env.sha256
      - name: Create public GitHub Release
        run: >-
          gh release create "$GITHUB_REF_NAME"
          dgx-forge-images.env dgx-forge-images.env.sha256
          --repo "$GITHUB_REPOSITORY"
          --verify-tag
          --title "DGX-Forge ${{ needs.release-metadata.outputs.version }}"
          --generate-notes
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Do not grant `packages: write` to this job. `gh release create` must fail rather than replace an existing release, preserving the immutable-version rule.

- [ ] **Step 6: Bind the release logic into offline evidence**

Add these paths to `input_paths` in `scripts/verify-supply-chain` and to `_copy` in `tests/scripts/test_verify_supply_chain.py`:

```text
.github/workflows/ci.yml
scripts/container-release-metadata
scripts/verify-public-image-inputs
```

Regenerate the evidence:

```bash
scripts/verify-supply-chain --generate --json
```

- [ ] **Step 7: Run workflow, security, and supply-chain tests**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest \
  tests/test_container_release_workflow.py \
  tests/scripts/test_container_release_metadata.py \
  tests/scripts/test_verify_public_image_inputs.py \
  tests/scripts/test_verify_supply_chain.py -v
scripts/verify-public-image-inputs
scripts/verify-supply-chain --json
```

Expected: all tests PASS; both verifier commands exit `0`.

- [ ] **Step 8: Commit the gated publisher**

```bash
git add .github/workflows/ci.yml tests/test_container_release_workflow.py \
  scripts/verify-supply-chain tests/scripts/test_verify_supply_chain.py \
  inventory/sbom/manifest.json
git commit -m "feat: publish tagged images to GHCR"
```

---

### Task 5: Propose reviewed upstream updates weekly

**Files:**
- Create: `.github/dependabot.yml`
- Create: `tests/test_dependabot_updates.py`
- Modify: `scripts/verify-supply-chain`
- Modify: `tests/scripts/test_verify_supply_chain.py`
- Modify: `inventory/sbom/manifest.json`

**Interfaces:**
- Consumes: GitHub's supported `docker`, `docker-compose`, and `github-actions` ecosystems across the exact repository directories containing manifests.
- Produces: weekly dependency pull requests against the default branch; produces no merge, version tag, container publication, release, or NAS mutation.

- [ ] **Step 1: Write a failing updater-policy test**

Create `tests/test_dependabot_updates.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github/dependabot.yml"


def test_dependabot_covers_every_container_and_action_location_weekly() -> None:
    text = CONFIG.read_text()
    assert text.startswith("version: 2\n")
    assert 'package-ecosystem: "docker"' in text
    assert 'package-ecosystem: "docker-compose"' in text
    assert 'package-ecosystem: "github-actions"' in text
    for directory in (
        '      - "/control"',
        '      - "/deploy/compose/hermes-agent"',
        '      - "/deploy/compose"',
        '      - "/deploy/compose/tailscale"',
    ):
        assert directory in text
    assert text.count('interval: "weekly"') == 3
    assert "automerge" not in text.lower()
```

- [ ] **Step 2: Run the focused test and verify the configuration is absent**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 \
  pytest tests/test_dependabot_updates.py -v
```

Expected: FAIL because `.github/dependabot.yml` does not exist.

- [ ] **Step 3: Add the exact weekly update configuration**

Create `.github/dependabot.yml`:

```yaml
version: 2
updates:
  - package-ecosystem: "docker"
    directories:
      - "/control"
      - "/deploy/compose/hermes-agent"
    schedule:
      interval: "weekly"
      day: "monday"
      time: "04:00"
      timezone: "Europe/Amsterdam"
    labels: ["dependencies", "containers"]
    open-pull-requests-limit: 5

  - package-ecosystem: "docker-compose"
    directories:
      - "/deploy/compose"
      - "/deploy/compose/tailscale"
    schedule:
      interval: "weekly"
      day: "monday"
      time: "04:00"
      timezone: "Europe/Amsterdam"
    labels: ["dependencies", "containers"]
    open-pull-requests-limit: 5

  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
      time: "04:00"
      timezone: "Europe/Amsterdam"
    labels: ["dependencies", "github-actions"]
    open-pull-requests-limit: 5
```

The Compose expressions and `images.lock.json` intentionally remain protected by supply-chain verification: if Dependabot changes one reference but cannot update its duplicate lock/evidence record, CI fails until the reviewed PR synchronizes the lock and regenerates evidence.

- [ ] **Step 4: Bind and verify updater configuration**

Add `.github/dependabot.yml` to `input_paths` in `scripts/verify-supply-chain` and `_copy` in `tests/scripts/test_verify_supply_chain.py`, then run:

```bash
scripts/verify-supply-chain --generate --json
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest \
  tests/test_dependabot_updates.py tests/scripts/test_verify_supply_chain.py -v
scripts/verify-supply-chain --json
```

Expected: tests PASS and the verifier reports `"ok":true`.

- [ ] **Step 5: Commit the reviewed-update policy**

```bash
git add .github/dependabot.yml tests/test_dependabot_updates.py \
  scripts/verify-supply-chain tests/scripts/test_verify_supply_chain.py \
  inventory/sbom/manifest.json
git commit -m "chore: propose weekly container updates"
```

---

### Task 6: Document the NAS pull-only deployment and verify the release path

**Files:**
- Create: `deploy/compose/README.md`
- Create: `tests/runbooks/test_nas_compose.py`
- Modify: `deploy/compose/.env.example`
- Modify: `README.md`
- Modify: `docs/runbooks/control-plane-bootstrap.md`
- Modify: `docs/runbooks/hermes-agent.md`
- Modify: `docs/runbooks/supply-chain.md`

**Interfaces:**
- Consumes: the checksum-protected public `dgx-forge-images.env` release asset and NAS-local environment/secret files.
- Produces: one authoritative NAS Compose entry point covering configuration, secrets, first boot, pull/update, GHCR visibility, and rollback.

- [ ] **Step 1: Write failing documentation contract tests**

Create `tests/runbooks/test_nas_compose.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_README = ROOT / "deploy/compose/README.md"
ENVIRONMENT = ROOT / "deploy/compose/.env.example"


def test_nas_compose_readme_is_the_complete_operator_entry_point() -> None:
    text = COMPOSE_README.read_text()
    for required in (
        "ghcr.io/carstvaartjes/dgx-forge-api",
        "ghcr.io/carstvaartjes/dgx-forge-worker",
        "ghcr.io/carstvaartjes/dgx-forge-hermes",
        "NAS_LAN_IP=10.0.0.2",
        "docker compose pull",
        "docker compose config --quiet",
        "compose.step-ca.yaml",
        "dgx-forge-images.env",
        "dgx-forge-images.env.sha256",
        "latest is evaluation-only",
        "Set package visibility to Public",
        "not the Docker bridge",
        "not the public WAN address",
    ):
        assert required in text


def test_environment_requires_three_release_images_without_duplicate_networks() -> None:
    text = ENVIRONMENT.read_text()
    assert "CONTROL_API_IMAGE=ghcr.io/carstvaartjes/dgx-forge-api:" in text
    assert "CONTROL_WORKER_IMAGE=ghcr.io/carstvaartjes/dgx-forge-worker:" in text
    assert "HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/dgx-forge-hermes:" in text
    assert text.count("DGX_MANAGEMENT_CIDRS=") == 1
    assert text.count("DGX_DIRECT_FABRIC_CIDRS=") == 1
```

- [ ] **Step 2: Run the documentation tests and verify the entry point is absent**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 \
  pytest tests/runbooks/test_nas_compose.py -v
```

Expected: FAIL because `deploy/compose/README.md` does not exist and the environment still uses the old package names.

- [ ] **Step 3: Write the consolidated NAS Compose README**

Document these sections in `deploy/compose/README.md` with complete commands and exact paths:

1. `linux/amd64` NAS prerequisites and a reserved host LAN address such as `NAS_LAN_IP=10.0.0.2`.
2. A direct statement that `NAS_LAN_IP` is the NAS host's physical management-LAN address, not Docker's bridge, Tailscale's `100.x` address, or the public WAN address.
3. Local DNS records for `enroll.dgx-forge.lan`, `agents.dgx-forge.lan`, and `registry.dgx-forge.lan` resolving to `10.0.0.2`.
4. How to download `dgx-forge-images.env` and `dgx-forge-images.env.sha256` from the public GitHub Release, run `sha256sum -c dgx-forge-images.env.sha256`, and copy the complete three-reference set into the host-local `.env`. State explicitly that this image-only fragment does not replace the host's site-specific `.env`.
5. The one-time GitHub package settings path for each package and the instruction `Set package visibility to Public`; state that public pulls need no NAS GitHub token.
6. All required `.env` variables grouped as images, NAS paths/networking, hostnames, PKI, Tailscale, and Hermes.
7. All secret files grouped by consumer, with content-format and ownership requirements; state that no secret value belongs in `.env`.
8. Exact step-ca production-overlay bootstrap order and links to `agent-pki.md` and `tailscale.md`.
9. Exact preflight/start commands:

```bash
cd /srv/dgx-forge/repository/deploy/compose
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml pull
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml up -d
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml ps
```

10. Updating by replacing all three digest references as one reviewed release set, pulling, rendering, and recreating; rollback by restoring the previous three-reference set.
11. An explicit evaluation-only section showing all three public `:latest` aliases, stating that production must not use them and that Docker does not continuously update running containers.

- [ ] **Step 4: Align examples and focused runbooks**

In `deploy/compose/.env.example`:

- replace the two old package paths with the approved `dgx-forge-*` paths;
- add `HERMES_AGENT_IMAGE` with a version-and-digest example;
- remove the duplicate `DGX_MANAGEMENT_CIDRS` and `DGX_DIRECT_FABRIC_CIDRS` assignments; and
- keep `NAS_LAN_IP=10.0.0.2` as the host LAN example; and
- mark `HERMES_UID=1100` and `HERMES_GID=1100` as fixed values for official public images rather than freely selectable runtime settings.

Link `deploy/compose/README.md` from the root `README.md`. Update `control-plane-bootstrap.md` to begin with that authoritative entry point. Update `hermes-agent.md` to say the UID/GID is fixed when GitHub Actions builds the published wrapper and that official releases require `1100:1100`. Update `supply-chain.md` with the exact three GHCR names, version-tag-only trigger, public-package visibility step, checksum-protected release asset, evaluation-only `latest` policy, and weekly reviewed Dependabot workflow.

- [ ] **Step 5: Run documentation, Compose, and offline verification**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest \
  tests/runbooks/test_nas_compose.py \
  deploy/compose/tests/test_hermes_agent.py \
  deploy/compose/tests/test_networking.py \
  deploy/compose/tests/test_agent_ingress.py -v
scripts/verify-public-image-inputs
scripts/verify-supply-chain --json
docker compose --env-file deploy/compose/tests/test.env \
  -f deploy/compose/compose.yaml \
  -f deploy/compose/compose.step-ca.yaml config --quiet
```

Expected: tests PASS, both verifiers exit `0`, and Compose exits `0` without a `build` key for any production service.

- [ ] **Step 6: Build all three release targets locally without pushing**

Run:

```bash
docker buildx build --platform linux/amd64 --load \
  --file control/Dockerfile --target api \
  --tag dgx-forge-api:release-dry-run .
docker buildx build --platform linux/amd64 --load \
  --file control/Dockerfile --target worker \
  --tag dgx-forge-worker:release-dry-run .
docker buildx build --platform linux/amd64 --load \
  --file deploy/compose/hermes-agent/Dockerfile --target managed \
  --tag dgx-forge-hermes:release-dry-run deploy/compose/hermes-agent
```

Expected: all three builds exit `0`; no registry login or push occurs.

- [ ] **Step 7: Run the complete repository verification**

Run:

```bash
uvx --from ruff==0.16.1 ruff check .
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest
git diff --check
```

Expected: Ruff reports no errors, pytest reports zero failures, and `git diff --check` exits `0`.

- [ ] **Step 8: Commit the operator handoff**

```bash
git add deploy/compose/README.md deploy/compose/.env.example \
  README.md docs/runbooks/control-plane-bootstrap.md \
  docs/runbooks/hermes-agent.md docs/runbooks/supply-chain.md \
  tests/runbooks/test_nas_compose.py
git commit -m "docs: add pull-only NAS deployment guide"
```

- [ ] **Step 9: Review the final range and confirm no release was pushed**

Run:

```bash
git status --short
git log --oneline --decorate -7
git diff --check HEAD~6..HEAD
```

Expected: the worktree is clean, the six task commits are visible, the diff check exits `0`, and no version tag, GitHub Release, or GHCR publication has been created during implementation.
