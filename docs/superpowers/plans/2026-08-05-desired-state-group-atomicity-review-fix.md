# Desired-State Group Atomicity Review Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make reconciliation retain and transition only complete persisted workload groups, place them without co-residency ambiguity, replay accepted mutations by a transactional causal generation, and admit repository quotas without weakening generic secret rejection.

**Architecture:** Resolved plans persist a canonical group contract containing ordered placement, entrypoint, release and adapter, request fingerprints, and lifecycle. Durable replay reconstructs whole groups strictly by database-assigned completion generation; placement and operation planning compare complete group fingerprints and otherwise tear down/rebuild atomically. The V2 planner temporarily supports exclusive workloads only, while job payload validation recognizes quotas solely through the validated reconciliation route shape.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, PostgreSQL 16, SQLite, pytest, jsonschema, Ruff.

## Global Constraints

- Preserve Task 20 Node → Certificate → Operation/Attempt locking and revocation behavior.
- Keep `0008_resolved_plan` the sole linear Alembic successor to `0007_issued_revocations`.
- Never infer a previous entrypoint from lexical node order or replay by timestamp/UUID.
- Reject non-exclusive desired workloads and co-resident current workload groups for now.
- Use strict RED → GREEN cycles and commit only after focused, PostgreSQL, repeated lifecycle, Ruff, compile, schema, and diff gates pass.

---

### Task 1: Transactional completion generation

**Files:**
- Modify: `control/migrations/versions/0008_resolved_reconciliation_plan.py`
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/src/vonk_control/orchestration.py`
- Test: `control/tests/test_agent_migrations.py`
- Test: `control/tests/test_orchestration.py`
- Test: `control/tests/test_reconcile_postgres.py`

**Interfaces:**
- Produces: nullable unique `Reconciliation.completion_generation` and singleton `reconciliation_completion_generation` counter row.
- Consumes: `ReconciliationOrchestrator.advance(..., "completed")`, assigning one strictly monotonic generation in the same transaction.

- [x] Write SQLite migration/orchestration and PostgreSQL concurrent-completion tests, including legacy null preservation and same-timestamp rows.
- [x] Run the tests and confirm failures for missing schema/model/generation behavior.
- [x] Add the column, singleton counter table, serialized allocation, and generation-only replay ordering.
- [x] Re-run focused SQLite and PostgreSQL tests to GREEN.

### Task 2: Canonical group contract and atomic transitions

**Files:**
- Modify: `control/src/vonk_control/desired_state.py`
- Modify: `control/src/vonk_control/reconcile.py`
- Test: `control/tests/test_desired_state.py`
- Test: `tests/e2e/test_platform_lifecycle.py`
- Modify: `scripts/accept-platform-lifecycle`

**Interfaces:**
- Produces: immutable `workload_groups` plan mapping with ordered nodes, explicit entrypoint, release/adapter, definition/profile/preparation fingerprints, and lifecycle.
- Consumes: accepted resolved plans plus exact successful start/stop evidence to reconstruct complete current groups.

- [x] Add failing reversed-entrypoint, same-count role/profile change, 1→N, N→1, move, removal, and legacy-null replay tests.
- [x] Confirm current partial retention and lexical old-head behavior fail those tests.
- [x] Reconstruct current groups from accepted plans and retain only an exact whole-group fingerprint.
- [x] Emit all old stops in the persisted old lifecycle order and rebuild every desired member after the complete teardown barrier.
- [x] Remove the lifecycle simulator clock advance and prove same-timestamp A→B replay by completion generation.

### Task 3: Exclusive-only placement safety

**Files:**
- Modify: `control/src/vonk_control/desired_state.py`
- Test: `control/tests/test_desired_state.py`

**Interfaces:**
- Produces: fail-closed rejection for `exclusive = false`, current co-residency, mixed managed/unmanaged occupancy, and ambiguous capacity reclamation.
- Consumes: exact current group and desired group contracts from Task 2.

- [x] Add failing non-exclusive, mixed/co-resident, input-order, and total-capacity double-count tests.
- [x] Confirm unmanaged occupants cannot become eligible even for non-exclusive requirements.
- [x] Reject unsupported co-residency and expose total capacity only for a single exact retained or wholly torn-down managed group.
- [x] Re-run placement transition matrix to GREEN.

### Task 4: Structurally validated route quotas

**Files:**
- Modify: `control/src/vonk_control/jobs.py`
- Test: `control/tests/test_jobs.py`
- Test: `control/tests/test_desired_state.py`

**Interfaces:**
- Produces: generic sensitive-key rejection with a narrow validated reconciliation route-quota traversal.
- Consumes: exact `{requests_per_minute, tokens_per_minute}` positive bounded integer quota mappings already pinned in resolved routes.

- [x] Add failing arbitrary/nested `tokens_per_minute` string-secret tests and a valid reconciliation quota test.
- [x] Remove the global safe-key exception.
- [x] Validate quota keys only at the exact `routes.<alias>.quota` path and reject every other sensitive-key occurrence.
- [x] Re-run job and reconciliation enqueue tests to GREEN.

### Task 5: Final integration and ledger

**Files:**
- Modify: `.superpowers/sdd/2026-08-03-agent-control-reconciliation/progress.md`

**Interfaces:**
- Consumes: all prior task behavior.
- Produces: one reviewed round-4 commit and evidence ledger entry.

- [x] Run focused desired-state/orchestration/jobs/migration tests.
- [x] Run real PostgreSQL completion-generation and existing lock tests.
- [x] Run lifecycle acceptance repeatedly with identical timestamps.
- [x] Run pinned Ruff, py_compile, schema parity, conflict-marker, and diff checks.
- [x] Update the ledger, commit, and report the SHA and exact counts.
