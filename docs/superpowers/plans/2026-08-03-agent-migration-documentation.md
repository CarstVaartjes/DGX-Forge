# Agent Migration and Public Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate existing GPU nodes from direct SSH control to outbound agents, preserve explicit recovery paths, and make the generic workflow clear to outside operators.

**Architecture:** Migration is evidence-driven and reversible until agent acceptance. Public docs use a guided entry point; historical fixed-node material is scope-labelled rather than rewritten, and automated checks protect navigation/configuration coverage.

**Tech Stack:** Markdown, Python documentation checks, existing onboarding/recovery scripts, agent/control acceptance suites, pytest

## Global Constraints

- Documentation must distinguish implemented behavior, migration-only compatibility, and uncompleted physical release gates.
- Routine production operations are documented only through CLI/web -> API -> outbound agent.
- SSH is documented only for enrollment, repair/replacement, certificate/A-B recovery, and emergency rollback.
- Examples contain no real users, addresses, hostnames, tokens, keys, or fixed fleet-size assumptions.
- Historical evidence remains factually unchanged and receives a visible scope label where necessary.
- Fresh/reimaged devices follow NVIDIA's cloud-init/OEMDATA contract; already-running devices may use the one-time SSH bootstrap. Canonical Landscape and NVIDIA Sync remain optional integrations, never required authorities.

---

### Task 1: Agent migration planner and per-node journal

**Files:**
- Create: `src/cluster_profiles/agent_migration.py`
- Modify: `src/cluster_profiles/install/orchestrator.py`
- Modify: `src/cluster_profiles/install/cli.py`
- Create: `deploy/ansible/roles/vonk_agent/`
- Create: `src/cluster_profiles/install/cloud_init.py`
- Test: `tests/cluster_profiles/install/test_agent_migration.py`
- Test: `tests/cluster_profiles/install/test_cloud_init.py`

**Interfaces:**
- Produces `AgentMigrationPlan` and resumable steps: preflight, grant, install, connect, evidence approval, certificate, probe, routine-transport acceptance, SSH-disable-for-routine marker.
- CLI: `node-install node enroll-agent NODE_ID [--apply] [--json]`.
- CLI: `node-install node prepare-media --baseos-release RELEASE --output DIR [--apply] [--json]` emits a sanitized, versioned seed overlay for NVIDIA's documented BaseOS/FastOS/OEMDATA workflow; it never writes an ISO or removable device itself.

- [ ] **Step 1: Write failing resume/rollback tests**

Test one stable node ID at a time, unavailable control plane, grant expiry,
installer failure, pending approval, certificate success, agent probe failure,
resume after restart, and explicit rollback that removes only incomplete agent
state without altering existing emergency SSH access.
Test canonical cloud-init seed generation, no embedded name/address/secret,
exact policy/tool digests, plan mode with no filesystem mutation, unsafe output
targets, and a reimaged node resuming at physical identity approval rather than
silently inheriting its old certificate.

- [ ] **Step 2: Run and observe absent migration**

Run: `uv run pytest tests/cluster_profiles/install/test_agent_migration.py -v`
Expected: FAIL importing migration planner/CLI command.

- [ ] **Step 3: Implement journaled migration gates**

Express agent user, directories, systemd units, pinned ORAS/Alloy/exporter
packages, the pinned NVIDIA lifecycle bundle, and local policy as versioned
idempotent Ansible roles. Invoke them
locally on the GPU node through a pinned Ansible Runner bundle; bootstrap SSH only
transfers/starts that bounded bundle and does not run repository-selected shell.
Extend existing install journal without changing immutable node ID. Each step
records canonical evidence and expected digest; approval is an external wait
state. Only after an authenticated agent probe succeeds does the fleet record
gain accepted agent protocol/version metadata. Do not delete recovery SSH.
For fresh/reimage mode, generate only the Vonk Forge cloud-init/OEMDATA overlay
consumed by NVIDIA's documented image tooling. Require the operator-supplied
NVIDIA BaseOS/FastOS media outside the repository, verify its declared digest,
and pause after first boot for the same physical identity/evidence gate. Do not
fork NVIDIA's installer scripts, bypass license acknowledgement, or write block
devices.

- [ ] **Step 4: Run onboarding and migration suites**

Run: `uv run pytest tests/cluster_profiles/install/test_agent_migration.py tests/cluster_profiles/install/test_cloud_init.py tests/cluster_profiles/install/test_install_cli.py tests/cluster_profiles/install/test_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit migration mode**

```bash
git add src/cluster_profiles/agent_migration.py src/cluster_profiles/install/orchestrator.py src/cluster_profiles/install/cli.py src/cluster_profiles/install/cloud_init.py deploy/ansible/roles/vonk_agent tests/cluster_profiles/install/test_agent_migration.py tests/cluster_profiles/install/test_cloud_init.py
git commit -m "feat: migrate GPU nodes to outbound agents"
```

### Task 2: Explicit agent repair and replacement tooling

**Files:**
- Create: `bin/vonk-agent-repair`
- Create: `src/cluster_profiles/agent_recovery.py`
- Test: `tests/cluster_profiles/test_agent_recovery.py`
- Create: `docs/runbooks/agent-recovery.md`

**Interfaces:**
- Commands: `inspect`, `repair-agent`, `repair-supervisor`, `reenroll`, `rollback-slot`, and `replace-node`; mutations require `--apply` and verified artifacts/evidence.

- [ ] **Step 1: Write failing recovery-boundary tests**

Assert plan mode makes no SSH call; apply uses strict host keys and dedicated
recovery identity; no arbitrary remote command argument; artifacts are pinned;
reenroll revokes old certificate; replacement requires new hardware evidence;
logs redact grant/token/key material.

- [ ] **Step 2: Run and observe missing tool**

Run: `uv run pytest tests/cluster_profiles/test_agent_recovery.py -v`
Expected: FAIL because launcher/module are absent.

- [ ] **Step 3: Implement fixed recovery operations over audited SSH adapter**

Reuse hardened SSH transport but map subcommands to fixed installed scripts and
typed parameters. Generate bounded local evidence importable through admin API.
Never expose a generic remote shell. Replacement preserves/changes node ID only
according to explicit fleet identity policy and revokes prior certificate.

- [ ] **Step 4: Run recovery tests and redaction checks**

Run: `uv run pytest tests/cluster_profiles/test_agent_recovery.py tests/cluster_profiles/install/test_remote.py -v`
Expected: PASS.

- [ ] **Step 5: Commit recovery tooling**

```bash
git add bin/vonk-agent-repair src/cluster_profiles/agent_recovery.py tests/cluster_profiles/test_agent_recovery.py docs/runbooks/agent-recovery.md
git commit -m "feat: repair GPU node agents through explicit recovery"
```

### Task 3: Rewrite README as generic guided entry point

**Files:**
- Modify: `README.md`
- Test: `tests/runbooks/test_public_docs.py`

**Interfaces:**
- README journey: service host -> first node -> agent approval -> repeat nodes -> models/profiles -> reconcile -> operate/update -> first-release gates.

- [ ] **Step 1: Write failing reader-contract tests**

```python
def test_readme_links_complete_generic_journey() -> None:
    links = markdown_links(Path("README.md").read_text())
    assert REQUIRED_START_HERE <= links

def test_recommended_readme_does_not_require_exactly_two_nodes() -> None:
    recommended = section("README.md", "Recommended workflow")
    assert "both configured nodes" not in recommended
    assert "inventory/cluster.toml" not in recommended
```

Required links include architecture, control bootstrap, Compose reference,
node onboarding, agent PKI/enrollment, platform operations, repository admin,
updates, observability, recovery, supply chain, NVIDIA lifecycle/cloud-init
integration, and legacy scope.

- [ ] **Step 2: Run and observe current README gaps**

Run: `uv run pytest tests/runbooks/test_public_docs.py -v`
Expected: FAIL missing links and containing fixed two-node quick-start language.

- [ ] **Step 3: Rewrite README around roles and newcomer sequence**

Lead with any-count GPU nodes plus generic Docker host and separate services.
Explain one production path, bootstrap/recovery SSH exception, Git authority,
CLI/web parity, model data not transiting NAS, current release status, quick
local development, grouped documentation, legacy compatibility, security, and
license. Keep commands executable and links relative.

- [ ] **Step 4: Run public-doc tests**

Run: `uv run pytest tests/runbooks/test_public_docs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit README**

```bash
git add README.md tests/runbooks/test_public_docs.py
git commit -m "docs: guide generic Vonk Forge operators"
```

### Task 4: Complete Compose configuration reference

**Files:**
- Create: `docs/runbooks/compose-configuration.md`
- Modify: `docs/runbooks/control-plane-bootstrap.md`
- Test: `tests/runbooks/test_compose_docs.py`

**Interfaces:**
- Reference covers every variable in `.env.example`, every Compose secret, service, volume, network, published port, startup order, health check, and backup owner.

- [ ] **Step 1: Write failing coverage test derived from configuration**

Parse `.env.example` keys and Compose `secrets`, then assert each appears as a
Markdown code token in the reference. Assert image values require `@sha256`,
agent CA roles are distinguished, only Caddy publishes ports, and example
render/start/health commands exist.

- [ ] **Step 2: Run and observe missing reference**

Run: `uv run pytest tests/runbooks/test_compose_docs.py -v`
Expected: FAIL because the runbook is absent.

- [ ] **Step 3: Write configuration and bootstrap sequence**

Group image digests, host paths/binds, secret files, Git policy, PKI, services,
networks, volumes, preflight, migration/admin bootstrap, startup, health,
backup, update, and failure recovery. Correct the obsolete bootstrap claim that
LiteLLM/Prometheus/Grafana arrive in a later phase; they are separate current
services.

- [ ] **Step 4: Run Compose documentation and rendered config tests**

Run: `uv run pytest tests/runbooks/test_compose_docs.py deploy/compose/tests -q`
Expected: PASS.

- [ ] **Step 5: Commit configuration docs**

```bash
git add docs/runbooks/compose-configuration.md docs/runbooks/control-plane-bootstrap.md tests/runbooks/test_compose_docs.py
git commit -m "docs: reference control-plane Compose configuration"
```

### Task 5: Current runbook journey and historical scope labels

**Files:**
- Modify: `docs/runbooks/platform-operations.md`
- Modify: `docs/runbooks/node-onboarding.md`
- Modify: `docs/runbooks/repository-administration.md`
- Modify: `docs/runbooks/vonkctl.md`
- Modify: `docs/runbooks/model-switching.md`
- Modify: `docs/runbooks/observability.md`
- Modify: `docs/runbooks/control-plane-recovery.md`
- Modify: `docs/runbooks/ssh-bootstrap.md`
- Modify: `docs/runbooks/ssh-recovery.md`
- Modify: `docs/runbooks/inventory.md`
- Modify: `docs/runbooks/fabric.md`
- Test: `tests/runbooks/test_documentation_scope.py`

**Interfaces:**
- Current docs link along recommended journey and state API/agent behavior.
- Historical/fixed-node docs begin with `> **Legacy two-GPU node scope:** ...` and link back to README migration guidance.

- [ ] **Step 1: Write failing scope/obsolete-language tests**

Current docs must not say `future NAS controller`, `both GPU nodes`, or direct SSH
for routine status/switch/deploy. Historical documents containing fixed
addresses/users/exact-two procedures must contain the legacy scope marker.
Check every local Markdown link resolves.

- [ ] **Step 2: Run and observe contradictions**

Run: `uv run pytest tests/runbooks/test_documentation_scope.py -v`
Expected: FAIL on current `vonkctl` and control bootstrap language.

- [ ] **Step 3: Update current workflow and label historical procedures**

Describe CLI/API/agent jobs, no fallback, agent update fan-out, route failure,
recovery tools, the pinned NVIDIA lifecycle-tool adapter, cloud-init versus
existing-node bootstrap, and optional Landscape/NVIDIA Sync boundaries. Add
“previous/next” links for newcomer stages. Preserve
historical commands/evidence beneath visible labels instead of changing their
recorded facts.

- [ ] **Step 4: Run all documentation tests**

Run: `uv run pytest tests/runbooks -q`
Expected: PASS.

- [ ] **Step 5: Commit runbook refresh**

```bash
git add docs/runbooks tests/runbooks/test_documentation_scope.py
git commit -m "docs: separate generic and legacy operations"
```

### Task 6: Physical migration and release gates

**Files:**
- Create: `scripts/accept-agent-migration`
- Create: `tests/e2e/test_agent_migration.py`
- Modify: `scripts/verify-platform-release`
- Modify: `docs/runbooks/platform-release-update.md`
- Modify: `inventory/reports/platform-release.json`

**Interfaces:**
- Acceptance report includes simulated status plus external gates for physical enrollment, routine no-SSH operation, disconnect/restart, certificate rotation/revocation, update/rollback, replacement, and emergency repair.
- It also records the pinned NVIDIA lifecycle bundle, one fresh/reimage cloud-init path, and one existing-node bootstrap path without treating either as proof of the other.

- [ ] **Step 1: Write failing release-gate test**

Assert verifier blocks when any physical agent gate is absent and refuses a
report claiming physical pass without operator, timestamp, release digest,
node IDs, evidence hashes, and sanitized command/result summaries.

- [ ] **Step 2: Run and observe missing gates**

Run: `uv run pytest tests/e2e/test_agent_migration.py tests/scripts/test_verify_platform_release.py -v`
Expected: FAIL missing script/schema gates.

- [ ] **Step 3: Implement simulated acceptance and external evidence schema**

Exercise install/enroll/approve/probe/CLI reconcile/update/revoke/repair using
simulators. Emit `remaining_release_gates` for every physical boundary. Update
release verifier to require accepted physical report and keep PR-only transition
blocked until protected code-host evidence also passes.

- [ ] **Step 4: Run complete implementation verification**

Run:

```bash
uv run pytest -q
uv run --project control pytest control/tests -q
uv run --project agent pytest agent/tests -q
npm --prefix control/web test -- --run
npm --prefix control/web run build
uv run pytest deploy/compose/tests tests/e2e/test_agent_migration.py -q
scripts/verify-supply-chain --json
scripts/accept-agent-migration --json
scripts/verify-platform-release --candidate 1.0.0 --json
git diff --check
```

Expected: implementation/test/build/supply-chain commands pass. Release
verification remains `blocked` until real hardware, recovery, and protected
code-host evidence is supplied; do not synthesize it.

- [ ] **Step 5: Commit migration acceptance**

```bash
git add scripts/accept-agent-migration tests/e2e/test_agent_migration.py scripts/verify-platform-release docs/runbooks/platform-release-update.md inventory/reports/platform-release.json
git commit -m "test: gate outbound agent production migration"
```
