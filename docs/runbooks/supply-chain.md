# Verify the platform supply chain

Standard service images are fixed by version and OCI index digest in
`deploy/compose/images.lock.json`; Compose uses those exact references as its
defaults. The custom control image is a release artifact and must be supplied
through `CONTROL_API_IMAGE`, `CONTROL_WORKER_IMAGE`, and `HERMES_AGENT_IMAGE`
with one complete set of registry digests. The three DGX-Forge packages are
`ghcr.io/carstvaartjes/dgx-forge-api`,
`ghcr.io/carstvaartjes/dgx-forge-worker`, and
`ghcr.io/carstvaartjes/dgx-forge-hermes`. Build the `api` and `worker`
Dockerfile targets from the same release commit; the worker target deliberately
contains neither Git nor OpenSSH. The Node and Python build bases are separately
digest-pinned in the lock.

## Future image releases

No images are currently being published. The repository variable
`DGX_CONTAINER_RELEASES_ENABLED` remains unset/default-off until the whole
repository is release-ready. Setting it to `true` is a deliberate maintainer
enablement action. Once enabled, only an exact stable SemVer version-tag push
(`vX.Y.Z`) can publish the three packages; branches, pull requests, malformed
tags, and Dependabot cannot publish.

For each package's initial publication, a maintainer must open its GitHub
package page and choose **Package settings** → **Danger Zone** → **Change
visibility** → **Set package visibility to Public**. Public NAS pulls then need
no GitHub token. A successful three-image publication creates the public
release assets `dgx-forge-images.env` and
`dgx-forge-images.env.sha256`; NAS operators verify the checksum and use all
three version-and-digest assignments as one release set. See the authoritative
[NAS pull-only Compose deployment guide](../../deploy/compose/README.md).

The workflow may update each package's `latest` tag after a successful stable
version release, but `latest` is evaluation-only and never a production image
input. Production uses only the release asset's immutable digests. Docker does
not update running containers merely because a tag moves.

Dependabot checks Docker build inputs, Docker Compose files, and GitHub Actions
weekly and opens ordinary reviewed pull requests. It does not auto-merge, tag,
create a release, or publish an image; maintainers review an accepted update
before making a later deliberate version-tag release.

Run the offline gate before building or deploying:

```bash
scripts/verify-supply-chain --json
```

The verifier checks image defaults, both dependency lockfiles, deterministic
SPDX 2.3 documents, the rebuilt `dgx-agent-protocol` wheel hash,
Dockerfile/Compose inputs, the LiteLLM cosign public key, and the
content-addressed evidence manifest. Normal verification performs no network
access. Regeneration is an explicit reviewed operation:

```bash
scripts/verify-supply-chain --generate --json
```

At image publication, build from the repository root so the control image can
build and install the standalone protocol wheel:

```bash
docker build --file control/Dockerfile \
  --target api \
  --build-arg NODE_IMAGE="$(jq -r '.build_bases.node' deploy/compose/images.lock.json)" \
  --build-arg PYTHON_IMAGE="$(jq -r '.build_bases.python' deploy/compose/images.lock.json)" \
  --tag dgx-control-api:local .
docker build --file control/Dockerfile \
  --target worker \
  --build-arg NODE_IMAGE="$(jq -r '.build_bases.node' deploy/compose/images.lock.json)" \
  --build-arg PYTHON_IMAGE="$(jq -r '.build_bases.python' deploy/compose/images.lock.json)" \
  --tag dgx-control-worker:local .
```

Publish both immutable control images, record their registry digests as
`CONTROL_API_IMAGE` and `CONTROL_WORKER_IMAGE`, generate image/filesystem SBOMs
with Syft for both targets, scan them with the
release-approved scanner, and sign each with Cosign. LiteLLM signatures use the
checked-in key copied from immutable upstream commit
`0112e53046018d726492c814b3644b7d376029d0`; verify the locked digest, never a
mutable tag. Store scan/signature attestations with the release evidence.
