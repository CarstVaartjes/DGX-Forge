# Task 17 Review Round 5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Authenticate every persisted resolved plan before durable replay and derive unmanaged compute occupancy from bounded production probe evidence.

**Architecture:** A pure persisted-plan validator in the orchestration boundary canonicalizes and cross-checks the complete plan, graph, payload, digest, commit, target, and reconciliation identity before either restart loading or workload replay can consume it. Separately, the existing bounded node-probe contract will report only an occupancy classification, which the control plane persists and combines with accepted reconciliation evidence so unexplained compute always makes placement unavailable.

**Tech Stack:** Python 3.12, SQLAlchemy 2, PostgreSQL 16, Alembic, pytest, vonk-agent-protocol, Ruff.

## Global Constraints

- Preserve Task20 Node → Certificate → Operation/Attempt lock ordering.
- Do not expose PIDs, process names, command lines, network addresses, or raw GPU process data.
- Preserve existing probe size/schema/security bounds.
- Use strict RED → GREEN cycles.

---

### Task 1: Shared persisted-plan authentication

**Files:**
- Modify: `control/src/vonk_control/orchestration.py`
- Modify: `control/src/vonk_control/desired_state.py`
- Test: `control/tests/test_orchestration.py`
- Test: `control/tests/test_desired_state.py`

**Interfaces:**
- Produces: `validate_persisted_resolved_plan(...) -> tuple[OperationGraph, Mapping[str, object]]`.
- Consumes: immutable scalar/document fields from one `Reconciliation` row.

- [x] Add corruption tests for plan digest, graph digest/content, base commit, operation payload digest, entrypoint, lifecycle, definition, and profile.
- [x] Run the focused tests and confirm the unvalidated durable replay cases fail.
- [x] Implement the pure validator and call it from both restart loading and replay before accessing `workload_groups`.
- [x] Refactor durable fixtures to complete authenticated resolved documents and run focused tests GREEN.

### Task 2: Production unmanaged-compute observation

**Files:**
- Modify only after ownership review: node probe/agent collection contract and control observation projection files identified by repository inspection.
- Test: corresponding agent, protocol, API, durable desired-state, and placement tests.

**Interfaces:**
- Produces: one bounded enum-like occupancy signal: clean, active-compute, or unknown.
- Consumes: existing local GPU/workload telemetry without retaining raw process identity.

- [x] Map the existing probe collector, protocol schema, persistence handler, and observation projection; send the exact design if supervisor ownership expands.
- [x] Add failing production-path tests for clean, accepted-managed, unexplained active compute, and unknown evidence.
- [x] Implement the minimal bounded collector/protocol/persistence projection and reject unexplained or unknown occupancy in placement.
- [x] Run agent/protocol/control production-path tests GREEN.

### Task 3: Integration evidence and commit

**Files:**
- Modify: `.superpowers/sdd/2026-08-03-agent-control-reconciliation/progress.md`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: one review-round commit with exact evidence.

- [x] Run focused control, PostgreSQL, repeated lifecycle, Ruff, compile, schema, migration-head, conflict, and diff gates.
- [x] Update the ledger, commit, and report the SHA and exact counts.
