# Deploy the delegated platform authority

This runbook defines the deployment gate for the online service that accepts
short-lived GitHub Actions OIDC identities and publishes narrowly delegated
DGX-Forge platform TUF targets and the `stable` discovery channel.

## Current implementation status

**Blocked:** this repository ships the client and protocol, but it does not
ship an authority server, server container image, Compose service, deployment
bundle, or production TUF backend. No authority is currently deployed or
configured for this repository.

The shipped client surfaces are:

- `scripts/platform-release-authority`;
- `src/spark_profiles/platform_authority_client.py`; and
- `scripts/publish-platform-target publish-authority`.

They cannot sign metadata, hold a delegated key, or replace the missing server.
Do not point them at an improvised endpoint or place a TUF private key in
GitHub Actions, the repository, a developer workstation, or the normal control
plane.

## Required server boundary

The server must be implemented and reviewed before deployment. It must:

- expose only HTTPS `GET`/`PUT` operations for
  `/v1/platform/targets/<encoded-target>` and
  `/v1/platform/channels/stable`;
- validate the GitHub OIDC issuer, exact audience, repository, workflow file,
  workflow SHA/ref, protected `platform-release` environment, stable tag, and
  non-reusable token lifetime before reading a publication body;
- authorize only canonical `platform/releases/X.Y.Z/<sha256>.json` targets and
  the discovery-only `stable` channel;
- verify canonical manifest bytes, target-name digest binding, retained target
  existence, predecessor policy, and all request/response size bounds;
- implement `If-None-Match: *` for immutable targets and ETag compare-and-swap
  for the channel;
- return byte-identical idempotent receipts and reject overwrites, lower/equal
  channel versions with different bytes, `latest`, and unrecognized claims;
- publish consistent-snapshot TUF metadata atomically and keep every supported
  predecessor fetchable;
- hold only a narrowly delegated online targets/channel key; the offline root
  private key must never enter the service;
- run as an unprivileged, read-only-root container with a dedicated persistent
  state volume, bounded request bodies/timeouts, no Docker socket, and no
  control-plane database or Spark credentials;
- emit append-only sanitized audit events binding OIDC subject/claims, request
  digest, target/channel identity, TUF version, decision, and receipt digest;
  and
- support authenticated encrypted backup and tested replacement-host recovery
  of delegated metadata, keys, version state, and audit continuity.

The current GitHub-hosted workflow has no private-network connector. Therefore
the configured HTTPS endpoint must be reachable from GitHub-hosted runners, or
a separately reviewed workflow change must add a constrained private-network
transport. Do not expose the NAS control UI, Docker API, TUF root, or any other
service alongside this endpoint.

## Implementation acceptance

The authority implementation needs its own reviewed PR and supply-chain
evidence. Acceptance must prove:

1. a valid protected-environment OIDC identity can create one exact target and
   advance `stable` after target publication;
2. replaying the same target and channel returns the same receipts;
3. wrong issuer, audience, repository, workflow, environment, ref, tag, or
   expired token is rejected before mutation;
4. target overwrite, unknown predecessor, invalid manifest, wrong digest,
   missing CAS, stale ETag, equal/lower conflicting TUF version, and `latest`
   are rejected;
5. interruption at every persistence boundary recovers without a partial
   target/channel state;
6. replacement-host restore preserves key identity, TUF versions, targets,
   channel CAS, and audit continuity; and
7. the container/image contains no offline root key or unrelated platform
   secret.

Evidence must include the exact server source commit, image digest, SBOM,
provenance, configuration digest, TLS identity, delegated public key/role,
offline delegation ceremony record, negative-test report, backup/restore
report, and sanitized audit/receipt digest. Until these exist, the release
checklist stays blocked.

## Deploy after implementation

The final implementation must supply its own digest-pinned Compose or
orchestrator manifest. Deploy only that reviewed artifact on a hardened
Docker-capable Linux host. Record rather than assume:

- the immutable image digest and configuration digest;
- the public HTTPS origin and certificate chain;
- the exact OIDC audience;
- the delegated role/key ID and offline delegation metadata version;
- root-owned encrypted backup locations and recovery owner; and
- firewall rules exposing only the authority HTTPS port.

After start, run the implementation's health, negative-authentication, target
idempotency, channel-CAS, backup, and replacement-host acceptance. There is no
generic command in this repository for those server-side operations yet; the
server PR must add it. A `200` health response alone is insufficient.

## Configure GitHub only after acceptance

Set non-secret endpoint metadata on the protected environment:

```bash
read -r -p "Accepted authority HTTPS origin: " authority_url
read -r -p "Accepted exact OIDC audience: " authority_audience
case "$authority_url" in https://*) ;; *) exit 64 ;; esac
test -n "$authority_audience"
gh variable set DGX_PLATFORM_AUTHORITY_URL \
  --env platform-release --body "$authority_url"
gh variable set DGX_PLATFORM_AUTHORITY_AUDIENCE \
  --env platform-release --body "$authority_audience"
gh variable list --env platform-release
```

In repository **Settings → Environments → platform-release**, require the
reviewed release approver and prevent self-review. Limit deployment branches to
the reviewed stable-tag policy. The environment contains no private TUF key,
PAT, registry password, or long-lived OIDC token.

Only after the environment policy and authority acceptance are recorded may
the repository maintainer enable publication:

```bash
gh variable set DGX_CONTAINER_RELEASES_ENABLED --body true
gh variable set DGX_PLATFORM_RELEASES_ENABLED --body true
gh variable list
```

If the URL/audience is changed, the delegated key is rotated, restore changes
the authority identity, or negative acceptance fails, set both release
variables back to `false` and do not tag:

```bash
gh variable set DGX_CONTAINER_RELEASES_ENABLED --body false
gh variable set DGX_PLATFORM_RELEASES_ENABLED --body false
```

## Release-time verification

The protected workflow obtains its own short-lived OIDC token. Operators never
copy that token into a terminal. Require the release evidence to show:

- the exact immutable target name and SHA-256;
- a positive, strictly advancing TUF targets version;
- retained predecessor targets;
- the channel document SHA-256 and matching target;
- target-before-channel ordering; and
- authority audit events whose request and receipt digests match the GitHub
  release assets.

On ambiguity, leave `stable` unchanged, preserve server and workflow evidence,
disable the release variables, and stop. Never retry with a local signer or
online root key.
