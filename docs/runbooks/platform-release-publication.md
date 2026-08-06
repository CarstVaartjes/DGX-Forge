# Publish a platform release

Platform publication is a protected CI operation. It publishes DGX-Forge
control services, host deployment assets, Spark agents, and their signed
platform metadata. Workload packages have an independent publication system;
adding or updating a model stack must not require this workflow.

## Current `0.1.0` state

There is no signed or installable DGX-Forge platform release yet. Three public
`0.1.0` image versions were uploaded manually while evaluating a local incident
workaround. They are disposable candidates only: they were not built by the
protected tag workflow, are not TUF-authorized, and must never be selected by a
NAS or Spark.

The local-upload workaround is withdrawn. Do not promote those bytes by adding
attestations or TUF metadata later; that would make the workstation uploader an
unreviewed build authority. The protected workflow must build every official
artifact itself from the exact merged tag.

## Release input

A stable `vX.Y.Z` tag must point at a reviewed commit containing canonical JSON
at `release/platform/X.Y.Z.input.json`. The document contains every v2 platform
release field except `deployment_bundle`; the workflow derives that descriptor
from the canonical bundle bytes. A release PR must update exact predecessor,
host-updater ABI, database, protocol, image, agent, SBOM, and provenance
bindings. Do not hand-write an OCI digest or reuse a manifest from another
checkout.

For the first real release only, `rollback.predecessors` is an empty array and
installation is valid only on a host with no active generation. Every later
release lists the complete exact predecessor descriptor retained for recovery.
An empty list on an already-installed host cannot bypass this check: planning
rejects a target that does not authorize the active generation.

Both repository variables are default-off and must be `true`:

- `DGX_CONTAINER_RELEASES_ENABLED`
- `DGX_PLATFORM_RELEASES_ENABLED`

The image/bundle build job has `contents: read` and `packages: write`, but no
OIDC permission. A separate `publish-platform-target` job uses the protected
GitHub environment `platform-release`, downloads the immutable build evidence,
and receives only `contents: read` plus `id-token: write`. Configure only these
environment variables for that delegated-authority job:

- `DGX_PLATFORM_AUTHORITY_URL`: HTTPS base URL of the online delegated
  publication service;
- `DGX_PLATFORM_AUTHORITY_AUDIENCE`: exact OIDC audience accepted by that
  service.

GitHub supplies `ACTIONS_ID_TOKEN_REQUEST_URL` and
`ACTIONS_ID_TOKEN_REQUEST_TOKEN` to the job. Do not create a long-lived GitHub
token, TUF root-key secret, or private-key environment variable. The TUF root
private key remains offline. The online service holds only the narrowly
delegated targets/channel authority permitted by repository, workflow,
environment, versioned target prefix, retained-predecessor policy, and channel.

## Deterministic clean-checkout build

CI runs these repo-owned interfaces from the tagged checkout:

```bash
scripts/build-control-deployment-bundle \
  --source-root deploy/compose \
  --output control-deployment.tar
scripts/publish-platform-target describe-bundle \
  --bundle control-deployment.tar \
  --repository ghcr.io/carstvaartjes/dgx-forge-control-deployment \
  > control-deployment-descriptor.json
scripts/build-platform-manifest \
  --input "release/platform/${RELEASE_VERSION}.input.json" \
  --bundle-descriptor control-deployment-descriptor.json \
  --artifact-evidence api-evidence.json \
  --artifact-evidence worker-evidence.json \
  --artifact-evidence hermes-evidence.json \
  --artifact-evidence agent-evidence.json \
  --artifact-evidence supervisor-evidence.json \
  --artifact-evidence tooling-evidence.json \
  --build-digest "sha256:${TAGGED_SOURCE_ARCHIVE_SHA256}" \
  --version "$RELEASE_VERSION" \
  --output platform-release.json
```

The workflow derives each published image reference from the digest emitted by
its own Buildx step, fetches that digest's raw manifest, SBOM and provenance,
and replaces the corresponding release-input locator with canonical evidence.
Thus a reviewed input cannot silently redirect the API, worker, or Hermes
artifact. A native `ubuntu-24.04-arm` job builds the self-contained
`dgx-agent`, validates the committed `dgx-agent-supervisor`, and creates the
deterministic `dgx-forge-tooling` archive. The package-writing job publishes
those payloads to these public OCI repositories:

```text
ghcr.io/carstvaartjes/dgx-forge-agent
ghcr.io/carstvaartjes/dgx-forge-agent-supervisor
ghcr.io/carstvaartjes/dgx-forge-tooling
```

The publisher records each payload's exact name, SHA-256, and size alongside
the OCI manifest, SPDX SBOM, and provenance. The platform builder binds all six
artifact evidence objects plus the digest of the exact tagged source archive.
The builder also rejects noncanonical input, an input-supplied bundle
descriptor, duplicate evidence locators, version mismatch, an existing output,
or a release that fails the shipped v2 parser and schema.

Immediately before any registry mutation, the package-writing job proves that
the version is absent from all six repositories: API, worker, Hermes, agent,
agent supervisor, and tooling. A partial or pre-existing version fails closed;
the job never overwrites it.

The API, worker, and Hermes OCI indexes contain `linux/amd64` and
`linux/arm64`. This does not yet make the entire third-party Compose graph a
supported ARM64 control-host deployment. Treat ARM64 Docker hosts as
provisional until every pinned upstream service and the complete deployment
gate pass there. Spark payloads are native `linux/arm64` artifacts.

At one-time Spark installation, set the canonical site document's
`registry_origin` to `https://ghcr.io` and its `repository` to
`carstvaartjes/dgx-forge-agent`. The installed agent transport deliberately
pulls platform agent updates only from that pinned repository; an operation
cannot redirect it. Supervisor and tooling locators remain manifest-bound
release evidence. This policy is identical for every Spark and contains no
node name, IP address, or fleet-size assumption.

The unprivileged build job pins ORAS setup, resolves its absolute executable,
and publishes only the bundle:

```bash
export DGX_PLATFORM_ORAS_BIN=/absolute/path/to/oras
scripts/publish-platform-target publish-bundle \
  --manifest platform-release.json \
  --bundle control-deployment.tar > bundle-publication.json
```

Only after that artifact is uploaded does the OIDC job invoke the authority:

```bash
export DGX_PLATFORM_TUF_PUBLISHER_BIN="$PWD/scripts/platform-release-authority"
export DGX_PLATFORM_CHANNEL_PUBLISHER_BIN="$PWD/scripts/platform-release-authority"
scripts/publish-platform-target publish-authority \
  --manifest platform-release.json \
  --bundle control-deployment.tar \
  --bundle-publication bundle-publication.json \
  --channel stable > authority-publication.json
```

Each executable is opened without following links, must be a single-link
regular executable that is not group/world-writable, and is run through its
validated descriptor. Subprocesses receive only their exact proxy/TLS,
registry, or OIDC inputs plus a fixed path/locale; output, time, and process
groups are bounded.

## Publication order and retry

The only valid order is:

1. upload the empty OCI config blob, canonical bundle layer, and OCI manifest
   by digest;
2. publish canonical release bytes as
   `platform/releases/X.Y.Z/<manifest-sha256>.json` and retain every exact
   supported predecessor target;
3. receive and validate the new positive TUF targets version; and
4. publish the canonical `stable` discovery document with an ETag compare-and-
   swap.

The immutable target is append-only. An exact target replay returns the same
receipt; a different document under the same name is rejected. A channel retry
with byte-identical content is accepted. Any different update must advance
`tuf_targets_version` strictly; equal or lower versions are rejected. OCI and
target steps are safe to retry after interruption. The channel is updated last,
is discovery-only, and is never an installation or rollback authority.

The workflow uploads and attaches the canonical image manifests, SBOMs,
provenance documents and artifact-evidence records, the deployment descriptor,
platform manifest, bundle/authority receipts, and the deterministic installable
host-updater archive plus its checksum. Keep them together. The receipts bind
target name/SHA-256, TUF targets version, bundle descriptor, channel, and
channel-document SHA-256.

A third minimal protected job downloads the already-built host-updater archive
and signs GitHub/Sigstore build provenance with `actions/attest`. It has
`contents: read`, `id-token: write`, and `attestations: write`, but no package,
release, registry, or TUF publication permission. Operators verify this
attestation with `gh attestation verify` before the first root installation.

## First-release operator sequence

Use the authoritative [`v0.1.0` release checklist](v0.1.0-release-checklist.md)
for ordering, owners, commands, evidence, and stop conditions. It also records
two current implementation blockers: the delegated authority server is not
shipped, and the staged physical-candidate/exporter path needed to close all six
physical gates does not exist yet.

The authority contract below remains the client/server protocol source. Follow
[Delegated platform authority deployment](platform-authority-deployment.md) for
the deployment gate and [Physical release acceptance](physical-release-acceptance.md)
for the six-gate evidence boundary. Do not delete package versions, create the
tag, enable publication, or improvise evidence while either runbook is blocked.

## Delegated authority HTTP contract

`scripts/platform-release-authority` obtains a short-lived GitHub Actions OIDC
token for the configured audience and calls only the HTTPS endpoints below.
Responses are canonical JSON and bounded to 64 KiB.

Immutable target publication:

```text
PUT /v1/platform/targets/<percent-encoded-target-name>
Authorization: Bearer <OIDC>
Idempotency-Key: <target-sha256>
If-None-Match: *
Content-Type: application/json
```

The canonical request contains `schema_version`, `target_name`,
`target_sha256`, base64 canonical manifest bytes, and the ordered
`retained_targets`. Success is `200` or `201` with the exact values and a
positive `targets_version`. On `409` or `412`, the client performs `GET` on the
same URL and accepts only an exact receipt; no overwrite is attempted.

Discovery-channel publication first reads
`GET /v1/platform/channels/stable`. Creation uses `If-None-Match: *`; replacement
uses `If-Match: <current-etag>`. The body is the canonical discovery document.
Only a byte-identical replay or a strictly greater TUF targets version is
accepted. A missing ETag, stale compare-and-swap, alias named `latest`, or
mismatched receipt fails the workflow.

## Authority implementation and incident response

The authority implementation must validate the GitHub issuer, audience,
repository, workflow ref/SHA, protected environment, tag, target prefix,
manifest canonicality, retained-target existence, and monotonic channel rule
before signing. It must publish consistent-snapshot target metadata atomically
and keep supported predecessors fetchable. This repository owns the client
contract; the authority may be hosted on any hardened Docker-capable machine.

If publication stops before the channel step, correct the infrastructure issue
and rerun the same tag: content-addressed OCI objects and the exact target replay
are idempotent. If the target receipt differs, the channel advanced elsewhere,
or signing policy is uncertain, stop. Preserve the receipts and audit log; do
not delete metadata, repoint the channel manually, rotate root trust online, or
publish a replacement under the same immutable name.
