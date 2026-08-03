# Agent-Based Control Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the control plane decompose repository plans into node-agent operations and eliminate routine SSH execution from the production worker.

**Architecture:** Reconciliation is a persisted orchestration state machine. It validates an eligible Git commit, withdraws affected routes, emits dependency-ordered node operations, consumes fenced results, performs acceptance, and atomically publishes routes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/PostgreSQL, existing placement/profile contracts, pytest

## Global Constraints

- Production routine operations use only `AgentJobService`; no subprocess, SSH, SCP, or direct agent connection is available to the worker.
- Plans pin commit, targets, placements, releases, routes, input digests, operation graph, agent protocol range, and plan digest.
- A failed/unavailable/revoked/incompatible node leaves affected routes withdrawn.
- Agent operations are safe under lease expiry through explicit inspection, compensation, or operator-wait states.
- Legacy SSH transport is accessible only through an explicitly selected compatibility entry point outside production settings.

---

### Task 1: Persisted reconciliation operation graph

**Files:**
- Create: `control/src/dgx_control/orchestration.py`
- Modify: `control/src/dgx_control/models.py`
- Create: `control/migrations/versions/0004_reconciliation_graph.py`
- Test: `control/tests/test_orchestration.py`

**Interfaces:**
- Produces `OperationNode`, `OperationGraph`, `ReconciliationOrchestrator.plan`, `advance`, `cancel`.
- Graph nodes contain operation ID, node ID, kind, dependencies, compensation kind, and exact payload digest.

- [ ] **Step 1: Write failing deterministic graph tests**

```python
def test_workers_start_before_entrypoint_and_stop_after_it(planner) -> None:
    graph = planner.plan(distributed_plan())
    assert graph.dependencies("head:start") == ("worker:start",)
    assert graph.dependencies("worker:stop") == ("head:stop",)
    assert graph.digest == planner.plan(distributed_plan()).digest
```

- [ ] **Step 2: Run and observe missing graph**

Run: `uv run --project control pytest control/tests/test_orchestration.py -v`
Expected: FAIL importing orchestration.

- [ ] **Step 3: Implement graph contracts and persistence**

Persist graph JSON/digest, current phase, route-withdrawal generation, and
terminal reason on `reconciliations`; add dependency rows or canonical JSON as
one immutable graph document. Reject cycles, unknown targets, duplicate
operations, cross-workload ordering errors, and operations absent from the
agent registry. Deterministically sort independent nodes by canonical ID.

- [ ] **Step 4: Run graph and migration tests**

Run: `uv run --project control pytest control/tests/test_orchestration.py control/tests/test_agent_migrations.py -v`
Expected: PASS.

- [ ] **Step 5: Commit graph**

```bash
git add control/src/dgx_control/orchestration.py control/src/dgx_control/models.py control/migrations/versions/0004_reconciliation_graph.py control/tests/test_orchestration.py
git commit -m "feat: persist agent reconciliation graphs"
```

### Task 2: Repository-to-agent plan resolver

**Files:**
- Create: `control/src/dgx_control/desired_state.py`
- Modify: `control/src/dgx_control/reconcile.py`
- Test: `control/tests/test_desired_state.py`
- Test: `control/tests/test_reconcile.py`

**Interfaces:**
- Produces `DesiredStateResolver.resolve(commit, profile_id, observations) -> ReconciliationPlan`.
- Consumes fleet/topology V2, workload/profile definitions, placement planner, immutable release manifests, and ready agent capabilities.

- [ ] **Step 1: Write failing one/two/sixteen-node resolution tests**

Test exact document hashes, missing references, stale observations, insufficient
capacity, incompatible agent versions, unsupported operations, and stable
placement under reordered repository documents.

- [ ] **Step 2: Run and observe current static reconciliation document limitation**

Run: `uv run --project control pytest control/tests/test_desired_state.py control/tests/test_reconcile.py -v`
Expected: FAIL because production planning only reads `inventory/reconciliation.json`.

- [ ] **Step 3: Implement derived desired-state planning**

Read exact commit objects through `RepositoryService`; validate V2 schemas and
cross-references; convert DB observations to `NodeObservation`; run
`PlacementPlanner`; resolve release and route inputs; require connected agent
protocol/capability compatibility; emit a complete operation graph. Keep static
reconciliation documents only as a test fixture/explicit compatibility format.

- [ ] **Step 4: Run resolver, placement, and repository tests**

Run: `uv run --project control pytest control/tests/test_desired_state.py control/tests/test_reconcile.py control/tests/test_repository.py -v && uv run pytest tests/spark_profiles/test_placement.py -v`
Expected: PASS.

- [ ] **Step 5: Commit resolver**

```bash
git add control/src/dgx_control/desired_state.py control/src/dgx_control/reconcile.py control/tests/test_desired_state.py control/tests/test_reconcile.py
git commit -m "feat: derive agent plans from repository state"
```

### Task 3: Fail-closed route and operation orchestration

**Files:**
- Modify: `control/src/dgx_control/orchestration.py`
- Modify: `control/src/dgx_control/routes.py`
- Modify: `control/src/dgx_control/litellm.py`
- Test: `control/tests/test_agent_reconciliation.py`

**Interfaces:**
- Orchestrator enqueues only dependency-ready operations and advances from persisted state after each terminal result.
- Route publisher consumes accepted endpoint evidence only after graph verification succeeds.

- [ ] **Step 1: Write failing lifecycle/fault tests**

Test route withdrawal precedes first mutation; release install precedes prepare;
worker start precedes head; all health/verify results precede publication;
disconnect/revocation/stale fence/bad evidence leave maintenance; retry after
restart does not duplicate completed mutation.

- [ ] **Step 2: Run and observe missing agent-driven lifecycle**

Run: `uv run --project control pytest control/tests/test_agent_reconciliation.py -v`
Expected: FAIL because current runtime handler shells to `sparkctl`.

- [ ] **Step 3: Implement persisted advancement and compensation**

Within transactions, find dependency-ready nodes, enqueue one node operation
each, and record IDs. When results arrive, validate evidence digest and advance.
On start/verify failure enqueue stop compensation for successfully started
members; on uncertain mutation enter `waiting-for-operator` after inspection.
Publish `RouteCandidate` and LiteLLM policy atomically only after complete
acceptance. Record bounded audit/reconciliation summary.

- [ ] **Step 4: Run lifecycle and route tests**

Run: `uv run --project control pytest control/tests/test_agent_reconciliation.py control/tests/test_routes.py control/tests/test_litellm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit orchestration**

```bash
git add control/src/dgx_control/orchestration.py control/src/dgx_control/routes.py control/src/dgx_control/litellm.py control/tests/test_agent_reconciliation.py
git commit -m "feat: reconcile cluster through outbound agents"
```

### Task 4: Remove SSH from production worker wiring

**Files:**
- Modify: `control/src/dgx_control/worker.py`
- Delete: routine subprocess behavior from `control/src/dgx_control/runtime.py`
- Create: `control/src/dgx_control/legacy_runtime.py`
- Modify: `control/src/dgx_control/settings.py`
- Test: `control/tests/test_production_worker.py`
- Test: `control/tests/security/test_no_routine_ssh.py`

**Interfaces:**
- Production worker registry contains orchestration/maintenance tasks only and emits agent operations.
- Legacy runtime requires `DGX_LEGACY_DIRECT_TRANSPORT=explicit-test-only` and is rejected in production mode.

- [ ] **Step 1: Write failing production-boundary tests**

Patch `subprocess.run/Popen` and transport constructors to raise if production
worker handles probe/reconcile. Assert operations are inserted in the database
instead. Assert production settings reject the legacy selector and no automatic
fallback occurs when agents are offline.

- [ ] **Step 2: Run and confirm current SSH subprocess handler fails test**

Run: `uv run --project control pytest control/tests/test_production_worker.py control/tests/security/test_no_routine_ssh.py -v`
Expected: FAIL because `RuntimeHandlers` invokes repository scripts.

- [ ] **Step 3: Wire agent orchestrator and isolate legacy implementation**

Move direct handlers to `legacy_runtime.py`; do not import it from production
API/worker modules. Production `Worker` advances persisted reconciliations and
performs housekeeping only. Agent HTTP claims execute node work. Make settings
reject compatibility transport in `production` deployment mode.

- [ ] **Step 4: Run worker, job, and security suites**

Run: `uv run --project control pytest control/tests/test_production_worker.py control/tests/test_worker.py control/tests/test_jobs.py control/tests/security/test_no_routine_ssh.py -v`
Expected: PASS.

- [ ] **Step 5: Commit transport cutover**

```bash
git add control/src/dgx_control/worker.py control/src/dgx_control/runtime.py control/src/dgx_control/legacy_runtime.py control/src/dgx_control/settings.py control/tests/test_production_worker.py control/tests/security/test_no_routine_ssh.py
git commit -m "refactor: remove routine SSH from control worker"
```

### Task 5: Metrics and operational visibility

**Files:**
- Modify: `control/src/dgx_control/metrics.py`
- Modify: `control/src/dgx_control/dashboard.py`
- Modify: `deploy/compose/prometheus/alerts.yaml`
- Modify: `deploy/compose/grafana/dashboards/fleet.json`
- Test: `control/tests/test_agent_metrics.py`
- Test: `deploy/compose/tests/test_observability.py`

**Interfaces:**
- Adds bounded labels for agent state/version compatibility, certificate expiry, operation counts, lease age, rollout state, and last-seen age.

- [ ] **Step 1: Write failing cardinality and alert tests**

Assert labels use node ID, operation enum, state, and version bucket only; never
job IDs, certificates, addresses, errors, actors, or payload content. Require
alerts for stale agents, expiring certificates, repeated failures, and rollout
pause with runbook links.

- [ ] **Step 2: Run and observe absent metrics**

Run: `uv run --project control pytest control/tests/test_agent_metrics.py -v && uv run pytest deploy/compose/tests/test_observability.py -v`
Expected: FAIL new assertions.

- [ ] **Step 3: Implement metrics/dashboard projections and alerts**

Read operational tables through aggregate queries, normalize version to
supported/old/new/incompatible, and keep errors only in redacted job logs.
Update fleet response with last seen, certificate expiry, and compatibility.

- [ ] **Step 4: Run Phase 4 verification**

Run: `uv run --project control pytest control/tests/test_desired_state.py control/tests/test_agent_reconciliation.py control/tests/test_production_worker.py control/tests/test_agent_metrics.py -q && uv run pytest deploy/compose/tests/test_observability.py -q && git diff --check`
Expected: all pass.

- [ ] **Step 5: Commit visibility**

```bash
git add control/src/dgx_control/metrics.py control/src/dgx_control/dashboard.py deploy/compose/prometheus/alerts.yaml deploy/compose/grafana/dashboards/fleet.json control/tests/test_agent_metrics.py deploy/compose/tests/test_observability.py
git commit -m "feat: observe outbound Spark agents"
```
