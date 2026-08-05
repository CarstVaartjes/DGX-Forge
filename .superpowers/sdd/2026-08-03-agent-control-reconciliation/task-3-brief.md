### Task 3: Fail-closed route and operation orchestration

**Start condition:** Task 2 must be accepted on `main` with migration `0008`,
explicit route entrypoint, and immutable LiteLLM quota inputs.

**Architecture:** Use PostgreSQL as a durable execution projection and publish one
idempotent route/LiteLLM bundle. Presence may withdraw an accepted route or supply
the current address for its already-authorized node; it may never create route
authority. Do not extend synchronous `RuntimeHandlers.reconcile()` and do not merge
the NAS route bridge wholesale.

**State machine:**

```text
planned -> withdrawal-pending -> routes-withdrawn -> dispatching
  -> compensating -> failed
  -> waiting-for-operator
  -> accepting -> publication-pending -> completed
```

Routes remain withdrawn in every non-completed state. No agent operation may be
inserted before durable maintenance withdrawal. Cancellation after mutation must
compensate or wait; it cannot directly become cancelled.

**Persistence (migration `0009`):**

- `reconciliation_operations`: reconciliation ID, graph operation ID, primary or
  compensation role, unique agent operation ID, expected payload digest, bounded
  state, canonical result/evidence digests, acceptance timestamp, and compensated
  graph operation ID.
- `route_publications`: reconciliation ID, state/generation, plan/evidence/route/
  LiteLLM/bundle digests, activation marker, and lease timestamps.
- Unique nullable `jobs.reconciliation_id` foreign key, so parent aggregation does
  not trust JSON payload fields.

**Transaction rules:**

- Lock sorted target nodes, then reconciliation, parent job, and execution rows.
- Enqueue only dependency-ready graph nodes; attach the agent UUID in the same
  transaction and enforce uniqueness across ticks/restarts.
- Consume results under the accepted Node -> Certificate -> AgentOperation ->
  Attempt order, then lock reconciliation/execution state. Verify exact graph ID,
  payload, fence, result, evidence, action/workload/release, and verify digest.
- Reconciliation jobs bypass generic first-wave parent aggregation; only the
  orchestrator terminalizes them.
- Compensate accepted starts with pinned stop payloads in reverse dependency order.
  Failed or uncertain mutation/compensation enters `waiting-for-operator`.
- Publish only after every graph operation is accepted. Stage route JSON, LiteLLM
  JSON, and a canonical manifest; atomically replace one activation marker last.
  The marker binds reconciliation, plan, accepted evidence set, route, and LiteLLM
  digests. A restart inspects the exact marker before acknowledging completion.

**Files:**

- Create `control/migrations/versions/0009_reconciliation_execution.py`
- Create `control/src/dgx_control/agent_reconciliation.py`
- Narrowly port `control/src/dgx_control/route_runtime.py` and the LiteLLM supervisor
- Create `control/tests/test_agent_reconciliation.py`
- Create `control/tests/test_agent_reconciliation_postgres.py`
- Modify models, orchestration, reconcile, agent jobs/API, routes, LiteLLM, API,
  worker ticking, Compose, and relevant tests
- Leave SSH removal/legacy runtime isolation to Task 4

**Mandatory TDD matrix:**

- One/two/sixteen-node dependency waves and concurrent tick idempotency.
- Withdrawal failure inserts zero agent operations.
- Release before prepare; workers before entrypoint; all health/verify before publish.
- Invalid/stale/revoked/incompatible/bad-digest evidence unlocks nothing.
- First completed wave cannot terminalize the parent job.
- Partial start failure compensates all successful starts in reverse order.
- Mutating uncertainty or failed/uncertain compensation waits for operator.
- Route/LiteLLM validation or apply failure retains maintenance.
- Crash before files, before marker, and after marker before DB acknowledgement.
- Restart resumes every nonterminal phase without duplicate mutation.
- Fresh presence, repository reread, or `/v1/models` probe cannot publish.
- Address change withdraws before probe/replacement; stale presence/revocation/
  ineligible commit/expired lease withdraws.
- PostgreSQL races: tick/tick, result/tick, result/revocation,
  compensation/tick, and publication/publication.
- Exact SQLite and PostgreSQL `0008 -> 0009 -> 0008 -> 0009` migration cycle with
  legacy reconciliation/job/agent rows.

**NAS port boundary:** Reuse authenticated address policy, structured endpoint,
atomic replacement, lease expiry, empty LiteLLM bootstrap, and bounded supervisor.
Do not reuse `desired-route.json`, presence-driven publication, repository rereads as
authority, or synchronous runtime publication. Port only after Task 2 is accepted.
