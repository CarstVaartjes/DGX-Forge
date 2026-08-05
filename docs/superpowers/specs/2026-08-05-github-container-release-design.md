# GitHub container release design

Date: 2026-08-05

Status: architecture approved; implementation blocked pending written-spec review

## Purpose

Publish the three DGX-Forge-owned runtime images from GitHub Actions so a NAS
deploys immutable release artifacts instead of compiling application code.
Publishing occurs only for explicit stable version tags. The images are public
because the repository and its runtime code are public; deployment credentials
and site-specific configuration remain outside every image.

This change extends the existing supply-chain and Compose design. It does not
combine services into one container, expose new ports, or change the separation
between the request-facing API, the private worker, and Hermes Agent.

## Decisions and alternatives

Three workflow shapes were considered:

1. Add a tag-gated publication job to the existing CI workflow and make it
   depend on the existing lint, generated-client, and test jobs.
2. Add an independent release workflow that repeats the validation suite before
   publishing.
3. Start a second workflow through `workflow_run` after CI completes and recover
   the originating tag and commit from that event.

The first option is selected. It provides one release gate without duplicating
CI or introducing cross-workflow tag handling. Ordinary branches and untagged
pushes continue to run CI but cannot obtain package-write permission or publish
an image.

## Published images

The workflow publishes these public GHCR packages:

```text
ghcr.io/carstvaartjes/dgx-forge-api
ghcr.io/carstvaartjes/dgx-forge-worker
ghcr.io/carstvaartjes/dgx-forge-hermes
```

The package names are public artifact names. Compose retains the internal
service and DNS names `control-api`, `control-worker`, and `hermes-agent` because
those names describe runtime roles and are already part of internal routing.

The API and worker are separate targets of `control/Dockerfile`, built from the
same commit and dependency set:

- `dgx-forge-api` serves the authenticated control API and compiled web UI;
- `dgx-forge-worker` privately claims durable work and reconciles fleet and
  LiteLLM routing state; and
- `dgx-forge-hermes` derives from the locked official Hermes image and adds only
  DGX-Forge's read-only-root-compatible identity and secret-loading wrapper.

All other Compose services continue to pull their digest-pinned upstream
images. DGX-Forge does not republish PostgreSQL, Caddy, LiteLLM, Tailscale,
Prometheus, Grafana, Registry, or step-ca.

## Trigger and version contract

Image publication is allowed only for a Git tag matching this stable SemVer
grammar:

```text
^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$
```

Examples include `v0.1.0` and `v1.2.3`. Branch pushes, pull requests, malformed
tags, prerelease tags, and build-metadata tags never publish. GitHub's event
filter may select the broader `v*` family, but a release job must validate the
exact grammar before acquiring write permissions or building release output.

For `v1.2.3`, each successful package receives:

```text
1.2.3
sha-<full-40-character-commit-id>
latest
```

`latest` moves only after a valid stable version tag passes the release gate.
Production Compose configuration never relies on `latest`; it uses the
workflow-reported `image@sha256:...` reference. Tags are conveniences for
discovery, while the digest is the deployment identity.

The initial platform is `linux/amd64`, matching the current UGREEN NAS and DGX
Spark deployment. Multi-architecture publication is out of scope until every
locked base, including the official Hermes base, is verified for the additional
platform and its runtime harness passes there.

## Workflow architecture

The existing CI jobs remain unchanged for normal pushes and pull requests. A
read-only release-metadata job checks that:

- the event is a tag push;
- the tag satisfies the exact stable SemVer grammar;
- the checked-out commit equals the tag target.

A separate publication job depends on the release-metadata job and every
existing CI gate. Only that job receives package-write permission. It reruns the
offline supply-chain verifier, logs in to GHCR with the job-scoped
`GITHUB_TOKEN`, builds the API, worker, and Hermes targets from the tagged
commit, and pushes their version, commit, and `latest` tags. GitHub Actions and
Docker actions are pinned to reviewed commit SHAs. BuildKit produces an OCI SBOM
and provenance for every published image.

The workflow uses the minimum relevant permissions:

```yaml
contents: read
packages: write
```

Any additional permission required by the selected GitHub-native attestation
mechanism is granted only to the release job. Pull-request jobs, ordinary push
jobs, and test jobs retain read-only repository permissions.

No long-lived registry credential is introduced. `GITHUB_TOKEN` is used only
by the registry login step and is never passed to Docker as a build argument,
secret mount, environment layer, label, or build output.

## Publication completeness and failure handling

GHCR cannot atomically commit three independent packages. Therefore the
workflow treats the release summary, rather than the presence of an individual
tag, as the definition of a complete release.

The workflow builds and verifies all three targets before reporting a release.
After every push succeeds, it resolves the registry digest for each package and
writes one summary containing exactly these three immutable references:

```text
CONTROL_API_IMAGE=ghcr.io/carstvaartjes/dgx-forge-api:1.2.3@sha256:...
CONTROL_WORKER_IMAGE=ghcr.io/carstvaartjes/dgx-forge-worker:1.2.3@sha256:...
HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/dgx-forge-hermes:1.2.3@sha256:...
```

If a build, verification, login, push, or digest-resolution step fails, the job
fails and emits no complete deployment block. A retry for the same immutable
tag must reproduce the same tagged commit. A partial registry artifact may
exist after an interrupted multi-package push, but documentation explicitly
forbids deployment unless the successful run summary contains the complete
three-image set.

The workflow must not silently overwrite a version tag that Git has moved to a
different commit. Operational documentation treats published version tags as
immutable and requires a new version after any release error that changes
source inputs.

## Compose integration

Compose continues to receive release image references through environment
interpolation. `CONTROL_API_IMAGE` and `CONTROL_WORKER_IMAGE` remain required.
The locally built `local/hermes-agent:managed` reference and its Compose
`build:` block are replaced by required `HERMES_AGENT_IMAGE` interpolation.
The upstream `HERMES_IMAGE` remains only a build-time lock used by the Hermes
release target.

The NAS deployment flow becomes:

```text
successful version-tag workflow
  -> copy the three digest-pinned references into the NAS .env
  -> docker compose pull
  -> docker compose config --quiet
  -> docker compose up -d
```

The `.env.example`, image lock manifest, supply-chain runbook, Hermes runbook,
and consolidated Compose deployment documentation must use the published names
and explain the distinction between `HERMES_AGENT_IMAGE` and the locked
upstream Hermes base.

The first workflow publication may create private GHCR packages because package
visibility is a registry setting. The maintainer performs a documented one-time
change of each package to public. Subsequent NAS pulls require no GitHub token.
The workflow must not store a personal access token merely to automate this
one-time administrative action.

## Confidentiality and secret handling

Public images intentionally disclose DGX-Forge application code, dependency
versions, static web assets, runtime entrypoints, and non-secret defaults. They
must not contain:

- NAS, Spark, LAN, or tailnet addresses;
- PostgreSQL, LiteLLM, Grafana, Hermes, or worker credentials;
- Tailscale OAuth credentials;
- CA, SSH, Git signing, or TLS private keys;
- repository or package tokens; or
- generated runtime state and user workspaces.

Those values remain runtime environment values or Compose secret files on the
NAS. Dockerfiles continue using explicit `COPY` sources. A root `.dockerignore`
must exclude local environments, worktrees, VCS metadata, caches, keys,
certificates, state, and secret directories from the control build context.
The Hermes build context remains its narrow component directory.

The repository's checked-in examples and tests may contain unmistakable dummy
values, but release verification rejects committed private-key material and
recognized live-token formats. Image inspection tests verify that representative
secret markers and repository metadata do not appear in the resulting images.

## Verification and acceptance

Automated tests must prove that:

- only exact stable version tags make the release condition true;
- the release job depends on every required CI gate;
- only the release job receives package-write permission;
- all actions are commit-SHA pinned;
- all three exact public package names are produced;
- the API and worker use their correct Dockerfile targets;
- Hermes uses the hardened derived Dockerfile and not the upstream image
  directly in production Compose;
- release tags include version, commit, and stable `latest` metadata;
- builds target only `linux/amd64`;
- SBOM and provenance generation are enabled;
- Compose requires all three digest-pinned image variables and contains no
  production `build:` directive; and
- ordinary pushes and pull requests cannot publish.

Before merging, render the production Compose configuration with representative
digest-pinned values, run the supply-chain verifier, run the focused Compose and
workflow tests, and build all three image targets. A release dry run must
exercise metadata generation without pushing packages.

## Operational result

After implementation, creating and pushing an immutable tag is the only image
release operation:

```bash
git tag -s v1.2.3 -m "DGX-Forge v1.2.3"
git push origin v1.2.3
```

Once CI succeeds, the maintainer copies the three digest-pinned references from
the workflow summary to the NAS `.env`. The NAS does not clone build
dependencies, run application builds, or authenticate to GHCR. Runtime
configuration, secrets, persistent volumes, LAN binding, Tailscale ingress, and
mTLS remain entirely under Compose and NAS administration.
