# Control-Host Generation and Supply-Chain Design

## Objective

Make control-host apply, rollback, and crash recovery operate only on exact,
TUF-authorized platform targets and content-addressed OCI deployment assets.
The mutable repository remains an authoring input; it is never a production
deployment or host-execution input.

This design replaces the current shared writable generation state, single
`platform-release.json` target, repository Compose path, mutable backup-script
execution, partially held lock, and non-journaled activation sequence.

## Chosen architecture

The alternatives considered were:

1. Keep the updater in the online API/worker and harden the shared volume. This
   cannot make host-selection state nonwritable to a compromised online
   process.
2. Run a root helper but continue reading Compose and scripts from the checkout.
   This protects the pointer but still executes mutable, non-release content.
3. Use a root-owned host updater, versioned TUF targets, and one OCI deployment
   bundle per release. This is the selected design because it separates
   authoring, authorization, supply, selection, and online execution.

The host updater is installed as root-owned trusted tooling. A release may
supply its successor updater for a future transaction, but candidate tooling
is never used to install itself. Host tooling compatibility is therefore an
explicit release-manifest range, and a genuinely new host-updater ABI requires
the predecessor updater to support that transition.

## Trust and storage boundaries

The production host uses a dedicated root-owned tree, separate from the
application `control-state` volume:

```text
/srv/vonk-forge/control-host/              root:root 0700
  operation.lock                          root:root 0600
  tuf/{metadata,targets}/                 root:root 0700
  bundles/sha256-<digest>/                root:root 0700
  generations/gen-<release-prefix>/       root:root 0700
  operations/<operation-id>/              root:root 0700
  backups/                                root:root 0700
  active-generation                       root:root 0600
/srv/vonk-forge/control-identity/           root:root 0755
  active.json                              root:root 0444
  candidates/                              root:root 0755
    <operation-id>.json                    root:root 0444
```

Only the root host updater writes these paths. API, worker, signer, and other
containers never mount `control-host`. They receive the root-owned
`control-identity` **directory** as a read-only bind mount. A single-file bind
mount is forbidden because atomic host replacement can leave that mount pinned
to the old inode. Loaders open the mounted directory with `O_DIRECTORY` and
`O_NOFOLLOW`, then reopen and identity-stably validate `active.json` by dirfd
on every use. The active projection is a
canonical, bounded document containing the selected generation, exact TUF
target name and SHA-256, release/build/version, OCI bundle digest, API/worker
image references, database revision, selection-receipt digest, and projection
sequence. The updater replaces it atomically only after writing the immutable
generation receipt and active pointer.

The application `/state` volume remains available for ordinary control-plane
state, but it is not an authority for host generation selection. The update
signer and running services validate their immutable image identity against
the freshly reopened read-only active projection and the projection's exact
versioned TUF target; they do not compare themselves with a mutable “latest”
target or retain an init-time projection copy.

## One host-wide operation lock

`operation.lock` is the only apply/rollback/recovery lock. A root process takes
an exclusive nonblocking `flock` before refreshing TUF or reading the active
pointer and holds the same descriptor through image/bundle acquisition,
backup, migration, candidate startup, pointer selection, worker readiness,
compensation, and the terminal journal entry.

Every mutating host command uses this boundary, including explicit recovery.
There is no nested `offline.lock`, no lock release between migration and API
startup, and no repository recovery script with a second lock convention.
Online processes do not hold this lock: exact container inspection and the
activation state machine determine which generation may run while the host
operation remains serialized.

## Versioned TUF platform targets

Each release is published under an immutable digest-derived name:

```text
platform/releases/<platform-version>/<manifest-sha256>.json
```

TUF consistent-snapshot metadata and current trusted `targets.json` authorize
the exact bytes. A channel document may name a candidate for discovery, but a
plan and every activation receipt bind the immutable target name, target
SHA-256, and TUF targets version. Apply never accepts caller-supplied local
bytes as authorization.

The current TUF target set retains every still-supported rollback target. A
release contains an exact predecessor descriptor, not only a compatible build
list:

```json
{
  "target_name": "platform/releases/1.2.0/<sha>.json",
  "target_sha256": "<sha>",
  "release_digest": "sha256:<sha>",
  "build_digest": "sha256:<sha>",
  "deployment_bundle_digest": "sha256:<sha>"
}
```

Rollback from N to N-1 refreshes current TUF metadata, loads the exact N-1
target named by N's selected receipt and manifest, verifies every descriptor,
and revalidates compatibility. It does not resolve a latest/channel target.
Consequently rollback remains exact when the latest published candidate is
already N+1. Removing N-1 from current trusted targets is an explicit rollback
revocation and makes automatic rollback fail closed.

The signer/agent authorization protocol must carry the selected versioned
platform target name and SHA-256. The existing fixed
`platform-release.json` assumption is removed from the host loader,
`PublishedTUFReleaseSource`, signer receipts, and agent verification.

## OCI deployment bundle

The platform target names one OCI artifact by repository, manifest digest,
manifest size/media type, bundle-layer digest, size, and media type. TUF
authenticates that descriptor; OCI supplies the bytes. OCI descriptor sizes
and digests are verified before parsing.

The bundle is a canonical tar containing `deployment-bundle.json` and every
release-controlled asset used by Docker Compose: main Compose and overlays,
Caddy configuration and entrypoint, LiteLLM bootstrap/supervisor/entrypoint,
Prometheus configuration, Grafana provisioning/dashboards, registry config,
step-ca public config, Tailscale/Hermes Compose assets, and release public
trust files. The bundle manifest lists the exact relative path, mode, size,
and SHA-256 of every file. Links, devices, duplicate paths, absolute paths,
parent traversal, unexpected files, and noncanonical archives are rejected.

The host fetches raw OCI manifest and layer bytes by digest using the pinned
ORAS host client, then performs its own bounded descriptor and tar validation.
It never uses `oras pull` extraction. The selected generation stores the
verified assets and the rendered Compose model. `docker compose config` and
all later Compose commands use that generation directory and a root-owned
site environment; they never use a repository path. Site secrets and local
addresses remain host inputs and are represented in the plan by a redacted
configuration digest.

This follows TUF's versioned/consistent-snapshot model and OCI's descriptor
content-addressability. See the [TUF specification](https://theupdateframework.github.io/specification/v1.0.19/),
[OCI descriptor specification](https://github.com/opencontainers/image-spec/blob/main/descriptor.md),
[OCI artifact manifest guidance](https://github.com/opencontainers/image-spec/blob/main/manifest.md),
and [Docker Compose config contract](https://docs.docker.com/reference/cli/docker/compose/config/).

## Fixed trusted backup boundary

Upgrade backup no longer accepts `--backup-script` or executes
`deploy/compose/bin/backup-control-plane`. `HostBackupBoundary`, installed with
the trusted host updater, performs the fixed sequence:

1. invoke PostgreSQL custom-format dump through the selected generation's
   Compose project with fixed argv;
2. collect only the allowlisted site configuration and selected-generation
   receipt/assets required for recovery;
3. build the canonical checksum-bound archive with the in-package backup
   implementation;
4. encrypt it with fixed `/usr/bin/age` argv and a root-owned recipients file;
5. fsync a new owner-only backup and return its path, size, and SHA-256.

No shell, arbitrary executable, free-form encryption command, repository
script, or candidate bundle program is executed on the host. Restore uses the
same trusted parser/decryptor and requires an exact backup receipt from the
operation journal.

## Exact planning and compatibility revalidation

`ControlGenerationPlan` is a digest over an exact current snapshot and exact
target snapshot. Current identity includes generation/pointer/receipt digest,
versioned target name/SHA-256, release/build/version, deployment bundle,
protocol/config ranges, database revision, and running container identity.
Target identity includes the same release fields, required host-updater ABI,
image digests, exact predecessor descriptor, bundle descriptors, required
bytes, and the redacted site-configuration digest.

Dry-run may compute this plan without mutation. Apply takes the host-wide lock,
refreshes TUF, reloads current state and running identities, rebuilds the plan,
and requires byte-for-byte plan equality before acquisition or backup. It
revalidates the exact database revision and target compatibility immediately
before migration and again before pointer selection. Any target, active
generation, site configuration, protocol, image, database, or predecessor
drift invalidates the plan.

Rollback builds and revalidates an equally exact plan from active N and the
N-1 descriptor. A boolean `predecessor_compatible` or matching build digest is
not sufficient on its own.

## Idempotent phase journal and recovery

Each operation has an immutable canonical `plan.json` plus monotonically named
phase entries (`0001-authorized.json`, `0002-bundle-verified.json`, and so on).
Every entry contains the operation/plan digest, phase, exact evidence, previous
entry digest, and timestamp. Entries are written with `O_EXCL`, fsynced, and
the operation directory is fsynced. The hash chain and contiguous sequence
make truncation, substitution, and phase skipping detectable.

Apply phases are:

1. authorization and exact revalidation;
2. bundle and image acquisition;
3. generation staging and rendered-Compose verification;
4. fixed backup completion;
5. worker/API stop and database migration;
6. candidate API preselection readiness;
7. immutable generation receipt and directory commit;
8. active pointer and read-only identity projection selection;
9. selected API and worker start;
10. generation-bound worker database-loop readiness;
11. terminal completion.

Rollback uses the same mechanics with exact predecessor authorization,
predecessor generation verification, pointer/projection selection, service
start, and generation-bound readiness.

Every side effect has an exact probe. On restart, recovery takes the same lock,
reads the highest valid journal phase, probes the next effect, and either
adopts an exact completed result or repeats an idempotent command. A migration
is considered complete only when the database reports the exact target
revision; predecessor revision can be resumed, while any third state requires
operator recovery. A container is adoptable only when its labels and running
identity match the operation and generation. An unfinished operation blocks a
new operation until recovered or explicitly failed closed.

## Candidate and selected readiness

The candidate API first starts in `preselection` mode, on a host-local
unpublished endpoint. This mode exposes only bounded startup/readiness output;
it does not serve the admin/agent API, start reconciliation, publish routes,
issue updates, or run background database mutation loops. Readiness proves the
candidate image identity, target database revision, configuration parsing,
required secret access, and exact candidate generation.

Before candidate startup, the updater writes a distinct canonical candidate
projection at `control-identity/candidates/<operation-id>.json`. It is bound to
the operation ID, plan digest, candidate generation, target name/SHA/version,
bundle digest, and candidate image identity and has
`"projection_kind":"candidate"`. Preselection receives the operation ID and
candidate path through fixed environment, mounts the identity directory
read-only, and requires that exact candidate projection. Selected-mode and
signer loaders accept only `"projection_kind":"active"` from `active.json`;
they cannot interpret a candidate projection as selected state. Candidate
projection removal is journaled cleanup after completion or compensation.

Only after preselection succeeds does the updater commit `generation.json`,
rename the generation, replace `active-generation`, and replace the identity
projection. It then recreates the API in `selected` mode and starts the worker.
The worker writes a database heartbeat after completing a real scheduler loop,
bound to generation ID, release/build digest, process start nonce, and loop
sequence. The selected API's host-only readiness endpoint succeeds only when
its own immutable identity matches the projection and a fresh worker heartbeat
matches that same generation and start nonce. This proof is required before
the terminal journal entry.

Failure before pointer selection restores the exact predecessor services.
Failure after pointer selection is compensated by the exact predecessor
rollback path under the same lock; the journal is never reported complete
while only the candidate API is healthy.

## Bounded subprocess boundary

All host commands use one `BoundedCommandRunner`: fixed argv, absolute trusted
executables, explicit minimal environment, fixed working directory, no shell,
no inherited stdin, close-on-exec descriptors, per-command deadlines, and
streaming control-output caps. Timeout sends terminate then kill after a short
fixed grace period. Control output beyond the cap terminates the process
instead of being accumulated in memory. Errors expose a bounded redacted
suffix and evidence digest, never environment or secret values.

Large binary paths—PostgreSQL dumps, canonical backup archives, age input and
output, OCI manifests/layers, and restore streams—never use in-memory
`stdin: bytes` or captured stdout. The caller creates safe source/sink files,
opens them with `O_NOFOLLOW` and the required exclusive mode, and passes only
preopened file descriptors. The runner streams fd-to-fd while enforcing the
operation's explicit artifact byte limit and reserved-filesystem quota and
computing a rolling byte count and SHA-256 receipt. Crossing the limit or
available-space reserve terminates the producer and removes the incomplete
sink. No backup-sized value is retained in Python memory.

Initial limits are 1 MiB per stdout/stderr **control** stream, 64 KiB JSON
responses, 15 minutes for OCI or image acquisition, 10 minutes for
backup/migration, 2 minutes for Compose and service changes, and 90 seconds
for each readiness gate. Artifact limits come from the exact plan and the
configured root-owned backup/bundle quota. Tests use much shorter injected
deadlines and quotas.

## Acceptance and review-finding coverage

Focused tests must prove:

| Finding | Required proof |
| --- | --- |
| C10 | Online UIDs cannot write generations/journals; only the projection is mounted read-only. |
| C11 | Apply, rollback, and recovery contend on one lock held through final readiness. |
| C12 | Multiple immutable versioned platform targets are selected by exact name/SHA/version. |
| C13 | Rollback accepts only the selected release's exact authorized predecessor descriptor. |
| C14 | Compose and every release asset come from the verified OCI bundle, never the checkout. |
| C15 | Upgrade backup executes the fixed trusted boundary and no mutable script/free-form command. |
| C16 | Current and target compatibility are reloaded under lock before side effects and selection. |
| C17 | Crash injection after every phase resumes idempotently or enters explicit recovery. |
| C18 | Preselection API is inert; completion requires generation-bound worker DB-loop readiness. |
| C19 | N to N-1 rollback succeeds while the discovery/latest target is N+1 and rejects a revoked N-1. |
| C20 | Time, output, argv, environment, and error-reporting bounds are enforced. |

End-to-end acceptance builds an OCI bundle from a synthetic release, publishes
N-1/N/N+1 versioned targets, applies N from N-1, injects crashes at every
phase, completes recovery, publishes N+1 as latest without selecting it, and
rolls N back to the exact N-1 target without touching a repository checkout.
