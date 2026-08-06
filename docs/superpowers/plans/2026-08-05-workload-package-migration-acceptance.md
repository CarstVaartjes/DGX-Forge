# Workload Package Migration and Acceptance Implementation Plan

> **Implementation status (2026-08-06): complete locally.** W17–W20 are
> implemented on `main`, with generic Mia/DS4 projections, unknown-family E2E,
> failure/scale/security acceptance, operator documentation, and release
> evidence. Physical/protected-host release gates remain intentionally external;
> see the [roadmap status ledger](2026-08-05-generalized-workload-package-roadmap.md#implementation-status-2026-08-06).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate current Mia, DS4, and repository model state onto the generalized package path, prove an entirely unknown workload can be delivered without SSH or a DGX-Forge update, and make the result a first-release gate.

**Architecture:** Compatibility readers keep existing APIs and accepted evidence usable while generic family/release/deployment documents become the only newly authored form. Acceptance uses a synthetic upstream created after the installed agent build, exercises publication through NAS Git/TUF, direct Spark acquisition, activation, rollback, rejection, restart, GC, and no-SSH assertions. Final release verification consumes both workload-plane and platform-plane evidence without merging their trust or update mechanisms.

**Tech Stack:** Python 3.12, TOML/JSON, OCI test registry, local signed HTTP fixture, TUF, pytest, Docker Compose, Bash acceptance scripts

## Global Constraints

- No new Mia-, DS4-, model-, runtime-, or adapter-specific package-engine branch or agent operation may be introduced.
- Current accepted evidence is preserved; migration cannot claim new physical acceptance that was not observed.
- Legacy model/profile API behavior remains available until generic-path equivalence passes, then becomes read-only compatibility behavior.
- The decisive test uses a family, adapter digest, release, and deployment unknown when the installed DGX-Forge agent artifact was built.
- Ordinary new workload releases use workload Git/TUF/package operations only; they never use `agent.update`, platform TUF, or SSH.
- Tasks 32–37 cannot close the first-release gate until W17–W20 pass.

---

### Task W17: Mia, DS4, and model-definition migration

**Files:**
- Create: `config/package-families/mia-deepseek.toml`
- Create: `config/package-families/ds4-deepseek.toml`
- Create: `config/workload-deployments/mia-deepseek-dual.toml`
- Create: `config/workload-deployments/ds4-deepseek-single.toml`
- Create: `manifests/workload-releases/mia-deepseek/`
- Create: `manifests/workload-releases/ds4-deepseek/`
- Create: `src/spark_profiles/workload_packages/legacy.py`
- Modify: `src/spark_profiles/catalog.py`
- Modify: `control/src/dgx_control/desired_state.py`
- Test: `tests/spark_profiles/test_workload_package_migration.py`
- Test: `tests/adapters/test_mia_deepseek_dual.py`
- Test: `tests/adapters/test_ds4_runtime.py`

**Interfaces:**
- Produces `LegacyWorkloadReader.read(old_definition) -> WorkloadDeployment` for read-only compatibility and canonical generic family/release/deployment fixtures for Mia and DS4.
- Model weights, tokenizers, encoders, runtime source/container, Python environment, configuration, and adapter are ordinary component descriptors or dependency package releases.

- [ ] **Step 1: Write RED equivalence and no-special-case tests**

Assert current public profile/model IDs and maturity evidence project identically, definitions contain exact existing pins, Mia multi-node lifecycle and DS4 single-node placement remain unchanged, shared components deduplicate, and no source under `agent/src/dgx_agent/packages/` contains `mia`, `ds4`, `deepseek`, or any migrated model ID.

```python
legacy = LegacyWorkloadReader.read(old_definition)
generic = WorkloadDeployment.load(new_definition)
assert legacy.public_projection() == generic.public_projection()
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --frozen pytest tests/spark_profiles/test_workload_package_migration.py tests/adapters/test_mia_deepseek_dual.py tests/adapters/test_ds4_runtime.py -v`

Expected: FAIL because generic fixtures and compatibility reader are absent.

- [ ] **Step 3: Add exact generic fixtures and read-only compatibility**

Transcribe existing immutable source/image/checkpoint/environment pins into typed descriptors without changing them. Represent the Mia solution, runtime, primary model, and auxiliary assets as a bounded dependency graph; represent DS4 through the same schema. Keep old files readable but make repository editors write only generic documents.

```python
def read_legacy_workload(document: Mapping[str, object]) -> WorkloadDeployment:
    return LegacyWorkloadReader.read(document)
```

- [ ] **Step 4: Verify migration and existing catalog behavior**

Run: `uv run --frozen pytest tests/spark_profiles/test_workload_package_migration.py tests/spark_profiles/test_catalog.py tests/spark_profiles/test_phase4_model_catalog.py tests/adapters/test_mia_deepseek_dual.py tests/adapters/test_ds4_runtime.py -q`

Expected: PASS; DS4 remains `verified`, Mia retains its existing maturity, and no acceptance evidence is upgraded.

- [ ] **Step 5: Commit W17**

```bash
git add config/package-families config/workload-deployments manifests/workload-releases src/spark_profiles/workload_packages/legacy.py src/spark_profiles/catalog.py control/src/dgx_control/desired_state.py tests/spark_profiles/test_workload_package_migration.py tests/adapters/test_mia_deepseek_dual.py tests/adapters/test_ds4_runtime.py
git commit -m "feat: migrate Mia and DS4 to workload packages"
```

### Task W18: Unknown-package end-to-end acceptance

**Files:**
- Create: `tests/fixtures/workload-packages/synthetic-upstream/`
- Create: `tests/e2e/test_unknown_workload_package.py`
- Create: `scripts/accept-workload-packages`
- Modify: `tests/test_shell_suites.py`

**Interfaces:**
- The harness records installed agent/platform digest before creating the synthetic family, publishes signed Git/TUF state afterward, and asserts that digest never changes.
- Exercises discover, resolve, validate, promote, canary, prepare, activate, health, infer, update-to-second-release, rollback, and unsigned/unapproved rejection.

- [ ] **Step 1: Build a failing synthetic lifecycle test**

Generate two deterministic native/OCI fixture releases and an ABI-v1 adapter after capturing the installed agent artifact digest. Require direct Spark/provider bytes, exact locks, NAS promotion, durable progress, atomic generation, route publication, offline rollback, and audit evidence; monkeypatch every SSH entry point and `agent.update` to fail on invocation.

```python
assert agent_digest_after == agent_digest_before
assert ssh_calls == []
assert agent_update_calls == []
assert active_release == second_release.digest
```

- [ ] **Step 2: Run and observe the missing end-to-end path**

Run: `uv run --frozen pytest tests/e2e/test_unknown_workload_package.py -v`

Expected: FAIL before W1–W17 because generic publication/reconciliation is incomplete.

- [ ] **Step 3: Implement the hermetic acceptance harness**

Use disposable Git, TUF metadata, HTTPS/OCI provider fixtures, control database, agent state/store, and Spark simulator boundaries. Create the family only after the agent is instantiated; promote release 1, activate it, promote release 2, activate it, remove network access, roll back to release 1, then prove unsigned/revoked/unpromoted release rejection.

```bash
scripts/accept-workload-packages --mode simulated --json
```

- [ ] **Step 4: Verify repeated acceptance and shell integration**

Run: `uv run --frozen pytest tests/e2e/test_unknown_workload_package.py tests/test_shell_suites.py -q && scripts/accept-workload-packages --mode simulated --json`

Expected: PASS twice from fresh state and once from an interrupted retained state, with stable redacted evidence digests.

- [ ] **Step 5: Commit W18**

```bash
git add tests/fixtures/workload-packages/synthetic-upstream tests/e2e/test_unknown_workload_package.py scripts/accept-workload-packages tests/test_shell_suites.py
git commit -m "test: prove unknown workload package delivery"
```

### Task W19: Failure, scale, security, and recovery acceptance

**Files:**
- Create: `tests/e2e/test_workload_package_failure_matrix.py`
- Create: `tests/e2e/test_workload_package_scale.py`
- Modify: `tests/agent/test_failure_matrix.py`
- Modify: `tests/control/test_failure_injection.py`
- Modify: `tests/control/test_fleet_scale.py`
- Modify: `scripts/verify-supply-chain`

**Interfaces:**
- Acceptance covers one/two/sixteen nodes plus a generated larger fleet without a product limit, all failure taxonomy values, concurrent shared downloads, canary stop, and service/agent restarts.

- [ ] **Step 1: Write RED matrix and generated-fleet tests**

Exercise discovery outage, upstream mutation, unsupported resolution, trust/provenance/license rejection, platform incompatibility, missing credential, capacity failure, retryable transport, digest/size mismatch, environment build failure, validation/activation/health/rollback failure, corrupt store repair, cancellation, GC interruption, PostgreSQL/Caddy/control restart, agent restart, and concurrent requests for identical content.

```python
assert every_failure_has({"family_id", "release_digest", "node_id", "fence", "reason_code"})
assert no_failure_contains_secret()
```

- [ ] **Step 2: Run the RED acceptance tests**

Run: `uv run --frozen pytest tests/e2e/test_workload_package_failure_matrix.py tests/e2e/test_workload_package_scale.py -v`

Expected: FAIL until all taxonomy, concurrency, and recovery behavior is implemented.

- [ ] **Step 3: Close only reproduced generic gaps**

Fix missing behavior in the owning W1–W16 module; do not add model-specific branches or broaden the privileged ABI. Record exact retry/compensate/operator-intervention disposition, preserve active generations on pre-activation failure, pause rollout on first canary failure, and bound all diagnostics/labels.

```python
assert result.disposition in {"safe-to-retry", "compensate", "operator-intervention"}
```

- [ ] **Step 4: Run the complete workload failure and scale gate**

Run: `uv run --frozen pytest tests/e2e/test_workload_package_failure_matrix.py tests/e2e/test_workload_package_scale.py tests/agent/test_failure_matrix.py tests/control/test_failure_injection.py tests/control/test_fleet_scale.py -q && scripts/verify-supply-chain`

Expected: PASS with no fixed node/family/adapter catalog and no secret-bearing evidence.

- [ ] **Step 5: Commit W19**

```bash
git add tests/e2e/test_workload_package_failure_matrix.py tests/e2e/test_workload_package_scale.py tests/agent/test_failure_matrix.py tests/control/test_failure_injection.py tests/control/test_fleet_scale.py scripts/verify-supply-chain
git commit -m "test: harden workload package operations"
```

### Task W20: Operations docs and first-release gate

**Files:**
- Create: `docs/runbooks/workload-packages.md`
- Modify: `README.md`
- Modify: `docs/runbooks/platform-update.md`
- Modify: `docs/superpowers/plans/2026-08-03-platform-release-hardening.md`
- Modify: `scripts/verify-platform-release`
- Modify: `schemas/platform-release-evidence.schema.json`
- Test: `tests/scripts/test_verify_platform_release.py`
- Test: `tests/runbooks/test_workload_packages.py`

**Interfaces:**
- Documents family creation, candidate review, promotion, rollout, progress, rollback, repair, GC, credentials/licenses, offline recovery, and the platform/workload release boundary.
- First-release evidence requires the W18 unknown-package flow and W19 security/recovery matrix in addition to platform update evidence.

- [ ] **Step 1: Write RED documentation and release-gate tests**

Require README links and commands for both CLI/web, no-SSH normal operation, Git/TUF/OCI authority boundaries, model payload direct-fetch behavior, NAS backup scope, workload rollback without network, DGX-Forge skew prompt behavior, and explicit physical-vs-simulated evidence fields.

```python
assert evidence["workload_packages"]["unknown_family_without_agent_update"] is True
assert evidence["workload_packages"]["ssh_calls"] == 0
```

- [ ] **Step 2: Run the RED docs/release tests**

Run: `uv run --frozen pytest tests/scripts/test_verify_platform_release.py tests/runbooks/test_workload_packages.py -v`

Expected: FAIL because the workload runbook and release evidence requirements are absent.

- [ ] **Step 3: Write operator docs and aggregate evidence**

Explain that NAS Docker services update through the host-local platform updater, compatible old Spark agents remain operational, the web/CLI prompts for signed topology-aware `agent.update` fan-out when NAS is newer, and workload releases remain independent. Include preview/apply/status/rollback/repair/GC examples with exact digest confirmation and recovery-only SSH boundaries.

```text
sparkctl admin packages candidates list --family synthetic-stack --json
sparkctl admin packages promote --candidate 00000000-0000-4000-8000-000000000001 --preview-digest aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --json
sparkctl admin deployments rollout --deployment synthetic-canary --plan-digest bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --json
```

- [ ] **Step 4: Run full repository and hosted-equivalent gates**

Run: `uvx --from ruff==0.16.1 ruff check . && uv run --python 3.12 --frozen --with pytest==9.1.1 pytest && uv run --project agent --frozen pytest && uv run --project agent_protocol --frozen pytest && npm --prefix control/web test -- --run && npm --prefix control/web run build && scripts/accept-workload-packages --mode simulated --json && uv run --frozen pytest tests/scripts/test_verify_platform_release.py -q && git diff --check`

Expected: all automated gates pass; any unperformed physical Spark acceptance remains an explicit external release blocker rather than being synthesized.

- [ ] **Step 5: Commit W20**

```bash
git add docs/runbooks/workload-packages.md README.md docs/runbooks/platform-update.md docs/superpowers/plans/2026-08-03-platform-release-hardening.md scripts/verify-platform-release schemas/platform-release-evidence.schema.json tests/scripts/test_verify_platform_release.py tests/runbooks/test_workload_packages.py
git commit -m "docs: operate and gate workload packages"
```
