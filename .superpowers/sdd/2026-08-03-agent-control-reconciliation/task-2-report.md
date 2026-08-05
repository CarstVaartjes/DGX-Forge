# Task 2 report — repository-to-agent desired-state resolver

Date: 2026-08-05
Plan: `docs/superpowers/plans/2026-08-03-agent-control-reconciliation.md`
Branch: `feature/desired-state`
Base: `2e2d2ca8024a1538bebaea8a76c2bd480fe4a1b6`
Brief: `.superpowers/sdd/2026-08-03-agent-control-reconciliation/task-2-brief.md`

## Outcome

Task 2 replaces the production static reconciliation input with a fail-closed,
profile-aware `DesiredStateResolver`. It reads every input from the exact Git commit
through `RepositoryService`, validates V2 fleet, topology, cluster-profile, and
workload contracts, validates strict immutable release and logical-route contracts,
checks all cross-references and hashes, and runs the existing
`spark_profiles.PlacementPlanner`.

The resulting `ReconciliationPlan` pins:

- the eligible commit and sorted targets;
- deterministic placements and placement input digests;
- logical route candidates and immutable release-manifest/artifact data;
- every raw repository document SHA-256;
- agent protocol range `(1, 1)` and compatible closed capabilities;
- a canonical Task 1 `OperationGraph`; and
- canonical per-operation payloads and exact payload digests.

The graph contains release-install, prepare, start, health, and verify operations.
Start operations carry stop compensation. A distributed profile makes the canonical
entrypoint node depend on all worker starts. Independent ready operations remain
canonically ordered.

`Reconciler` accepts either a desired-state resolver or the new explicit
`CompatibilityDefinitions` adapter. A plain static callable is rejected, so
`inventory/reconciliation.json` cannot become an implicit fallback. Production API
planning now requires `profile_id` and uses durable PostgreSQL health plus connected
agent state. The plan response exposes its graph and protocol range.

## Reuse and packaging

The control service now depends on the repository's existing `spark-profiles`
package rather than copying placement logic. The control Docker build installs that
package from the allowlisted root build context alongside the pinned agent protocol
wheel. The existing Docker security test still verifies the exact protocol-wheel
hash and direct installation source.

The final integration is linear after the accepted control-plane migrations:
`0006_reconciliation_graph` → `0007_issued_revocations` →
`0008_resolved_plan`. The `0008_resolved_reconciliation_plan.py` migration owns
the unique plan digest and persisted resolved-plan columns. Task20's reviewed
Node → Certificate → Operation/Attempt lock order and revocation-recovery evidence
remain authoritative; desired state layers capability advertisement and bounded
probe observations onto that path.

## RED/GREEN evidence

The initial required RED was observed before production implementation:

```text
ModuleNotFoundError: No module named 'dgx_control.desired_state'
ImportError: cannot import name 'CompatibilityDefinitions' from 'dgx_control.reconcile'
collected 0 items / 2 errors
```

Additional TDD cycles captured missing durable observation projection, the missing
profile-aware API field, and closed-registry acceptance of implemented `node.probe`.
The first complete-suite run also exposed a real image regression: pip could not
resolve the local `spark-profiles==0.1.0` package in the Docker build stage. The
allowlisted source/package installation fix made the original security test pass.

Final evidence:

- `uv run --project control pytest control/tests/test_desired_state.py
  control/tests/test_reconcile.py control/tests/test_repository.py -v`:
  `30 passed`;
- `uv run pytest tests/spark_profiles/test_placement.py -v`: `5 passed`;
- `uv run --project control pytest control/tests -q`:
  `318 passed in 24.01s`;
- `uv run --project control pytest
  control/tests/security/test_agent_protocol.py -q`: `17 passed`;
- pinned Ruff over all changed Python files: `All checks passed!`;
- direct `py_compile` over all changed Python files passed;
- `git diff --check` passed.

## Explicit limitations and boundaries

- The current repository still contains legacy profiles and no
  `manifests/releases/<workload>.json` V2 release documents. Production resolution
  therefore fails closed until those repository definitions are migrated. Legacy
  static plans remain available only through an explicitly constructed
  `CompatibilityDefinitions` adapter, which production does not construct.
- Protocol compatibility is intentionally fixed to version 1 because the installed
  `dgx-agent-protocol` package exposes schema version 1 but no negotiated range
  constants. Unknown capabilities, including unimplemented update/rollback, fail
  closed; implemented `node.probe` may be advertised in addition to required rollout
  operations.
- Durable resolution requires a fresh health observation plus populated agent
  `last_seen_at`, protocol version, and capabilities. Missing agent evidence fails
  closed instead of falling back to SSH or repository assumptions.
- Routes are logical, unpublished candidates only. This task does not withdraw or
  publish routes, enqueue/execute node work, compensate failures, invoke SSH, or
  perform the Task 3/4 worker cutover.

Implementation is ready for independent review on `feature/desired-state`.

## Post-Task20 integration verification

The branch was rebased onto Task20 main `edc8148`. Conflict resolution retained
Task20's enrollment, rotation, revocation-recovery, and PostgreSQL lock-order
semantics, then added only closed capability advertisement and bounded probe
observations. Verification covered the single Alembic head and real
`0007_issued_revocations` ↔ `0008_resolved_plan` SQLite/PostgreSQL cycles, 23
PostgreSQL lock/migration/atomic-plan tests, 122 focused control tests, all 360
segmented control tests, all 541 segmented agent tests, and the repository-driven
lifecycle E2E. Ruff, `py_compile`, conflict-marker scanning, and diff checks passed.
The monolithic runner still reproduces an interpreter segmentation fault inside
jsonschema on Python 3.12; isolated modules pass and expose no assertion failure.
