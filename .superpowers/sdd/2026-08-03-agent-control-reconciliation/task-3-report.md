# Task 3 report — final reconciliation control repair

Date: 2026-08-05
Plan: `docs/superpowers/plans/2026-08-03-agent-control-reconciliation.md`
Branch: `feature/agent-reconciliation`
Repair base: `194af6c`

## Outcome

This repair closes the final reconciliation authority, queue fencing, route
ownership, LiteLLM activation, cancellation, and lifecycle findings while keeping
PostgreSQL as the durable execution authority. It does not perform the Task 4/19
SSH or cluster-egress removal.

Planner quota digests and the route publisher now hash the same
`canonical_message()` bytes. Durable desired-state replay derives reconciliation
authority only from the unique `Job.reconciliation_id` foreign key, never a JSON
hint. Claims lock reconciliation targets before queue authority, require the exact
linked Job/projection/phase, continuously validate commit, protocol, capability,
node, and certificate authority, and quiesce every primary and compensation sibling
when authority becomes uncertain. Unsafe lease expiry is swept autonomously by
reconciliation maintenance without requiring a later agent claim.

A locked singleton `route_publication_owner` row owns the one global activation
marker. Newer plans can supersede older plans only inside that critical section;
old completed renewal and cancellation paths cannot overwrite a newer maintenance
or completed owner. Completed-owner maintenance revalidates repository eligibility,
current deployment head, node compatibility/revocation, and current certificate-
bound address evidence on every pass and withdraws immediately on loss.

Route activation now waits for a canonical acknowledgement written by the live
LiteLLM supervisor. The acknowledgement binds the exact activation digest,
generation, state, LiteLLM digest, expiry, and live child PID. The supervisor writes
it atomically only after the selected child passes its bounded liveliness check,
refreshes it while that exact child remains live, clears it on reload/crash/health
loss, and stops a published child at lease expiry. Compose gives LiteLLM a separate
bounded supervisor volume, UID/GID `10002:10001`, no capabilities, a read-only root,
`no-new-privileges`, and only the required tmp/ack writes.

Cancellation is a first-class durable intent. The production RBAC/audit route
commits an idempotent, redacted cancellation row before any marker effect. Worker
ticks resume `requested`, withdrawal, processing, compensation, completion, and
operator-wait states after process death. Completed withdrawal is committed before
the cancellation decision, and compensation completion or uncertainty converges
the cancellation row after restart. The old direct orchestrator cancellation entry
point is disabled.

The lifecycle acceptance script now drives initial deployment and A-to-B
replacement through production `AgentReconciliationService`, real durable claims
and result consumption, authenticated presence, `AtomicRouteBundlePublisher`, and
the exact supervisor acknowledgement contract. It no longer mutates phases or Jobs
manually and no longer uses the legacy route/LiteLLM publisher path.

## Strict RED/GREEN evidence

- Planner quota bytes: the real resolver-to-atomic-publisher regression failed at
  route quota digest verification, then the planner/route focused set passed
  `14 passed` after canonical hashing.
- Publication ownership: the restart/R1-R2 PostgreSQL pair failed `2` cases before
  the singleton owner and passed `2 passed` after it. Old completed cancellation
  clobbering a newer owner failed once, then passed.
- Multi-operation uncertainty and claim authority: queued/running sibling cases
  failed `2` before whole-reconciliation quiescence and passed `3` after the locked
  authority phase was enforced. An authoritative operator-wait claim failed once,
  then passed.
- Autonomous expiry: queued/running sibling cases failed `2` before maintenance
  sweeping and passed `2` after it.
- FK-only replay: forged/missing JSON hint cases failed `2`, then passed `2` after
  replay used only `Job.reconciliation_id`.
- Redaction: a direct secret-like agent reason failed once, then passed with
  redacted terminal persistence.
- Tick/fairness/repoll: three regressions failed before no-op ticks returned false,
  queue turns alternated, and long polls rechecked PostgreSQL; the focused set then
  passed `9`.
- Continuous eligibility: protocol/capability claim and eligibility/current-head/
  address result cases failed `6` at the missing constructor contract, then passed
  `6`; the combined eligibility set passed `14`.
- Cancellation: the production API route failed once before durable wiring and
  passed `2`; intent-before-effect, withdrawal crash recovery, and legacy direct-
  cancel disablement each failed then passed. Final self-review found two additional
  convergence gaps (post-compensation restart and API-enqueued operator wait):
  `2 failed`, then `2 passed` after active cancellation intent participated in
  candidate selection and terminal state convergence.
- Supervisor protocol: publisher and supervisor acknowledgement tests each failed
  before implementation. Exact request validation, marker-before-DB restart reuse,
  live child crash cleanup, exact lease expiry, and Compose assertions are green.
- Lifecycle: the E2E first failed with `agent operation lacks reconciliation
  authority` on the manual path and passed after production-service conversion.
- Coverage audit: durable one/two/sixteen-target execution, service reconstruction
  from every normal nonterminal phase, compensation restart, presence non-authority,
  completed-owner loss across six authority dimensions, cancellation convergence,
  migration parity, and live supervisor crash/expiry passed `11` focused cases plus
  the supervisor and migration files.

## Final finding disposition

- **Canonical quota digest:** planner and publisher share protocol canonical bytes;
  newline-terminated JSON remains a filesystem-only concern.
- **Unsafe expiry and sibling work:** agent-declared uncertainty, expired mutation
  leases, authority loss, cancellation, and target loss fence every primary and
  compensation operation/attempt before operator wait. The maintenance sweep does
  not rely on a follow-up claim.
- **Queue authority:** a claim requires its exact linked Job, reconciliation,
  projection, role phase, current attempt, active identity, pinned commit,
  persisted protocol range, and complete implemented capability set. A persisted
  generic-job retry disposition is consumed only for its exact attempt.
- **Singleton publication:** the seeded singleton row serializes current ownership;
  PostgreSQL tests cover old completed restart, newer maintenance, newer completion,
  old cancellation, publication/publication, and stale-lease renewal races.
- **Continuous eligibility:** production API and worker use `GitPolicy.eligible`
  and `RepositoryService.head(settings.deployment_branch)`. Result/publication and
  completed-owner maintenance also require current authenticated address evidence.
  Presence can update address evidence but cannot create a reconciliation,
  projection, publication, or owner.
- **Supervisor request/ack:** all new and idempotent publish/withdraw requests wait
  for the exact recent live-child acknowledgement. Timeout, invalid selection,
  start failure, crash, health loss, and expiry remain fail closed.
- **Desired-state authority:** accepted and uncertain mutation replay uses the
  database FK only; forged payload hints cannot attach authority.
- **Cancellation:** intent, request identity, actor, reason, and state are durable;
  API access is operator/administrator only and audited; marker withdrawal is
  restartable; running mutation/compensation uncertainty cannot become a clean
  cancellation; terminal cancellation state converges after restart.
- **Scheduling and observability safety:** no-transition ticks return false,
  generic and reconciliation work alternate, cross-process enqueue is discovered
  by bounded DB repoll, and terminal reasons are redacted and bounded.
- **Lifecycle/least privilege:** lifecycle acceptance uses the production services;
  rendered Compose proves the unprivileged LiteLLM and supervisor-volume contract.
- **Task boundary:** no SSH implementation, production SSH cutover, or cluster-egress
  removal was added in this repair.

## Verification

- Full control tests, isolated one file per interpreter because the local CPython
  build segfaults during large pytest assertion-rewrite collections: `560 passed`
  before the final two cancellation tests; the fresh changed reconciliation/jobs/
  API/PostgreSQL matrix after that repair passed `141 passed in 24.27s`.
- Actual PostgreSQL reconciliation/jobs suite: `43 passed in 12.40s`.
- Exact SQLite and PostgreSQL `0008 -> 0009 -> 0008 -> 0009` cycle plus execution
  model parity: `6 passed`; sole Alembic head output is
  `0009_reconciliation_execution (head)`.
- Complete rendered Compose suite: `35 passed in 8.93s`, including exact UID/GID,
  `cap_drop: ALL`, `no-new-privileges`, read-only root, volume ownership, live
  supervisor crash, and lease expiry assertions.
- Platform lifecycle E2E: `1 passed in 1.18s`.
- Shared agent suite: `545 passed`; two unchanged PyInstaller slot-artifact tests
  hit the documented local bytecode-corruption failure (`SIGSEGV`/`dis.py`
  `TypeError`).
- Pinned Ruff 0.16.1: `All checks passed!`.
- `compileall` for control source/tests and the supervisor, lifecycle `py_compile`
  plus executable-bit check, rendered Compose YAML validation, schema-copy parity,
  and `git diff --check`: passed.

The monolithic root/control pytest processes are not clean evidence in this local
environment: unchanged `jsonschema`, `tomllib`, supply-chain, and PyInstaller paths
reproduce SIGSEGV/SIGABRT and invalid/unknown bytecode opcodes. All Task 3 changed
paths, all control files in isolated interpreters, real PostgreSQL races, migration
cycles, Compose, and lifecycle tests completed without a product assertion failure.

No independent readiness claim is made by this implementation report.
