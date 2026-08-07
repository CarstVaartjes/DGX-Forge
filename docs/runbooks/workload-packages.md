# Workload package operations

This is the compatibility runbook for the current signed workload-package
release plane. New Vonk recipes are catalog records, not Git files: local
PostgreSQL owns their revisions, imports, install plans, placements, and runs,
and the optional public catalog only supplies immutable data to import. Do not
use this Git/TUF promotion flow as a prerequisite for authoring or running a
local recipe. A future catalog-backed release projection will preserve the
same digest, capacity, topology, and evidence checks.

This runbook is the operator contract for model and runtime releases. A
workload package is a signed, content-addressed description of a complete
stack: source or OCI inputs, model/checkpoint files, adapters, Python
environments, configuration, and the validation evidence needed to run it.
The package path is generic. Adding a model, a new Mia/DS4/vLLM release, or a
new auxiliary component does not require a DGX-Forge release.

The NAS is the administration and authority host. Its Docker services (the
API/worker, PostgreSQL, Caddy, LiteLLM, Hermes, Prometheus, and Grafana) are
separate services and are updated by the host-local platform updater. Sparks
run the outbound mTLS agent and keep large model payloads in their local
content-addressed stores. Payloads are fetched directly from the declared,
authenticated Git, HTTPS, OCI, or other approved provider; the NAS is not a
model-weight relay.

## Release planes and trust boundaries

Keep the two release planes independent:

| Plane | Authority | Updates | Does not update |
| --- | --- | --- | --- |
| Platform | platform TUF and the signed platform manifest | NAS Docker generations, the Spark agent/supervisor, protocol and privileged helper ABI | model IDs, checkpoints, adapters, or ordinary runtime releases |
| Workload | NAS-admin Git/TUF repository and signed workload locks | families, releases, adapters, images, environments, checkpoints, configuration, and deployment plans | the agent, supervisor, platform services, or SSH configuration |

The Spark agent contains a stable, typed package ABI and safe operation
vocabulary. It must not contain a catalog of model names or adapter versions.
The workload trust root authorizes immutable release-lock targets; node policy
authorizes only the ABI operations and declared capabilities. The normal path
never uses SSH, `agent.update`, platform TUF, or a control-plane file copy.
SSH remains available for one-time onboarding and explicitly documented
recovery only; recovery commands and their evidence must identify that
exception.

## Before publishing

1. Create a generic family document with a stable `family_id`, declared
   architecture/OS/capabilities, license and credential requirements, and a
   dependency graph. Do not add a model-specific branch to the agent.
2. Build each component from an immutable source revision or digest. Record
   the exact source, media type, byte size, unpacked size, platform, and
   content digest. Never use a mutable tag as an identity.
3. Produce a canonical release lock that includes the adapter ABI, compatibility
   constraints, validation steps, provenance, and all component digests.
4. Run local lint, license, provenance, capacity, and architecture checks. Keep
   secrets out of the lock, command line, logs, and evidence.
5. Sign and publish the lock and TUF metadata from the NAS administration
   workflow. The public Git/TUF state is the reviewable source; a local edit is
   not an authorized release.

## Candidate review and promotion

The CLI and web Admin → Workload packages expose the same API. Commands are
plan-first and return a digest that an administrator must review.

```bash
# Discover and inspect signed candidates (no Spark mutation)
sparkctl admin packages candidates list --family synthetic-stack --json
sparkctl admin packages candidates get --candidate CANDIDATE_UUID --json

# Validate and preview promotion. Keep the returned plan digest.
sparkctl admin packages validation-preview --candidate CANDIDATE_UUID --json
sparkctl admin packages validate --candidate CANDIDATE_UUID \
  --plan-digest VALIDATION_PLAN_DIGEST --json
sparkctl admin packages promote \
  --candidate CANDIDATE_UUID \
  --preview-digest sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --json

# The web UI presents the same candidate lock, evidence, and plan digest.
```

Promotion is rejected when the lock is unsigned, unapproved, malformed,
revoked, incompatible, or not the exact candidate selected by the signed
review. A successful promotion records the immutable release digest, source
commit, validation evidence, approver, and audit/job links. It does not fetch
model weights to the NAS or alter a Spark.

## Rollout and progress

Select a repository-declared deployment and review the topology-aware plan.
The plan shows canary nodes, batches, offline nodes, storage/download
requirements, compatibility, and the predecessor release.

```bash
sparkctl admin deployments rollout-preview \
  --deployment synthetic-canary --json
sparkctl admin deployments rollout \
  --deployment synthetic-canary \
  --plan-digest sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  --json

sparkctl admin deployments status --deployment synthetic-canary \
  --rollout ROLLOUT_ID --json
sparkctl admin deployments repair-preview --deployment synthetic-canary --json
sparkctl admin deployments repair --deployment synthetic-canary \
  --plan-digest REPAIR_PLAN_DIGEST --json
```

The control plane queues typed package operations over each Spark's outbound
mTLS channel. A node resolves the signed lock, reserves storage, acquires
components directly from their providers, verifies every digest and size,
materializes a new immutable generation, then performs prepare, activate, and
health checks. Progress is durable and bounded: byte counters, phase,
attempt, cancellation, restart recovery, and operation/fence IDs survive an
agent restart. The currently active generation remains serving until the new
generation passes activation and health.

Canary failure pauses later batches. A rollback selects only the recorded
predecessor release and is itself a fenced, audited package operation. If the
predecessor is unavailable or a rollback cannot be proved safe, the rollout
enters `waiting-for-operator`; it never silently selects an arbitrary older
generation.

## Rollback, repair, and Garbage collection

```bash
sparkctl admin deployments rollback-preview \
  --deployment synthetic-canary --rollout ROLLOUT_ID --json
sparkctl admin deployments rollback \
  --deployment synthetic-canary \
  --rollout ROLLOUT_ID \
  --plan-digest ROLLBACK_PLAN_DIGEST \
  --json

sparkctl admin packages gc-preview --json
sparkctl admin packages gc --plan-digest GC_PLAN_DIGEST --json
```

Rollback is possible offline when the predecessor generation and its verified
objects are present on the Spark. Stop network access only after recording the
release and generation digests; the agent must not silently re-download or
fall back to a NAS copy. Repair reconstructs missing indexes from verified
objects and refuses path traversal, symlink substitution, digest mismatch, or
an unapproved lock. GC is preview-first, keeps active and recorded rollback
generations, and is resumable after interruption.

## Credentials, licenses, and provider outages

Credentials are references to root-controlled secret stores, never values in
Git or a package lock. The operator grants only the provider and release
needed for the operation. License evidence is reviewed before promotion and
is retained by digest. A missing credential, license, network, or provider
object produces a typed, redacted failure with a retry/compensate/operator
disposition; it does not expose a token or mark a partial download complete.

Downloads use bounded, resumable ranges with durable progress, reservation,
atomic promotion, cancellation, and restart recovery. A failed or cancelled
transfer remains outside the active generation until its digest and size are
verified. Providers are contacted by Sparks through their authenticated
outbound route; the NAS control worker does not proxy arbitrary URLs.

## NAS platform updates and Spark skew

NAS Docker services and Spark worker code have separate update actions. When
the NAS reports a newer compatible DGX-Forge platform release than one or more
Sparks, the web Admin → Updates page and `sparkctl admin updates skew --json`
show the exact versions, affected nodes, signed target digest, and a
topology-aware fan-out preview. The operator must explicitly confirm the
signed `agent.update` command for each eligible Spark (canary first, then the
remaining nodes). The command uses the outbound mTLS agent channel and A/B
supervisor; it does not use SSH.

```bash
sparkctl admin updates skew --json
sparkctl admin updates plan --target-version 2.0.0 --json
sparkctl admin updates apply --plan-digest PLAN_DIGEST --json
sparkctl admin updates status --json
```

An older, compatible Spark agent may continue serving ordinary workload
releases while the operator reviews the skew prompt. A workload package never
triggers this prompt: only a platform capability/protocol/agent update does.
If a platform update is required for a package's genuinely new privileged ABI,
the candidate must state that compatibility requirement and the UI must show
the separate platform action before rollout.

## Recovery-only SSH

SSH is permitted only for one-time bootstrap, certificate/key recovery, or a
host that cannot establish its outbound agent channel. Record the operator,
target node, reason, start/end time, command digest, and recovery evidence.
Do not use SSH to install a workload, copy a model, run a routine update, or
repair a rollout. After recovery, rotate the affected credential/certificate,
restore the outbound channel, and verify the node's platform/workload state
through the control plane.

## Evidence and first-release gate

The first release requires both independent acceptance sets:

- the unknown-family flow creates a family after the installed agent was built,
  publishes signed release 1, activates release 2, rolls back to release 1
  while offline, and rejects unsigned/unapproved input;
- the failure/recovery matrix covers transport, trust, capacity, activation,
  health, cancellation, GC, restart, canary, and concurrent-download cases.

The workload evidence must explicitly record that no SSH or `agent.update` call
occurred and distinguish simulated from physical evidence. The platform
release verifier combines these reports with platform update evidence; it does
not treat a simulator as physical Spark acceptance.

```bash
scripts/accept-workload-packages --mode simulated --json
scripts/accept-workload-package-failures --json
scripts/verify-platform-release --candidate 1.0.0 --json
```

A blocked result names the exact missing gate. Keep the redacted JSON output,
source commit, release digests, and test command in the protected release
artifact. Hosted CI stores the two canonical outputs as
`workload-package-acceptance.json` and
`workload-package-failure-matrix.json`; the release verifier checks their
content digests instead of trusting an unrecorded console result. Do not
synthesize physical hardware evidence; record it later with the approved Spark
inventory procedure.
