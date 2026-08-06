# DGX-Forge platform release update

This runbook updates DGX-Forge itself: the Docker control services on a
Docker-capable control host and the DGX-Forge agents on any number of enrolled
DGX Sparks. It does not update model packages, DGX OS, firmware, the kernel,
NVIDIA drivers, or CUDA. Workload packages have an independent release cadence;
see [Runtime releases](runtime-release.md).

The normal Spark path is the outbound, mutually authenticated agent channel.
It is not SSH. An administrator can still SSH into a Spark, but SSH is reserved
for bootstrap and recovery and is never part of a successful rollout. See
[SSH recovery](ssh-recovery.md) before an incident, not during one.

## Safety model

A platform update has two deliberately separate phases:

1. A root-run, offline control-host operation selects a new immutable
   generation from a TUF-authorized release and digest-pinned OCI artifacts.
2. The newly selected NAS/control version reports version skew. An administrator
   previews and explicitly confirms a signed Spark update plan. The worker then
   advances one canary, its soak period, and batches of one by default.

Updating the control host never automatically updates a Spark. Merely viewing
the NAS-newer prompt also never mutates the fleet. The plan pins the exact
release, fleet observations, topology, agent inputs, canary, batches, and
rollback slots. A changed input makes the digest stale and requires a new plan.

Stop on any of these conditions:

- a TUF target, OCI digest, SBOM, provenance record, or release manifest cannot
  be verified exactly;
- the backup, free-space, compatibility, topology, workload-availability, or
  route-withdrawal gate fails;
- the candidate control API or worker does not become ready;
- a Spark is newer than the control release, outside its protocol range, or
  lacks both `agent.update` and `agent.rollback`;
- the canary fails, rolls back, or does not reconnect with its signed running
  identity; or
- reported state disagrees with the expected active A/B slot or build digest.

Do not continue by changing a tag, editing a plan, suppressing a health check,
or running an update command over SSH.

## Install the host updater once

The first control-host bootstrap installs the release's
`dgx-forge-host-updater.tar`; later platform generations do not execute updater
code from a Git checkout. Download the tar and checksum from the same immutable
GitHub release, then verify its signed GitHub build provenance and digest before
extracting it:

```bash
gh attestation verify dgx-forge-host-updater.tar \
  --repo REPLACE_OWNER/REPLACE_REPOSITORY
sha256sum --check dgx-forge-host-updater.tar.sha256
install_root=/opt/dgx-forge/host-updater/0.1.0
sudo python3 -m venv "$install_root"
staging=$(mktemp -d)
tar --extract --file dgx-forge-host-updater.tar --directory "$staging"
sudo "$install_root/bin/python" -m pip install \
  --find-links "$staging" "$staging"/dgx_control-0.1.0-py3-none-any.whl
sudo install -d -m 0755 /usr/local/bin
sudo ln -sfn "$install_root/bin/dgx-control-offline" \
  /usr/local/bin/dgx-control-offline
rm -rf -- "$staging"
sudo dgx-control-offline --help
```

Perform this bootstrap from a trusted administrator session. Pin the release
owner/repository in local operating procedure and retain the verified tar for
recovery. Do not install an unattested artifact, use a source checkout as the
root entry point, or copy a newer updater onto the host merely because its tag
is newer. A future updater replacement follows the same signed release and
reviewed maintenance process.

## Prepare the release and recovery inputs

Platform publication is a CI release-authority workflow, not a NAS operation.
It builds the canonical deployment bundle and v2 platform manifest, publishes
the bundle by immutable OCI digest, adds the digest-derived manifest name to
delegated TUF targets while retaining supported predecessors, and only then
updates the stable discovery channel. The channel is never an install target.
The offline TUF root private key is never present in CI, on the control host, or
on a Spark; CI receives only the narrowly delegated signing authority required
by the publication policy.

The guarded `platform-release` GitHub environment runs the equivalent of:

```bash
scripts/build-control-deployment-bundle \
  --source-root deploy/compose \
  --output dist/control-deployment.tar
scripts/publish-platform-target describe-bundle \
  --bundle dist/control-deployment.tar \
  --repository ghcr.io/REPLACE_ORG/dgx-forge/control-deployment \
  > dist/control-deployment-descriptor.json
scripts/build-platform-manifest \
  --input release/platform/REPLACE_VERSION.input.json \
  --bundle-descriptor dist/control-deployment-descriptor.json \
  --version REPLACE_VERSION \
  --output dist/platform-release.json
scripts/publish-platform-target publish-bundle \
  --manifest dist/platform-release.json \
  --bundle dist/control-deployment.tar
# A separate protected OIDC job publishes the immutable target and channel.
```

Publication requires both `DGX_CONTAINER_RELEASES_ENABLED` and
`DGX_PLATFORM_RELEASES_ENABLED`. The workflow supplies absolute trusted-tool
paths in `DGX_PLATFORM_ORAS_BIN`, `DGX_PLATFORM_TUF_PUBLISHER_BIN`, and
`DGX_PLATFORM_CHANNEL_PUBLISHER_BIN`. The delegated publisher authenticates
with the bounded `DGX_PLATFORM_AUTHORITY_URL`,
`DGX_PLATFORM_AUTHORITY_AUDIENCE`, and GitHub Actions OIDC request variables;
do not replace these with a TUF private-key environment variable.

Use a dedicated maintenance window. Keep the current and candidate versioned
platform targets, their TUF metadata, and the exact predecessor deployment
bundle available. Record their SHA-256 digests outside the control host. The
release must authorize the exact predecessor needed for rollback; `latest` is
not a rollback target.

Confirm that the encrypted database backup recipients file is current and
root-owned, then run the normal backup/restore readiness checks from
[Control-plane recovery](control-plane-recovery.md). Retain enough free space
for the candidate generation, the predecessor generation, the encrypted
backup, and transient OCI acquisition.

For disconnected installation, move only the reviewed TUF metadata and exact
content-addressed OCI objects on the approved offline medium. Verify the medium
digest before copying it to the control host. Do not replace digest references
with tags or use the medium as a TUF trust root. Root rotation still starts from
the already trusted root.

The apply command requires these root-controlled inputs:

```bash
export DGX_PLATFORM_TUF_ROOT=/srv/dgx-forge/trust/platform/root.json
export DGX_PLATFORM_TUF_METADATA_URL=https://updates.example.invalid/platform/metadata/
export DGX_PLATFORM_TUF_TARGET_URL=https://updates.example.invalid/platform/targets/
export DGX_BACKUP_RECIPIENTS_FILE=/srv/dgx-forge/secrets/backup-recipients.txt
target_name=platform/releases/2.0.0/REPLACE_MANIFEST_SHA256.json
```

The URLs may point at the approved local/offline mirror. They must not point at
mutable, unauthenticated files.

## Plan and update the control host

First load and validate the release without mutation:

```bash
sudo dgx-control-offline \
  --state-path /srv/dgx-forge/control-host \
  upgrade --target-name "$target_name"
```

Review the candidate version/build/release digests, exact API and worker image
digests, migration revision, required bytes, predecessor generation, and plan
digest. A dry run must not pull, back up, migrate, stop, or start anything.

Apply the same exact target. Do not stop services with a Compose file from a
checkout: the host updater owns the fixed stop/start sequence and uses only the
selected generation's verified deployment bundle.

```bash
sudo dgx-control-offline \
  --state-path /srv/dgx-forge/control-host \
  upgrade --target-name "$target_name" --apply
```

The updater holds the single host-operation lock, verifies TUF and OCI inputs,
creates a restricted generation, takes the encrypted backup before migration,
starts the API, checks readiness through Caddy, selects the generation, and
starts the worker. Preserve the returned generation IDs and evidence digests.

If the command exits with recovery-required status, do not retry blindly. First
inspect the journaled recovery plan, then apply that exact plan:

```bash
sudo dgx-control-offline \
  --state-path /srv/dgx-forge/control-host \
  recover
sudo dgx-control-offline \
  --state-path /srv/dgx-forge/control-host \
  recover --apply
```

Keep the worker stopped while recovery is required. An ambiguous database
migration is never automatically reversed. If readiness or worker activation
fails before an unsafe migration boundary, the updater reselects the recorded
predecessor.

For an operator-requested compatible rollback, plan the exact recorded
predecessor, and then apply it. The updater performs the service transition; do
not run a separate mutable Compose command:

```bash
sudo dgx-control-offline \
  --state-path /srv/dgx-forge/control-host \
  rollback --generation REPLACE_RECORDED_PREDECESSOR
sudo dgx-control-offline \
  --state-path /srv/dgx-forge/control-host \
  rollback --generation REPLACE_RECORDED_PREDECESSOR --apply
```

Never select an arbitrary generation. A contract migration or ambiguous backup
state may require restore/roll-forward instead of image rollback; use
[Control-plane recovery](control-plane-recovery.md).

## Review the NAS-newer fleet prompt

After the control API and worker are healthy, view skew from the web Admin →
Updates page or the CLI:

```bash
sparkctl admin updates skew --json
```

The prompt is expected only when a Spark's semantic platform version or exact
build digest differs from the active control generation. Review:

- every affected, incompatible, retired, and offline-pending node;
- the exact `target.release`, raw `target_sha256`, and positive
  `tuf_targets_version` returned by skew;
- current and target versions, build digests, protocol ranges, and A/B slots;
- the canary and later batches;
- active workloads and routes affected by each batch;
- soak duration and workload-availability gates; and
- the exact skew digest.

Do not apply from the prompt. An offline old node remains pending. An
incompatible node blocks mutation until the compatibility problem is resolved;
it is not silently omitted.

## Plan, confirm, and monitor Spark fan-out

Create a fresh preview from the versioned release name shown by skew:

```bash
sparkctl admin updates plan \
  --release "$target_name" \
  --json
```

The default is one deterministic canary, a soak, and later batches of one.
Select a preferred canary only when its placement and active workloads make it
the safest representative. There is no compiled fleet-size limit.

Confirm the exact plan digest in the web UI or CLI. This is the mutation point:

```bash
sparkctl admin updates apply \
  --plan-digest sha256:REPLACE_PLAN_SHA256 \
  --json
```

The API issues a short-lived administrator action grant and the isolated signer
binds each `agent.update` authorization to the rollout, job, exact node set,
release, operation ID, and lease fence. Agents acquire only their
architecture-specific TUF-authorized artifact, write the inactive slot, verify
it, and ask the stable supervisor to activate it. Route withdrawal precedes
mutation; route publication follows authenticated reconnect, readiness, and
self-test.

Monitor without starting a second rollout:

```bash
sparkctl admin updates status REPLACE_ROLLOUT_UUID --json
```

Check the canary's reported running version, build digest, protocol, supervisor
generation, and active slot. A downloaded or desired version is not acceptance
evidence. During the soak, verify the models and routes that were serving before
the update, not merely the agent health endpoint.

## Failure, rollback, resume, and offline nodes

The first failure pauses fan-out. Do not approve resume before finding the
cause. If activation times out or crashes, the supervisor returns to the last
verified slot. Confirm the rollback slot and running digest in the rollout
detail. If both slots are corrupt, the node is a recovery case and must remain
withdrawn.

The paused rollout's recovery worker creates the signed `agent.rollback`
operations for nodes already changed; there is no manual rollback button or
public rollback endpoint. Keep refreshing status while the state is paused. Do
not try to manufacture a rollback job. Only after every required rollback is
observed does the rollout enter `waiting-for-approval` and the web Admin →
Updates page exposes **Approve rollout resume**. An administrator—not an
operator—may then approve resume with a bounded audit reason. Approval creates
a fresh plan/authorization boundary; it does not reuse stale signatures. The
CLI remains suitable for skew, plan, apply, and status; use the web workflow for
the administrator-only approval until an equivalent CLI command is present.

An offline node remains `offline-pending` and is not counted as successfully
updated. When it reconnects, re-run skew and create a fresh plan. Never bypass
the queue with SSH. If the agent cannot establish outbound mTLS, use
[SSH recovery](ssh-recovery.md) only to restore the installed agent/supervisor
and its trust configuration, then return to the standard signed update path.

## Final verification and evidence

Before declaring the rollout complete, require all intended online nodes to
report the exact target version/build/protocol and a stable active slot. Verify
the pre-existing model profiles, LiteLLM/Caddy routes, workload availability,
and audit trail. Confirm that no standard-path command used SSH.

Run the deterministic acceptance gate during development:

```bash
scripts/accept-platform-update --json
scripts/accept-platform-update \
  --output inventory/reports/platform-update.json
```

This report is always `evidence_kind: simulated`. It proves contract and failure
injection coverage, not a physical update. It deliberately leaves these release
gates open:

- `signed-platform-update-manifest-evidence`;
- `physical-control-host-update-recovery`; and
- `physical-spark-canary-rollback`.

The release verifier accepts those gates only from a canonical,
content-addressed `platform-update.json` produced by an approved physical
acceptance workflow with all three independent evidence digests. That exporter
is not shipped yet; follow [Physical release acceptance](physical-release-acceptance.md)
for its implementation requirements and all six first-release gates. Each
physical artifact is a canonical Ed25519 envelope signed by the separately
controlled physical-acceptance authority. The signed body binds one candidate, exact
release digest, UUID run ID, evidence type, observation time, and protected
detail digest. All three envelopes and the parent report must name the same
candidate/release/run; mixing envelopes from another candidate, release, or run
stays blocked. The verifier is intentionally stateless: the same complete
signed acceptance remains reusable for that exact release approval, so the
release authority—not this verifier—owns run-ID issuance and freshness policy.
Renaming or editing the simulator report does not satisfy any physical gate.

Provision the physical-acceptance public document out of band as a root-owned,
non-group-writable file beneath an entirely root-owned, non-group-writable,
non-symlink directory chain, and compare its key ID to the release-approval
record. The verifier opens and identity-stably reads this trust anchor and
rejects caller-owned key files. Never copy a key from the evidence bundle it is
supposed to authenticate. The fixed envelope names are:

- `inventory/reports/platform-update-signed-manifest.json`;
- `inventory/reports/platform-update-control-host-recovery.json`; and
- `inventory/reports/platform-update-spark-canary-rollback.json`.

Because the approved physical exporter is currently unavailable, the release
stays blocked. A locally generated key or hand-authored envelope is not an
acceptable substitute.

Evidence must contain node IDs, versions, content digests, slot/generation
identities, timestamps, outcome states, and bounded sanitized errors. Remove
tokens, private keys, certificates, registry credentials, model inputs/outputs,
NAS paths that disclose secrets, and full environment dumps. Do not sanitize by
changing a digest after it has been signed; retain the original in the protected
evidence store and publish a separately content-addressed sanitized projection.

The signed manifest claim must prove that the immutable target filename SHA is
the shared release digest and must bind the build digest plus every published
architecture's agent payload hash. The control-host claim separately binds the
candidate generation recovered after a crash and the predecessor selected by
the subsequent rollback. The Spark claim must include its architecture,
before/target platform versions, agent payload hashes, target build digest, A/B
slot transition, supervisor generation, exact `outbound-mtls-agent-channel`
transport, and `ssh_used_for_standard_path: false`; its target payload and build
must match the signed manifest claim. Omitting any binding keeps its gate open.

Finally run:

```bash
sudo scripts/verify-platform-release \
  --candidate REPLACE_SEMVER \
  --physical-evidence-public-key \
    /srv/dgx-forge/trust/physical-acceptance-public.json \
  --json
```

The exit status remains blocked until the physical update gates and every other
first-release gate are present.

## DGX OS and NVIDIA maintenance boundary

DGX OS, firmware, kernel, driver, and CUDA maintenance follows the separate
[DGX platform update](platform-update.md) procedure. The pinned NVIDIA
`spark_updatectl.py` adapter may contribute reboot readiness, next-boot kernel,
and kernel rollback evidence to that workflow. It cannot authorize a DGX-Forge
release, verify DGX-Forge TUF metadata, transport the DGX-Forge OCI bundle, sign
an agent operation, or replace the A/B supervisor. Likewise, a successful
DGX-Forge rollout is not evidence that firmware or the operating system is
current.
