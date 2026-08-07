# Control-Host Generation and Supply-Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make control-host apply, rollback, and recovery select exact versioned TUF releases and verified OCI deployment assets through one root-owned, journaled host boundary.

**Architecture:** A root-installed updater owns immutable generation state, one operation lock, a hash-chained phase journal, fixed backup/command boundaries, and the read-only identity projection consumed by containers. Versioned TUF targets authorize exact platform manifests and predecessors; each manifest pins one OCI bundle containing every Compose/config asset actually executed.

**Tech Stack:** Python 3.12, python-tuf, OCI/ORAS 1.3, Docker Compose v2, SQLAlchemy/Alembic, FastAPI, `flock`, canonical JSON/tar, pytest.

## Current implementation ledger — 2026-08-06

The implementation for Tasks 1–10 is landed on `main` through the platform
update and workload-lane integration. The checkbox sequence below remains the
original red/green execution checklist; current evidence is recorded here so
it is not mistaken for an unimplemented subsystem. Versioned platform release
identity, canonical deployment bundles, root-owned generations, bounded host
commands/backups, exact OCI acquisition, preselection/readiness, journaled
apply/recovery, signer separation, publication tooling, and the release
verifier are all present and covered by focused tests.

The remaining release work is external evidence only: physical control-host
update/recovery, physical replacement-host recovery, physical GPU node canary and
rollback, authenticated-encryption recovery, and a signed platform-update
manifest produced with the production release key. No simulator or local test
is treated as proof of those gates.

## Global Constraints

- Follow [the approved design](../specs/2026-08-06-control-host-generation-supply-chain-design.md).
- Production never executes or deploys Compose/config/script content from a mutable repository checkout.
- Only root writes `/srv/vonk-forge/control-host`; containers mount the root-owned `/srv/vonk-forge/control-identity/` directory read-only and reopen projections by dirfd/`O_NOFOLLOW`.
- One exclusive `operation.lock` descriptor spans every apply, rollback, and recovery side effect through generation-bound worker readiness.
- TUF target name, target SHA-256, targets version, release/build digest, OCI manifest/layer descriptors, and predecessor are exact bindings.
- No shell, free-form command, mutable backup script, unbounded subprocess output, or unbounded deadline is permitted.
- Every production change follows red-green-refactor with only the named focused tests; broad suites run once at the final gate.
- Preserve the existing root-agent additions in `control/tests/test_upgrade.py` and `control/src/vonk_control/upgrade.py`; refactor them, do not replace or discard them.
- Do not create a commit in this task stream.

---

### Task 1: Versioned platform release and exact predecessor contract

**Files:**
- Modify: `schemas/platform-update-manifest.schema.json`
- Modify: `src/cluster_profiles/schemas/platform-update-manifest.schema.json`
- Modify: `src/cluster_profiles/platform_release.py`
- Modify: `tests/cluster_profiles/test_platform_release.py`
- Modify: `control/src/vonk_control/update_authority.py`
- Modify: `control/tests/test_update_authority.py`

**Interfaces:**
- Produce `OciDeploymentBundle(reference, manifest_digest, manifest_size, manifest_media_type, layer_digest, layer_size, layer_media_type)`.
- Produce `AuthorizedPredecessor(target_name, target_sha256, release_digest, build_digest, deployment_bundle_digest)`.
- Change `VerifiedReleaseSource.refresh(target_name: str) -> tuple[bytes, int]` so no consumer assumes `platform-release.json`.

- [ ] **Step 1: Write failing schema/parser tests**

  Add literal v2 fixtures proving immutable `platform/releases/<semver>/<sha>.json`
  names, one exact OCI bundle descriptor, a bounded host-updater ABI range, and
  exact predecessor objects. Reject the legacy compatible-build-only shape,
  target aliases, descriptor digest/size mismatch, duplicate predecessors,
  and target-name/SHA disagreement.

- [ ] **Step 2: Verify red**

  Run:
  `uv run pytest -q tests/cluster_profiles/test_platform_release.py control/tests/test_update_authority.py -k 'bundle or predecessor or target_name or versioned'`

  Expected: failures show the current schema lacks bundle/predecessor fields
  and the authority still uses the fixed target name.

- [ ] **Step 3: Implement the minimal v2 contract**

  Add strict dataclasses and parsing. Canonical release digest remains
  `sha256(canonical_manifest_bytes)`. Update authority preparation/receipts to
  accept and emit the caller-selected exact target name, target SHA-256, and
  current TUF targets version.

- [ ] **Step 4: Verify green**

  Run the Step 2 command and require all selected tests to pass.

### Task 2: Canonical OCI deployment bundle build and verification

**Files:**
- Create: `schemas/control-deployment-bundle.schema.json`
- Create: `src/cluster_profiles/schemas/control-deployment-bundle.schema.json`
- Create: `src/cluster_profiles/deployment_bundle.py`
- Create: `tests/cluster_profiles/test_deployment_bundle.py`
- Create: `scripts/build-control-deployment-bundle`
- Create: `tests/scripts/test_build_control_deployment_bundle.py`
- Modify: `scripts/verify-supply-chain`

**Interfaces:**

```python
@dataclass(frozen=True)
class VerifiedDeploymentBundle:
    archive_sha256: str
    manifest_sha256: str
    files: Mapping[str, BundleFile]

def verify_deployment_bundle(raw: bytes, descriptor: OciDeploymentBundle) -> VerifiedDeploymentBundle: ...
def extract_deployment_bundle(raw: bytes, destination: Path, verified: VerifiedDeploymentBundle) -> None: ...
```

- [ ] **Step 1: Write failing canonical-bundle tests**

  Build a fixture containing every path referenced by the production Compose
  graph. Assert deterministic bytes/modes/digests and reject omitted assets,
  extra files, links, duplicate members, traversal, devices, oversized files,
  noncanonical tar headers, descriptor mismatch, and a mutable path outside
  the extracted generation.

- [ ] **Step 2: Verify red**

  Run:
  `uv run pytest -q tests/cluster_profiles/test_deployment_bundle.py tests/scripts/test_build_control_deployment_bundle.py`

  Expected: module and builder are absent.

- [ ] **Step 3: Implement builder and safe verifier**

  Use sorted POSIX paths, fixed uid/gid/mtime, modes `0644` or explicitly
  allowlisted `0755`, a canonical `deployment-bundle.json`, and descriptor-
  first size/SHA verification. Extraction must create new files with
  `openat`/`O_NOFOLLOW` semantics and fsync the result.

- [ ] **Step 4: Verify green and supply-chain integration**

  Run the Step 2 command plus
  `scripts/verify-supply-chain --json >/tmp/vonk-supply-chain.json`.

### Task 3: Root host state, identity projection, and one operation lock

**Files:**
- Create: `control/src/vonk_control/host_state.py`
- Create: `control/tests/test_host_state.py`
- Modify: `control/src/vonk_control/upgrade.py`
- Modify: `control/tests/test_upgrade.py`
- Modify: `control/src/vonk_control/offline.py`
- Modify: `control/tests/test_offline.py`

**Interfaces:**

```python
class HostOperationLock:
    def __enter__(self) -> HostOperationLock: ...
    def __exit__(self, *_args: object) -> None: ...

class HostGenerationStore:
    def load_active(self) -> SelectedGeneration | None: ...
    def commit_generation(self, staged: Path, receipt: SelectionReceipt) -> Path: ...
    def select(self, receipt: SelectionReceipt) -> None: ...
    def project_candidate(self, operation: HostOperationPlan) -> CandidateProjection: ...

class PhaseJournal:
    def create(self, plan: HostOperationPlan) -> JournalState: ...
    def append(self, phase: str, evidence: Mapping[str, object]) -> JournalState: ...
    def load_pending(self) -> JournalState | None: ...
```

- [ ] **Step 1: Write failing filesystem and contention tests**

  Cover root/effective-owner and mode checks, symlink/hardlink/race rejection,
  canonical bounded dirfd reads, visibility of atomic `active.json`
  replacement through a directory bind mount, candidate/active projection-kind
  separation, atomic pointer+projection replacement, immutable
  generation receipts, contiguous hash-chained phase entries, concurrent
  apply/rollback/recovery contention, and proof that the lock remains held
  during injected candidate/readiness callbacks.

- [ ] **Step 2: Verify red**

  Run:
  `uv run --project control --with-editable . pytest -q control/tests/test_host_state.py control/tests/test_offline.py control/tests/test_upgrade.py -k 'host_state or operation_lock or journal or projection'`

  Expected: missing host-state interfaces and multiple existing lock paths.

- [ ] **Step 3: Implement state primitives and consolidate locks**

  Move safe receipt logic already added by the root agent into
  `HostGenerationStore`; retain all canonicality/race/hardlink protections.
  Replace `OfflineLock`, `OnlineLock`, and `_acquire_offline_lock` use in host
  mutations with one root-only `HostOperationLock`. Online startup reads only
  the projection and never creates a lock file.

- [ ] **Step 4: Verify green**

  Run the Step 2 command and require all selected tests to pass.

### Task 4: Bounded command runner and fixed backup boundary

**Files:**
- Create: `control/src/vonk_control/host_commands.py`
- Create: `control/src/vonk_control/host_backup.py`
- Create: `control/tests/test_host_commands.py`
- Create: `control/tests/test_host_backup.py`
- Modify: `control/src/vonk_control/offline.py`
- Modify: `control/tests/test_upgrade.py`
- Modify: `deploy/compose/tests/test_backup_restore.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class CommandPolicy:
    timeout_seconds: float
    stdout_limit: int
    stderr_limit: int

@dataclass(frozen=True)
class ArtifactPolicy:
    byte_limit: int
    required_free_bytes: int

class BoundedCommandRunner:
    def run(self, argv: tuple[str, ...], *, cwd: Path, env: Mapping[str, str], policy: CommandPolicy) -> CommandResult: ...
    def stream(self, argv: tuple[str, ...], *, cwd: Path, env: Mapping[str, str], source_fd: int | None, sink_fd: int, command: CommandPolicy, artifact: ArtifactPolicy) -> ArtifactReceipt: ...

class HostBackupBoundary:
    def create_upgrade_backup(self, generation: SelectedGeneration, operation_id: str) -> BackupReceipt: ...
    def verify_for_restore(self, receipt: BackupReceipt) -> VerifiedBackup: ...
```

- [ ] **Step 1: Write failing behavioral tests**

  Use real short subprocesses to prove timeout terminate/kill, streaming
  control-output cap, no shell, minimal environment, closed stdin/fds, fixed
  cwd, and bounded redacted errors. For large artifacts prove preopened
  source/sink fd streaming, rolling byte/SHA receipt, byte limit and disk
  reserve enforcement, incomplete-sink cleanup, and absence of backup-sized
  Python bytes. Prove upgrade backup invokes only fixed Docker/pg_dump and
  `/usr/bin/age` argv, includes exact allowlisted inputs, fsyncs a new file,
  and offers no backup-script or free-form encryption-command path.

- [ ] **Step 2: Verify red**

  Run:
  `uv run --project control --with-editable . pytest -q control/tests/test_host_commands.py control/tests/test_host_backup.py control/tests/test_upgrade.py -k 'subprocess or backup or command'`

  Expected: current `subprocess.run` accumulates output and executes a supplied
  repository backup script.

- [ ] **Step 3: Implement fixed boundaries**

  Use `Popen` with concurrent bounded pipe draining, monotonic deadlines, and
  a terminate/kill grace period. Move the canonical archive code behind
  `HostBackupBoundary`; remove upgrade `--backup-script` and
  `VONK_BACKUP_ENCRYPT_COMMAND` handling in favor of a root-owned recipients
  file setting.

- [ ] **Step 4: Verify green**

  Run the Step 2 command and require all selected tests to pass.

### Task 5: OCI acquisition and exact generation planning

**Files:**
- Create: `control/src/vonk_control/oci_bundle.py`
- Create: `control/tests/test_oci_bundle.py`
- Modify: `control/src/vonk_control/upgrade.py`
- Modify: `control/tests/test_upgrade.py`
- Modify: `control/src/vonk_control/offline.py`

**Interfaces:**

```python
class OciBundleSource:
    def fetch(self, descriptor: OciDeploymentBundle) -> bytes: ...

class ControlUpgrade:
    def plan(self, target_name: str) -> ControlGenerationPlan: ...
    def apply(self, plan: ControlGenerationPlan) -> ControlGenerationResult: ...
    def rollback_plan(self, generation_id: str) -> ControlRollbackPlan: ...
```

- [ ] **Step 1: Write failing exactness tests**

  Prove raw OCI manifest/layer fetch by digest using fixed ORAS manifest/blob
  commands, independent digest/size/media-type verification, and no `oras pull`.
  Prove plans bind current pointer/receipt/running identity/database revision,
  selected TUF target name/SHA/version, bundle/images, host ABI, exact
  predecessor, required bytes, and redacted site-config digest.

- [ ] **Step 2: Verify red**

  Run:
  `uv run --project control --with-editable . pytest -q control/tests/test_oci_bundle.py control/tests/test_upgrade.py -k 'oci or plan or compatibility or drift'`

  Expected: current plan accepts an already-parsed release and binds neither
  versioned TUF identity nor deployment bundle/site state.

- [ ] **Step 3: Implement acquisition and under-lock revalidation**

  Resolve the target by exact name through current TUF metadata. Rebuild the
  whole plan after taking `HostOperationLock`, before any pull/backup, and
  reject any inequality. Add exact database/compatibility probes before
  migration and pointer selection.

- [ ] **Step 4: Verify green**

  Run the Step 2 command and require all selected tests to pass.

### Task 6: Preselection API and generation-bound worker DB readiness

**Files:**
- Modify: `control/src/vonk_control/settings.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/worker.py`
- Modify: `control/src/vonk_control/models.py`
- Create: `control/migrations/versions/0012_control_process_heartbeats.py`
- Create: `control/tests/test_control_process_heartbeat_migration.py`
- Create: `control/tests/test_generation_readiness.py`
- Modify: `control/tests/test_api.py`
- Modify: `control/tests/test_worker.py`

**Interfaces:**

```python
class StartupMode(StrEnum):
    PRESELECTION = "preselection"
    SELECTED = "selected"

class GenerationReadinessService:
    def candidate(self, generation_id: str, start_nonce: str) -> Mapping[str, object]: ...
    def selected(self, generation_id: str, start_nonce: str) -> Mapping[str, object]: ...
```

- [ ] **Step 1: Write failing migration and mode tests**

  Prove preselection exposes only the host readiness route and performs no
  repository/admin/agent/route/update registration or worker loop. Prove a
  worker heartbeat is written only after a completed real scheduler loop and
  binds generation/release/build/start nonce/sequence. Reject stale, wrong-
  generation, wrong-nonce, API-only readiness, a missing/wrong-operation
  candidate projection, and any attempt to treat a candidate projection as
  active. Test migration upgrade and downgrade with existing rows.

- [ ] **Step 2: Verify red**

  Run:
  `uv run --project control --with-editable . pytest -q control/tests/test_generation_readiness.py control/tests/test_control_process_heartbeat_migration.py control/tests/test_api.py control/tests/test_worker.py -k 'preselection or generation_readiness or heartbeat'`

  Expected: startup mode, table, and generation-bound readiness are absent.

- [ ] **Step 3: Implement minimal modes and heartbeat**

  Build a separate preselection FastAPI app rather than conditionally mounting
  production routes. Preselection reopens the exact root-owned candidate
  projection by directory fd; selected mode and signer reopen only
  `active.json` and require `projection_kind=active`. Persist a heartbeat after `run_once`
  returns, including idle loops, and make the host-only selected readiness
  query require a fresh exact match.

- [ ] **Step 4: Verify green**

  Run the Step 2 command and require all selected tests to pass.

### Task 7: Journaled apply, crash recovery, and exact rollback

**Files:**
- Modify: `control/src/vonk_control/upgrade.py`
- Modify: `control/src/vonk_control/offline.py`
- Modify: `control/tests/test_upgrade.py`
- Create: `control/tests/test_upgrade_recovery.py`

**Interfaces:**

```python
class ControlUpgrade:
    def apply(self, plan: ControlGenerationPlan) -> ControlGenerationResult: ...
    def recover(self) -> ControlGenerationResult: ...
    def rollback(self, plan: ControlRollbackPlan) -> ControlGenerationResult: ...
```

- [ ] **Step 1: Write table-driven crash tests before production changes**

  Inject one crash after every design phase and restart with new service
  objects. For each case assert exact probing, no duplicate backup/migration,
  correct resume or explicit recovery, immutable journal chain, and no new
  operation while one is pending. Add N-1/N/N+1 TUF fixtures proving N to N-1
  rollback ignores the N+1 channel, and reject revoked/tampered/wrong-bundle
  predecessors.

- [ ] **Step 2: Verify red**

  Run:
  `uv run --project control --with-editable . pytest -q control/tests/test_upgrade_recovery.py control/tests/test_upgrade.py -k 'crash or recover or rollback or activation_order'`

  Expected: current apply has no phase journal/recovery and rollback trusts
  only a recorded generation name.

- [ ] **Step 3: Implement the phase dispatcher**

  Express every phase as `probe_exact()` plus idempotent `perform()` and append
  evidence only after exact observation. Sequence candidate preselection,
  immutable receipt/directory, pointer/projection, selected API, worker, and
  DB-loop readiness exactly as specified. Use the same dispatcher for
  compensation and explicit rollback.

- [ ] **Step 4: Verify green**

  Run the Step 2 command and require all selected tests to pass.

### Task 8: Production Compose and signer/agent identity projection

**Files:**
- Modify: `deploy/compose/compose.yaml`
- Modify: `deploy/compose/.env.example`
- Modify: `deploy/compose/tests/test.env`
- Modify: `deploy/compose/tests/test_networking.py`
- Modify: `control/src/vonk_control/update_signer.py`
- Modify: `control/src/vonk_control/update_authority.py`
- Modify: `control/src/vonk_control/agent_jobs.py`
- Modify: `agent/src/vonk_agent/update.py`
- Modify: `control/tests/test_update_signer.py`
- Modify: `control/tests/test_agent_jobs.py`
- Modify: `agent/tests/test_update.py`

**Interfaces:**
- Every selected container consumes `/run/vonk-forge/control-identity/` as a read-only directory and reopens `active.json` by dirfd for each validation.
- GPU node update receipts carry the exact selected `platform_target_name`, `platform_target_sha256`, and `tuf_targets_version`.

- [ ] **Step 1: Write failing Compose and receipt tests**

  Prove API/worker/signer lack a writable host-generation mount; the projection
  directory is root-owned and read-only; atomic host replacement is observed
  by a fresh dirfd lookup; production Compose is launched from a verified
  generation; signer startup uses the selected target rather than latest; and
  agents reject fixed-name, wrong-name, wrong-version, or wrong-SHA receipts.

- [ ] **Step 2: Verify red**

  Run:
  `uv run --project control --with-editable . pytest -q deploy/compose/tests/test_networking.py control/tests/test_update_signer.py control/tests/test_agent_jobs.py && uv run --project agent pytest -q agent/tests/test_update.py -k 'platform_target'`

  Expected: existing Compose shares writable `/state` and the authorization
  path assumes `platform-release.json`.

- [ ] **Step 3: Wire the projection and dynamic target identity**

  Keep application state mounts only where independently required, add the
  projection mount, and route signer/worker/agent target selection through the
  exact versioned identity. Do not add TUF private material to API or GPU nodes.

- [ ] **Step 4: Verify green**

  Run the Step 2 command and require all selected tests to pass.

### Task 9: Release publication, operator docs, and end-to-end acceptance

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `scripts/container-release-metadata`
- Create: `scripts/publish-platform-target`
- Create: `tests/e2e/test_control_host_generation.py`
- Modify: `docs/runbooks/platform-release-update.md`
- Modify: `docs/runbooks/control-plane-recovery.md`
- Modify: `deploy/compose/README.md`
- Modify: `scripts/verify-platform-release`

**Interfaces:**
- Release output includes the OCI bundle descriptor plus immutable versioned TUF target name/SHA.
- Operator commands are `upgrade --target-name ... [--apply]`, `recover --apply`, and `rollback --generation ... [--apply]`; none accepts repository Compose or backup-script paths.

- [ ] **Step 1: Write failing publication and E2E acceptance tests**

  Build synthetic N-1/N/N+1 releases and bundle assets, publish all supported
  immutable targets plus an N+1 channel, apply N, crash/recover each phase,
  roll back to N-1, reject unsigned/revoked/mutable inputs, and assert every
  Compose invocation uses the selected generation directory.

- [ ] **Step 2: Verify red**

  Run:
  `uv run pytest -q tests/e2e/test_control_host_generation.py tests/scripts/test_build_control_deployment_bundle.py`

  Expected: versioned publication and host-generation E2E path are absent.

- [ ] **Step 3: Implement publication and update operator material**

  Publish the bundle before the immutable TUF target, retain authorized
  predecessor targets for the support window, and make channel publication
  the final non-authoritative discovery step. Document root-owned install,
  ORAS/age prerequisites, backup recipients, recovery evidence, rollback
  revocation, and first-release migration from the repository deployment.

- [ ] **Step 4: Verify green**

  Run the Step 2 command and require all selected tests to pass.

### Task 10: Focused integration and final verification

**Files:** all files above.

- [ ] **Step 1: Run subsystem suites in parallel**

  Run the platform schema/bundle tests, host state/command/backup/upgrade tests,
  generation readiness/migration tests, signer/agent receipt tests, and Compose
  networking tests as separate parallel commands. Record each count and wall
  time so no slow group hides progress.

- [ ] **Step 2: Run the single broad release gate once**

  Run:

  ```bash
  uv run pytest -q tests/cluster_profiles/test_platform_release.py \
    tests/cluster_profiles/test_deployment_bundle.py \
    tests/e2e/test_control_host_generation.py
  uv run --project control --with-editable . pytest -q control/tests/test_host_state.py \
    control/tests/test_host_commands.py control/tests/test_host_backup.py \
    control/tests/test_oci_bundle.py control/tests/test_upgrade.py \
    control/tests/test_upgrade_recovery.py \
    control/tests/test_generation_readiness.py \
    control/tests/test_control_process_heartbeat_migration.py \
    control/tests/test_update_signer.py control/tests/test_agent_jobs.py
  uv run --project agent pytest -q agent/tests/test_update.py
  uv run pytest -q deploy/compose/tests/test_networking.py
  ```

- [ ] **Step 3: Run static and artifact checks**

  Run Ruff only on changed Python files, compile the offline/host entry points,
  run both supply-chain verifiers, then `git diff --check`.

- [ ] **Step 4: Report the checkpoint without committing**

  Report exact test counts/timings, any physical-host-only acceptance gate,
  and the complete changed-file list. Leave commit/push to the parent agent.

#### Post-integration verification checkpoint

- Platform-release, deployment-bundle, and control-host E2E tests: **102
  passed in 7.97s**.
- Host state/commands/backup/OCI/upgrade/readiness/heartbeat/signer and agent
  job tests: **275 passed in 29.91s**.
- Agent update tests: **21 passed in 0.20s**; Compose networking: **12 passed
  in 1.37s**.
- The dedicated signer/update/control integration selection passed **259
  tests in 41.51s**; the PostgreSQL agent-job/reconciliation race selection
  passed **64 tests in 29.92s**.
- Ruff `0.16.1`, entry-point compilation, supply-chain verification, and
  `git diff --check` passed. The platform verifier remains blocked only on the
  six external gates listed above.
