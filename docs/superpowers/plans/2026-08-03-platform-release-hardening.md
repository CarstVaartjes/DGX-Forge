# Platform Release Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the generic GPU node platform and control plane can be securely installed, upgraded, recovered, and released with PR-only repository mutation.

**Architecture:** Hardening is evidence-driven: executable threat assertions, dependency/image provenance, restore and host-loss drills, scale/failure tests, and a release gate aggregate signed artifacts into a checked-in acceptance report. The release transition enables irreversible PR-only mode only after all gates pass.

**Tech Stack:** pytest, Playwright, Docker Compose, Trivy/Grype or equivalent scanner, Syft SPDX SBOM, Cosign verification, PostgreSQL backup tools, JSON Schema.

## Global Constraints

- No release claim without a requirement-by-requirement evidence manifest.
- No private credentials, prompts, model responses, or raw sensitive identity in evidence.
- All production images and dependencies are locked and reproducible.
- Backup restoration must succeed on a different generic Docker-capable Linux host or disposable equivalent.
- Test one, two, sixteen, and a generated larger fleet; no node-count constant is acceptable.
- First real release enables protected deployment branch and irreversible PR-only mutation.
- Hardware-mutating acceptance requires explicit approved targets and preserved recovery access.

---

### Task 1: Add executable security policy and threat model

**Files:**
- Create: `docs/security/threat-model.md`
- Create: `control/tests/security/test_boundaries.py`
- Create: `control/tests/security/test_authorization_matrix.py`
- Create: `control/tests/security/test_untrusted_repository.py`

**Interfaces:**
- Threat boundaries: public ingress, admin browser, CLI token, Git remote, repository content, PostgreSQL, worker, SSH node command, backup storage.
- Security matrix maps viewer/operator/administrator to every API mutation.

- [ ] **Step 1: Write failing boundary assertions**

```python
def test_repository_content_cannot_select_executable_or_network_target(security_harness):
    commit = security_harness.commit_profile(adapter="../../bin/sh", upstream="http://attacker.invalid")
    result = security_harness.plan(commit)
    assert not result.ok


def test_every_mutating_route_has_explicit_role(api_schema, authorization_matrix):
    routes = mutating_routes(api_schema)
    assert routes == set(authorization_matrix)
```

- [ ] **Step 2: Run and identify uncovered boundaries**

Run: `uv run --project control pytest control/tests/security -v`
Expected: FAIL until policies and matrix cover every route.

- [ ] **Step 3: Complete threat model and enforce missing validations**

For each boundary record assets, attacker, entry points, prevention, detection, recovery, and executable test. Add only targeted enforcement required by failing assertions; preserve typed allowlists and safe argv execution.

- [ ] **Step 4: Run security suite**

Run: `uv run --project control pytest control/tests/security -v`
Expected: PASS.

- [ ] **Step 5: Commit threat model**

```bash
git add docs/security control/tests/security control/src
git commit -m "security: enforce control plane threat boundaries"
```

### Task 2: Lock dependencies, images, SBOMs, and provenance

**Files:**
- Modify: `control/uv.lock`
- Modify: `control/web/package-lock.json`
- Modify: `deploy/compose/compose.yaml`
- Create: `scripts/verify-supply-chain`
- Create: `tests/scripts/test_verify_supply_chain.py`
- Create: `docs/runbooks/supply-chain.md`

**Interfaces:**
- `scripts/verify-supply-chain --json` checks no floating image tags, expected image digests/signatures, locked Python/npm dependencies, and generated SPDX SBOM digests.

- [ ] **Step 1: Write failing floating-tag and changed-SBOM tests**

```python
def test_verifier_rejects_floating_image(tmp_repository):
    tmp_repository.replace_image("caddy:latest")
    result = tmp_repository.verify_supply_chain()
    assert result.returncode != 0
    assert "digest" in result.stderr


def test_verifier_rejects_stale_sbom(repository):
    repository.change_locked_dependency()
    assert repository.verify_supply_chain().returncode != 0
```

- [ ] **Step 2: Run and observe missing verifier**

Run: `uv run pytest tests/scripts/test_verify_supply_chain.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement offline-verifiable manifest and SBOM checks**

Parse rendered Compose images, require `@sha256:`, verify configured signatures when publisher support exists, generate SPDX JSON for the control image and web bundle, compare manifest digests, and report structured failures without network during normal CI verification.

- [ ] **Step 4: Run tests and verifier**

Run: `uv run pytest tests/scripts/test_verify_supply_chain.py -v && scripts/verify-supply-chain --json`
Expected: PASS.

- [ ] **Step 5: Commit supply-chain evidence**

```bash
git add control deploy scripts/verify-supply-chain tests/scripts/test_verify_supply_chain.py docs/runbooks/supply-chain.md
git commit -m "security: lock platform supply chain"
```

### Task 3: Prove upgrade, rollback, backup, and host-loss recovery

**Files:**
- Create: `tests/control/test_upgrade_recovery.py`
- Create: `scripts/accept-control-recovery`
- Modify: `docs/runbooks/control-plane-recovery.md`
- Create: `inventory/reports/control-plane-recovery.json`

**Interfaces:**
- Acceptance creates versioned JSON with source/target versions, backup digest, restore host facts, migration results, audit counts, route state, and timestamps.

- [ ] **Step 1: Write failing old-to-new-to-rollback test**

```python
def test_upgrade_failure_restores_previous_known_good_stack(recovery_harness):
    baseline = recovery_harness.deploy_previous()
    backup = recovery_harness.backup()
    recovery_harness.deploy_candidate(fail_migration=True)
    restored = recovery_harness.restore_previous(backup)
    assert restored.audit_digest == baseline.audit_digest
    assert restored.routes == "maintenance"
```

- [ ] **Step 2: Run and observe absent recovery harness**

Run: `uv run pytest tests/control/test_upgrade_recovery.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement disposable two-host recovery acceptance**

Deploy baseline, create representative state, encrypt backup, destroy stack and volumes, restore on a second clean Docker context, apply migrations, verify users/jobs/audit/repository commit, keep inference maintenance until health, then record sanitized evidence.

- [ ] **Step 4: Run recovery acceptance**

Run: `uv run pytest tests/control/test_upgrade_recovery.py -v && scripts/accept-control-recovery --output inventory/reports/control-plane-recovery.json`
Expected: PASS with schema-valid report.

- [ ] **Step 5: Commit recovery evidence**

```bash
git add tests/control scripts/accept-control-recovery docs/runbooks/control-plane-recovery.md inventory/reports/control-plane-recovery.json
git commit -m "test: prove control plane disaster recovery"
```

### Task 4: Run fleet scale and failure-injection acceptance

**Files:**
- Create: `tests/control/test_fleet_scale.py`
- Create: `tests/control/test_failure_injection.py`
- Create: `inventory/reports/control-plane-scale.json`

**Interfaces:**
- Scale cases: 1, 2, 16, and 64 simulated nodes.
- Failure cases: database restart, worker kill, Caddy invalid candidate, LiteLLM failure, Git outage, SSH timeout, service-host restart.

- [ ] **Step 1: Write failing parameterized scale and failure tests**

```python
@pytest.mark.parametrize("nodes", [1, 2, 16, 64])
def test_fleet_operations_have_no_fixed_node_limit(platform_harness, nodes):
    result = platform_harness.reconcile_simulated_fleet(nodes)
    assert result.planned_nodes == nodes
    assert result.duplicate_mutations == 0


@pytest.mark.parametrize("fault", ["postgres", "worker", "caddy", "litellm", "git", "ssh", "host"])
def test_fault_never_publishes_unhealthy_route(platform_harness, fault):
    result = platform_harness.inject(fault)
    assert result.affected_routes == "maintenance"
```

- [ ] **Step 2: Run and observe unimplemented harness/failure behavior**

Run: `uv run pytest tests/control/test_fleet_scale.py tests/control/test_failure_injection.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement deterministic simulators and Compose fault controls**

Use fake SSH nodes for fleet scale and disposable Compose controls for service faults. Record operation latency, queue recovery, duplicate mutation count, terminal job state, and route state in sanitized JSON.

- [ ] **Step 4: Run acceptance and validate report**

Run: `uv run pytest tests/control/test_fleet_scale.py tests/control/test_failure_injection.py -v`
Expected: PASS and report includes all node counts/faults.

- [ ] **Step 5: Commit scale/failure evidence**

```bash
git add tests/control inventory/reports/control-plane-scale.json
git commit -m "test: accept fleet scale and service failures"
```

### Task 5: Exercise end-to-end node-to-profile lifecycle

**Files:**
- Create: `tests/e2e/test_platform_lifecycle.py`
- Create: `inventory/reports/platform-lifecycle.json`
- Create: `docs/runbooks/platform-operations.md`

**Interfaces:**
- Workflow: onboard fresh simulated/approved GPU node, propose record, merge, create/update model/profile, merge, reconcile, serve, observe, withdraw, and audit.

- [ ] **Step 1: Write failing full workflow test**

```python
def test_repository_to_running_profile_and_safe_withdrawal(platform):
    node = platform.onboard_new_node(host="dynamic.local", user="operator")
    platform.merge(platform.proposal_for(node))
    profile = platform.merge(platform.propose_profile(node_id=node.id))
    deployment = platform.reconcile(profile.commit)
    assert platform.infer("test-model").status_code == 200
    platform.withdraw(profile)
    assert platform.infer("test-model").status_code == 503
    assert platform.audit_covers(deployment)
```

- [ ] **Step 2: Run and observe incomplete cross-component behavior**

Run: `uv run pytest tests/e2e/test_platform_lifecycle.py -v`
Expected: FAIL.

- [ ] **Step 3: Complete only missing integration seams and record evidence**

Run against simulated nodes by default and explicitly approved hardware for the final installation acceptance. Require dynamic host/name input, canonical PR, eligible merge, exact placement, healthy route, metrics, job/audit linkage, and fail-closed withdrawal.

- [ ] **Step 4: Run end-to-end and full repository suites**

Run: `uv run pytest -q && uv run --project control pytest -q && npm --prefix control/web test -- --run && npm --prefix control/web run test:e2e && uv run pytest tests/e2e/test_platform_lifecycle.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit lifecycle evidence**

```bash
git add tests/e2e inventory/reports/platform-lifecycle.json docs/runbooks/platform-operations.md
git commit -m "test: accept repository-driven platform lifecycle"
```

### Task 6: Aggregate release gates and enable PR-only mode

The aggregate is a two-plane first-release gate. In addition to the platform
reports below, it consumes the independent workload acceptance artifacts
`inventory/reports/workload-package-acceptance.json` and
`inventory/reports/workload-package-failure-matrix.json`. The former must prove
an unknown family can activate release 2, roll back to release 1 while
offline, and reject unsigned/unapproved releases without SSH or `agent.update`;
the latter must prove the generic failure/recovery matrix with secret-free,
typed dispositions. A workload artifact is never treated as a platform image
or as evidence of physical GPU node acceptance.

**Files:**
- Create: `scripts/verify-platform-release`
- Create: `tests/scripts/test_verify_platform_release.py`
- Create: `schemas/platform-release-evidence.schema.json`
- Create: `inventory/reports/platform-release.json`
- Modify: `README.md`

**Interfaces:**
- `scripts/verify-platform-release --candidate VERSION --json` verifies every required report, test command, lock, migration, backup age, and Git protection assertion.
- Only a successful aggregate job may call `enable_release_pr_only`.

- [ ] **Step 1: Write failing missing-gate and irreversible-transition tests**

```python
def test_release_verifier_lists_every_missing_gate(release_repository):
    release_repository.remove_report("control-plane-recovery")
    result = release_repository.verify_release("1.0.0")
    assert result.returncode != 0
    assert "control-plane-recovery" in result.stderr


def test_pr_only_transition_requires_successful_release_digest(policy, failed_report):
    with pytest.raises(ReleaseGateError):
        policy.enable_release_pr_only(release_digest=failed_report.digest)
```

- [ ] **Step 2: Run and observe missing verifier/schema**

Run: `uv run pytest tests/scripts/test_verify_platform_release.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement aggregate evidence verifier and release transition**

Validate report schema/digests/timestamps, exact version, supply-chain output, recovery/scale/lifecycle results, protected branch and required checks, and clean Git tree. Emit one signed or content-addressed release evidence document; then enable PR-only mode through an audited administrator operation.

The verifier records workload evidence separately under `workload_packages` and
fails closed when either independent report is absent, malformed, unsigned,
secret-bearing, or missing the release-2/rollback/rejection assertions. It must
not execute an acceptance run as a substitute for a checked-in, content-
addressed report; hosted CI uploads the run output and the release job verifies
that exact digest.

- [ ] **Step 4: Run complete release verification**

Run: `uv run pytest -q && uv run --project control pytest -q && npm --prefix control/web test -- --run && scripts/verify-supply-chain --json && scripts/verify-platform-release --candidate 1.0.0 --json && git diff --check`
Expected: all commands PASS and `inventory/reports/platform-release.json` validates.

- [ ] **Step 5: Commit first release gates**

```bash
git add scripts/verify-platform-release tests/scripts/test_verify_platform_release.py schemas/platform-release-evidence.schema.json inventory/reports/platform-release.json README.md
git commit -m "release: accept Vonk Forge platform 1.0.0"
```
