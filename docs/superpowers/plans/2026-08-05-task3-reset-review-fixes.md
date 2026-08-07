# Task 3 Reset Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the residual Task 3 unsafe-expiry, automatic-scheduling, and publication-handoff findings without changing the accepted SSH/egress boundary.

**Architecture:** PostgreSQL target locks continue to serialize reconciliation traffic. Claim-triggered uncertainty reuses whole-reconciliation quiescence, automatic ticks validate the completed publication owner before falling through to one actionable row, and an existing `withdrawal-pending` publication row is the durable successor-handoff intent. That newer intent fences predecessor renewal/cancellation writes, and the singleton owner changes only after the exact maintenance marker has been acknowledged.

**Tech Stack:** Python 3.12, SQLAlchemy 2, PostgreSQL 16, pytest, `vonk_agent_protocol`, atomic route publisher/supervisor acknowledgement.

## Global Constraints

- Keep sorted target locks before reconciliation, Job, projection, agent operation, and attempt locks.
- Preserve Node -> Certificate -> Presence -> Operation -> Attempt identity fencing.
- Use real PostgreSQL for cross-session state and race/interleaving regressions.
- Preserve generic-worker/reconciliation alternation and all already-green Task 3 behavior.
- Do not change Task 4/19 SSH transport or cluster-egress behavior.
- Avoid a migration: the accepted `withdrawal-pending` and singleton-owner rows already encode the required durable handoff.

---

### Task 1: Claim-triggered whole-reconciliation quiescence

**Files:**
- Modify: `control/tests/test_agent_reconciliation_postgres.py`
- Modify: `control/src/vonk_control/agent_jobs.py`

**Interfaces:**
- Consumes: `AgentJobService._quiesce_reconciliation_operations(Session, reconciliation_id, now)` and linked `Job.reconciliation_id` authority.
- Produces: claim-triggered unsafe expiry that fences every primary/compensation sibling in the same transaction; `_active()` rejects heartbeat/result after the linked phase loses execution authority.

- [x] **Step 1: Write the PostgreSQL failing regression**

Add `test_postgres_claim_expiry_quiesces_queued_and_running_siblings_and_rejects_callbacks`. Build three independent `workload.start` projections, claim A and B, leave C queued, advance the shared clock past A's lease, and trigger expiry with a later A claim. Assert A is expired/waiting, B's operation/attempt/projection are waiting, C's operation/projection are failed, the reconciliation and Job wait for the operator, and both B heartbeat and result are rejected without persisted progress/result.

- [x] **Step 2: Run the regression and verify RED**

Run:

```bash
uv run --project control pytest control/tests/test_agent_reconciliation_postgres.py::test_postgres_claim_expiry_quiesces_queued_and_running_siblings_and_rejects_callbacks -q
```

Expected: FAIL because B remains running and C remains queued after `_project_unsafe_expiry()` updates only A.

- [x] **Step 3: Implement the minimal queue fix**

After the selected expired projection is marked uncertain, call the existing deterministic whole-reconciliation quiescence routine while the sorted targets remain locked. Before `_active()` returns a linked running attempt, validate the authoritative Job/reconciliation/projection phase; reject the callback transaction with `StaleAgentAttempt` if it is no longer executable.

- [x] **Step 4: Run RED to GREEN and focused queue coverage**

Run:

```bash
uv run --project control pytest control/tests/test_agent_reconciliation_postgres.py::test_postgres_claim_expiry_quiesces_queued_and_running_siblings_and_rejects_callbacks control/tests/test_agent_reconciliation_postgres.py::test_postgres_agent_declared_uncertainty_quiesces_all_primary_siblings control/tests/test_agent_reconciliation_postgres.py::test_postgres_maintenance_sweeps_unsafe_expiry_without_follow_up_claim control/tests/test_agent_queue_reconciliation.py -q
```

Expected: all selected cases pass.

---

### Task 2: Actionable automatic scheduling without completed-owner blind spots

**Files:**
- Modify: `control/tests/test_agent_reconciliation_postgres.py`
- Modify: `control/src/vonk_control/agent_reconciliation.py`
- Verify: `control/tests/test_worker.py`

**Interfaces:**
- Consumes: singleton `RoutePublicationOwner`, active cancellation states, nonterminal reconciliation phases, and the existing completed lease-renewal threshold.
- Produces: an automatic tick that preflights the completed owner first and, only when that preflight is a true no-op, advances the deterministic oldest actionable row in the same call.

- [x] **Step 1: Write three multi-row failing regressions**

Add:

```text
test_postgres_automatic_ticks_do_not_starve_older_requested_cancellation
test_postgres_automatic_ticks_do_not_starve_older_planned_execution
test_postgres_automatic_ticks_do_not_starve_older_unsafe_expiry
```

Each fixture has a newer, fresh completed owner plus an older actionable row and calls only `tick()` repeatedly. Assert cancellation converges, planned execution leaves `planned`, and expired mutation reaches operator wait. Also assert the completed owner is checked before fallback so authority loss withdraws immediately and a due lease renewal remains bounded.

- [x] **Step 2: Run the three tests and verify RED**

Run:

```bash
uv run --project control pytest control/tests/test_agent_reconciliation_postgres.py -q -k 'automatic_ticks_do_not_starve'
```

Expected: three failures because `_candidate_id()` repeatedly returns the newer fresh completed row and `tick()` returns `False`.

- [x] **Step 3: Implement bounded owner-preflight fallback**

Split completed-owner selection from actionable selection. On automatic calls, validate the exact singleton completed owner once; if that call changes state, return `True`. If it is fresh and unchanged, select the deterministic oldest cancellation/noncompleted candidate excluding completed rows and advance it in the same outer tick. Do not recursively retry more than this owner-plus-actionable pair.

- [x] **Step 4: Run scheduling and worker coverage GREEN**

Run:

```bash
uv run --project control pytest control/tests/test_agent_reconciliation_postgres.py -q -k 'automatic_ticks_do_not_starve or stale_completed_candidate or completed_owner' && uv run --project control pytest control/tests/test_worker.py -q
```

Expected: multi-row scheduling, owner maintenance, stale-candidate protection, and generic worker alternation pass.

---

### Task 3: Acknowledged publication-owner handoff

**Files:**
- Modify: `control/tests/test_agent_reconciliation_postgres.py`
- Modify: `control/src/vonk_control/agent_reconciliation.py`

**Interfaces:**
- Consumes: `RoutePublication.state == "withdrawal-pending"`, `AtomicRouteBundlePublisher.withdraw()`, exact supervisor acknowledgement before that call returns, and locked `RoutePublicationOwner` ordering.
- Produces: successor authorization without transfer, followed by owner transfer only after an exact maintenance acknowledgement has been stored durably.

- [x] **Step 1: Write handoff/interleaving RED regressions**

Add:

```text
test_postgres_successor_owner_transfers_only_after_maintenance_ack
test_postgres_successor_authority_loss_withdraws_predecessor_before_wait
test_postgres_successor_withdrawal_crash_restarts_before_owner_transfer
```

Use the real atomic publisher with a deterministic acknowledgement callback. For normal handoff, block the ack and assert the committed singleton still names the predecessor until release. For authority loss after intent, assert maintenance is acknowledged before the successor becomes owner/operator-wait. For crash after acknowledged marker but before DB commit, assert the predecessor remains committed owner, predecessor renewal/cancellation cannot overwrite the successor maintenance marker, restart reuses the exact marker/generation, then transfer converges. Strengthen the old completed cancellation regression so it cannot mutate the successor marker after transfer.

- [x] **Step 2: Run the handoff tests and verify RED**

Run:

```bash
uv run --project control pytest control/tests/test_agent_reconciliation_postgres.py -q -k 'successor_owner_transfers or successor_authority_loss or successor_withdrawal_crash'
```

Expected: normal handoff observes early owner transfer; authority loss leaves the predecessor marker published; crash state already names the successor.

- [x] **Step 3: Separate authorization from transfer**

Make owner lookup/ordering authorize a newer `planned` or `withdrawal-pending` reconciliation without mutating the singleton. Persist the publication row and phase first, and treat the newest durable handoff intent as a fence against predecessor renewal/cancellation writes. In the withdrawal transaction, call the publisher and wait for exact acknowledgement, store the maintenance marker, then change singleton owner/generation. Apply the same acknowledged withdrawal when continuous authority is lost during `withdrawal-pending`. A restart repeats the exact idempotent withdrawal before transfer.

- [x] **Step 4: Run handoff/owner/publication coverage GREEN**

Run:

```bash
uv run --project control pytest control/tests/test_agent_reconciliation_postgres.py -q -k 'publication or owner or successor or cancellation'
```

Expected: all owner races, restart cases, acknowledged handoffs, and historical cancellation protections pass.

---

### Task 4: Evidence, verification, and scoped commit

**Files:**
- Modify: `.superpowers/sdd/2026-08-03-agent-control-reconciliation/task-3-report.md`
- Modify: `.superpowers/sdd/2026-08-03-agent-control-reconciliation/progress.md`
- Modify: `docs/superpowers/plans/2026-08-05-task3-reset-review-fixes.md`

**Interfaces:**
- Consumes: exact RED/GREEN output and final verification logs.
- Produces: reset rationale, finding-by-finding evidence, and one scoped commit without a readiness claim.

- [x] **Step 1: Record exact RED/GREEN evidence**

Append the observed failure messages/counts and subsequent passing commands for C2, I3/I7/actionability, and acknowledged owner handoff. Explain that the reset began from clean `2b4a5acd796a3639c730a9df0e93791ae2d7a8e0` and avoided schema changes.

- [x] **Step 2: Run required final gates**

Run the changed core and PostgreSQL suites, all focused Task 18 tests, exact migration cycles, Compose/supervisor/lifecycle coverage, the segmented control suite, Ruff 0.16.1, compileall, Compose render/YAML parsing, sole Alembic head, and `git diff --check`. Record exact counts and any environment-only failure separately.

- [x] **Step 3: Inspect and stage only reset-fix files**

Run:

```bash
git status --short
git diff --check
git diff --stat
```

Stage only the queue/reconciliation implementation, focused tests, this plan, Task 3 report, and Task 3 progress ledger.

- [x] **Step 4: Commit the reset repair**

Run:

```bash
git commit -m "fix(control): close reconciliation reset findings"
```

Return the commit SHA, exact verification evidence, and remaining concerns. Do not claim readiness; integration with updated `main` and a fresh scoped rereview remain required.

## Execution evidence

- Reset source: clean `2b4a5acd796a3639c730a9df0e93791ae2d7a8e0`; no schema or migration change.
- Claim-expiry RED: the second running mutation remained running and active callbacks remained writable. GREEN: the focused quiescence/phase matrix passed `23`; final core reconciliation/queue/jobs/worker coverage passed `111`.
- Automatic scheduling RED: the three requested automatic-only cases returned only `False`; four additional no-op/pending-cancellation cases failed together; explicit newer-plan preemption returned `True`. GREEN: the bounded selector cases passed `8`, while preserving one transition per tick and current-owner serialization.
- Publication handoff RED: the three primary cases observed early owner transfer, a still-published predecessor after authority loss, and a successor owner after rollback. GREEN: owner changes only after exact maintenance acknowledgement; restart reuses the exact marker; pending handoff fences predecessor renewal/cancellation; publication-ack crash followed by authority loss or cancellation withdraws first.
- Final verification: `40` PostgreSQL reconciliation cases, `18` focused reset regressions, `111` core cases, `580` segmented control tests, `6` migration/model-cycle tests, `35` Compose tests, `26` route/supervisor/lifecycle tests, Ruff `0.16.1`, compileall, rendered Compose JSON, sole head `0009_reconciliation_execution`, and `git diff --check` all passed.
- Independent semantic rereview: `0 Critical`, `0 Important`, `0 Minor`. Integration onto updated `main` and post-integration reverification remain mandatory; this plan makes no readiness claim.
