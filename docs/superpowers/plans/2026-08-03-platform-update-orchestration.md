# Platform Update Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one signed DGX-Forge release safely to the Docker service host and Spark agents with compatibility checks, canary fan-out, A/B rollback, and recovery evidence.

**Architecture:** TUF root/targets/snapshot/timestamp metadata authorizes a canonical compatibility manifest that binds OCI control images, migrations, agent slots, node tooling, SBOMs, provenance, and protocol ranges. OCI/ORAS transports exact blobs. Host-local offline tooling updates the control plane; the online orchestrator updates healthy agents in topology-aware batches.

**Tech Stack:** Python 3.12, JSON Schema, `python-tuf`/ngclient, OCI Distribution and ORAS, Docker Compose, cosign-compatible provenance, Alembic, systemd A/B supervisor, pytest

## Global Constraints

- Every release input is immutable and signature/digest verified before mutation.
- The control plane never replaces itself through an ordinary online API job.
- Database migrations use expand/contract sequencing and retain old/new service compatibility during rollout.
- Spark updates use the agent channel when healthy; SSH is recovery-only.
- Default rollout is explicit canary, soak, then batches of one; distributed workload availability constrains batches.
- First failure pauses fan-out and continuing after rollback requires operator approval.
- This plan updates DGX-Forge application/control/agent artifacts. DGX OS,
  driver, firmware, and kernel maintenance is a separate node-maintenance
  workflow. The pinned NVIDIA `spark_updatectl.py` may supply reboot readiness,
  next-boot kernel, and rollback evidence, but cannot authorize or transport a
  DGX-Forge release.

---

### Task 1: Canonical platform release manifest

**Files:**
- Create: `schemas/platform-update-manifest.schema.json`
- Create: `src/spark_profiles/platform_release.py`
- Create: `src/spark_profiles/update_trust.py`
- Create: `tests/fixtures/tuf/`
- Modify: `pyproject.toml`
- Test: `tests/spark_profiles/test_platform_release.py`

**Interfaces:**
- Produces `PlatformRelease.load(path)`, `compatibility(current)`, and canonical `digest`; `UpdateTrust.refresh()` and `trusted_target(name) -> TargetInfo` use TUF metadata and persisted trusted-root/version state.
- Manifest binds control images/assets/config version, migration floor/ceiling, agent artifacts by architecture, supervisor/tooling artifacts, protocol ranges, SBOM/provenance, and rollback compatibility.

- [ ] **Step 1: Write failing manifest validation tests**

Test floating image tags, missing digest/SBOM, overlapping architecture
entries, invalid protocol interval, destructive migration without predecessor
compatibility, unknown fields, canonical digest under reordered input, expired
timestamp/snapshot metadata, rollback/freeze/mix-and-match metadata, root rotation,
and a target whose bytes disagree with TUF length/hash.

- [ ] **Step 2: Run and observe missing contract**

Run: `uv run pytest tests/spark_profiles/test_platform_release.py -v`
Expected: FAIL importing platform release module.

- [ ] **Step 3: Implement strict schema and typed loader**

Use the maintained TUF client implementation and persist its trusted root and
highest accepted metadata versions atomically. Require full SHA-256 digests,
OCI `@sha256`, semantic version, supported architectures, `control_api_protocol` and `agent_protocol`
closed intervals, migration expand revision and optional later contract
revision, plus recovery-compatible predecessor digests. The manifest is a TUF
target; do not invent a second signature/rollback metadata format and never
resolve mutable tags.

- [ ] **Step 4: Run schema/package tests**

Run: `uv run pytest tests/spark_profiles/test_platform_release.py tests/spark_profiles/fleet/test_schemas.py -v`
Expected: PASS with packaged schema copy.

- [ ] **Step 5: Commit release contract**

```bash
git add schemas/platform-update-manifest.schema.json src/spark_profiles/schemas/platform-update-manifest.schema.json src/spark_profiles/platform_release.py src/spark_profiles/update_trust.py tests/fixtures/tuf pyproject.toml tests/spark_profiles/test_platform_release.py
git commit -m "feat: define signed platform update manifests"
```

### Task 2: Host-local control-plane upgrade generations

**Files:**
- Modify: `control/src/dgx_control/offline.py`
- Modify: `bin/dgx-control-offline`
- Create: `control/src/dgx_control/upgrade.py`
- Test: `control/tests/test_upgrade.py`

**Interfaces:**
- Produces `dgx-control-offline upgrade --release PATH [--apply]` and `rollback --generation ID --apply`.
- Generation records exact release digest, Compose rendering hash, database backup manifest, previous generation, and readiness evidence.

- [ ] **Step 1: Write failing plan/apply/rollback tests**

Use disposable fake Docker/Compose and PostgreSQL boundaries. Assert dry-run
mutates nothing; online lock rejects; backup precedes migration; images are
digests from manifest; worker stops before migration; API readiness precedes
generation commit; failed readiness restores prior config/images; ambiguous
database failure enters operator recovery rather than destructive rollback.

- [ ] **Step 2: Run and observe missing upgrade command**

Run: `uv run --project control pytest control/tests/test_upgrade.py -v`
Expected: FAIL because subcommand/service is absent.

- [ ] **Step 3: Implement host-local generation state machine**

Require exclusive offline lock and stopped worker/API proof. Refresh and verify
TUF metadata, fetch exact OCI targets with ORAS, verify release and
disk, invoke existing encrypted backup boundary, render Compose into a new
restricted generation, pull exact images, run expand migration, start API then
worker, probe through Caddy, and atomically select generation. Record commands
as fixed argv with bounded output; no shell. Roll back image/config generation
automatically only when database compatibility permits.

- [ ] **Step 4: Run offline/recovery/upgrade tests**

Run: `uv run --project control pytest control/tests/test_upgrade.py control/tests/test_offline.py -v && uv run pytest deploy/compose/tests/test_backup_restore.py -v`
Expected: PASS.

- [ ] **Step 5: Commit host updater**

```bash
git add control/src/dgx_control/offline.py control/src/dgx_control/upgrade.py bin/dgx-control-offline control/tests/test_upgrade.py
git commit -m "feat: update control plane through recoverable generations"
```

### Task 3: Agent update operation and supervisor activation

**Files:**
- Create: `agent/src/dgx_agent/update.py`
- Modify: `agent/src/dgx_agent/operations.py`
- Modify: `agent/supervisor/dgx-agent-supervisor`
- Test: `agent/tests/test_update.py`

**Interfaces:**
- `AgentUpdater.plan(artifact, release) -> UpdatePlan`, `apply(plan) -> PendingActivation`.
- Supervisor consumes activation request with inactive slot, expected digest, previous slot, deadline, and control readiness marker.

- [ ] **Step 1: Write failing A/B update tests**

Test wrong architecture/signature/digest, insufficient space, incompatible
protocol, active-slot overwrite attempt, interrupted download, successful
reconnect marker, crash loop, readiness timeout, automatic rollback, and both
slots corrupt requiring recovery.

- [ ] **Step 2: Run and observe missing updater**

Run: `uv run --project agent pytest agent/tests/test_update.py -v`
Expected: FAIL importing updater.

- [ ] **Step 3: Implement inactive-slot update and readiness handshake**

Pull only the TUF-authorized OCI digest with the fixed ORAS boundary into an
inactive temp slot, verify target hash/length, fsync, atomic rename, persist activation
request, and ask supervisor to restart through a fixed local signal. New agent
writes readiness only after configuration/state migration, mTLS reconnect, and
self-test. Supervisor returns to previous slot on deadline or process failure.
`agent.rollback` selects only a verified recorded previous slot.

- [ ] **Step 4: Run agent update/supervisor tests**

Run: `uv run --project agent pytest agent/tests/test_update.py agent/tests/test_supervisor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit agent update**

```bash
git add agent/src/dgx_agent/update.py agent/src/dgx_agent/operations.py agent/supervisor/dgx-agent-supervisor agent/tests/test_update.py
git commit -m "feat: update Spark agents through A/B slots"
```

### Task 4: Topology-aware rollout planner and orchestrator

**Files:**
- Create: `control/src/dgx_control/updates.py`
- Modify: `control/src/dgx_control/models.py`
- Create: `control/migrations/versions/0006_update_rollouts.py`
- Test: `control/tests/test_updates.py`

**Interfaces:**
- Produces `UpdatePlanner.plan(release, fleet, topology, active_workloads, agents, policy) -> UpdatePlan` and `UpdateOrchestrator.advance(plan_id)`.
- Policy includes canary node, soak seconds, batch size default 1, minimum distributed replicas, and failure pause.

- [ ] **Step 1: Write failing fan-out tests**

Cover one/two/sixteen nodes, deterministic canary, explicit preferred canary,
retired/offline/incompatible agents, topology where peers cannot share batch,
distributed workload quorum, pause on first failure, soak clock, rollback,
operator approval resume, and no hard fleet limit.

- [ ] **Step 2: Run and observe missing planner**

Run: `uv run --project control pytest control/tests/test_updates.py -v`
Expected: FAIL importing updates.

- [ ] **Step 3: Implement persisted rollout plans and state machine**

Pin release/commit/fleet/topology/agent input digests. Select one canary, then
stable node-ID batches respecting workload availability and topology exclusion.
Withdraw/drain affected routes before each batch, enqueue `agent.update`, wait
for reconnect/new version/self-test, enforce soak, restore routes only after
acceptance, and pause on any failure. Resume after rollback requires stored
administrator approval and emits audit event.

- [ ] **Step 4: Run rollout and reconciliation interaction tests**

Run: `uv run --project control pytest control/tests/test_updates.py control/tests/test_agent_reconciliation.py -v`
Expected: PASS; update and profile reconciliation leases cannot overlap a node.

- [ ] **Step 5: Commit rollout orchestrator**

```bash
git add control/src/dgx_control/updates.py control/src/dgx_control/models.py control/migrations/versions/0006_update_rollouts.py control/tests/test_updates.py
git commit -m "feat: roll out Spark agent updates safely"
```

### Task 5: Update API, CLI, and web workflow

**Files:**
- Modify: `control/src/dgx_control/api.py`
- Modify: `src/spark_profiles/control_client.py`
- Modify: `src/spark_profiles/cli.py`
- Create: `control/web/src/pages/updates.tsx`
- Test: `control/tests/test_update_api.py`
- Test: `tests/spark_profiles/test_update_cli.py`
- Test: `control/web/src/pages/updates.test.tsx`

**Interfaces:**
- API `/api/v1/updates/plan`, `/updates`, `/updates/{id}`, `/updates/{id}/approve-resume`.
- CLI commands exactly match the agent design specification.

- [ ] **Step 1: Write failing equivalence and authorization tests**

Assert CLI/web plan digest equality, plan defaults, operator can plan/apply,
administrator-only resume after rollback, stale digest rejection, audit events,
and no endpoint accepting a control-host self-update online.

- [ ] **Step 2: Run and observe missing update interfaces**

Run: `uv run --project control pytest control/tests/test_update_api.py -v && uv run pytest tests/spark_profiles/test_update_cli.py -v && npm --prefix control/web test -- --run src/pages/updates.test.tsx`
Expected: FAIL with missing routes/commands/page.

- [ ] **Step 3: Implement thin adapters over update services**

Render canary/batches, compatibility, affected workloads/routes, soak, gates,
and rollback state. Apply requires exact digest. CLI supports `plan`, `apply`,
and `status`; web adds confirmation and administrator resume. Never expose
artifact credentials or agent certificate material.

- [ ] **Step 4: Run interface tests/build**

Run: `uv run --project control pytest control/tests/test_update_api.py -q && uv run pytest tests/spark_profiles/test_update_cli.py -q && npm --prefix control/web test -- --run && npm --prefix control/web run build`
Expected: PASS.

- [ ] **Step 5: Commit update UX**

```bash
git add control/src/dgx_control/api.py src/spark_profiles/control_client.py src/spark_profiles/cli.py control/web/src/pages/updates.tsx control/tests/test_update_api.py tests/spark_profiles/test_update_cli.py control/web/src/pages/updates.test.tsx
git commit -m "feat: administer staged platform updates"
```

### Task 6: Update recovery and acceptance evidence

**Files:**
- Create: `docs/runbooks/platform-release-update.md`
- Create: `scripts/accept-platform-update`
- Create: `tests/e2e/test_platform_update.py`
- Modify: `scripts/verify-platform-release`
- Modify: `schemas/platform-release-evidence.schema.json`

**Interfaces:**
- Acceptance covers control host old->new->rollback, canary failure, agent A/B rollback, resumed rollout, and final fleet/model verification.

- [ ] **Step 1: Write failing aggregate gate test**

Require content-addressed update report with explicit simulated/physical fields;
release verifier must remain blocked without physical control-host recovery,
physical canary/rollback, and signed manifest evidence.

- [ ] **Step 2: Run and observe absent update gate**

Run: `uv run pytest tests/e2e/test_platform_update.py tests/scripts/test_verify_platform_release.py -v`
Expected: FAIL missing script/report requirement.

- [ ] **Step 3: Implement simulator acceptance and runbook**

Exercise real manifest loader, host generation state machine fakes, agent slots,
rollout planner, API/CLI, and failure injection. Document download/offline media,
backup, canary selection, pause/resume, rollback, SSH recovery, and evidence
sanitization. Cross-link DGX OS maintenance and explain the fixed NVIDIA
`spark_updatectl.py` evidence boundary so operators cannot confuse host
firmware/kernel maintenance with DGX-Forge TUF/OCI fan-out. Never convert
simulated evidence into a physical pass.

- [ ] **Step 4: Run Phase 6 verification**

Run: `uv run pytest tests/spark_profiles/test_platform_release.py tests/e2e/test_platform_update.py tests/scripts/test_verify_platform_release.py -q && uv run --project control pytest control/tests/test_upgrade.py control/tests/test_updates.py control/tests/test_update_api.py -q && uv run --project agent pytest agent/tests/test_update.py agent/tests/test_supervisor.py -q && scripts/accept-platform-update --json && git diff --check`
Expected: implementation suites pass; first-release verifier remains blocked only on explicit external gates.

- [ ] **Step 5: Commit update acceptance**

```bash
git add docs/runbooks/platform-release-update.md scripts/accept-platform-update tests/e2e/test_platform_update.py scripts/verify-platform-release schemas/platform-release-evidence.schema.json
git commit -m "test: gate staged DGX-Forge platform updates"
```
