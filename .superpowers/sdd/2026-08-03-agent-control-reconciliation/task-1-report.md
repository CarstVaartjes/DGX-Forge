# Task 1 report — persisted reconciliation operation graph

Date: 2026-08-05
Plan: `docs/superpowers/plans/2026-08-03-agent-control-reconciliation.md`
Branch: `feature/reconciliation-graph`
Base: `64e19e9952d5b449ee2e0a6f255501580a71a911`
Brief: `.superpowers/sdd/2026-08-03-agent-control-reconciliation/task-1-brief.md`

## Outcome

Task 1 adds a deterministic, persisted operation-graph contract without performing
node work or changing route state:

- `OperationNode` carries canonical operation, target, workload, kind, dependency,
  compensation, and payload-digest bindings;
- `OperationGraph` exposes the dependency-ordered immutable graph document and stable
  SHA-256 digest;
- `ReconciliationOrchestrator.plan` validates and persists a graph;
- `advance` applies only the next persisted phase and a nondecreasing route-withdrawal
  generation;
- `cancel` records a bounded, redacted terminal reason without changing the graph.

The graph accepts only operation kinds implemented by the closed agent registry. It
rejects cycles, unknown operation targets, duplicate operations and dependencies,
unknown dependencies, cross-workload dependencies, invalid identifiers/digests, and
unsupported operation or compensation kinds. Topological peers are ordered by
canonical operation ID, so reordered input produces the same document and digest.

## Persistence and migration

The existing `reconciliations` row now stores the canonical graph JSON and digest,
current phase, route-withdrawal generation, and optional terminal reason. Advancement
and cancellation update only mutable orchestration state; tests verify the persisted
graph and digest remain unchanged.

The written plan called the new migration `0005_reconciliation_graph.py`, but revision
`0005_certificate_rotation` already exists on this base. The implementation therefore
uses the next linear revision, `0006_reconciliation_graph.py`, with
`down_revision = "0005_certificate_rotation"`. It supplies deterministic legacy
defaults for existing reconciliation rows and is covered by head-schema parity and
downgrade/re-upgrade migration tests.

## RED/GREEN evidence

The required RED was observed before production implementation:

```text
ModuleNotFoundError: No module named 'dgx_control.orchestration'
collected 0 items / 1 error
```

After implementation:

- `uv run --project control pytest control/tests/test_orchestration.py -v`:
  `11 passed`;
- `uv run --project control pytest control/tests/test_orchestration.py
  control/tests/test_agent_migrations.py -v`: `17 passed`;
- `uv run --project control pytest control/tests/test_migrations.py -q`:
  `1 passed`, including head-to-base round-trip;
- the first complete-suite run exposed PostgreSQL interpreting `:1` in the JSON
  `server_default` SQL text as a bind parameter (`schema_version` became `NULL`);
  replacing the SQL expression with SQLAlchemy's dialect-safe string default fixed
  the source of the regression;
- `uv run --project control pytest
  control/tests/test_agent_jobs_postgres.py::test_postgres_claim_locks_only_operations_without_nullable_join
  -q`: `1 passed` after that fix;
- `uv run --project control pytest control/tests -q`: `300 passed in 32.37s`;
- `uvx --from ruff==0.16.1 ruff check` over the changed Python files:
  `All checks passed!`;
- direct `py_compile` over the changed Python files passed;
- `git diff --check` passed.

## Scope boundary

This task does not execute operations, open connections to agents, invoke SSH or
subprocesses, withdraw or publish routes, resolve repository desired state, or
implement Tasks 2–5. The explicit `workload_id` binding exists only to make
cross-workload dependency rejection deterministic for later resolver input.

Independent review of `64e19e9..2e2d2ca` found no Critical, Important, or Minor
issues and returned Ready: Yes. It independently passed 18 graph/migration tests,
pinned Ruff, and diff checks, and exercised the real PostgreSQL 17.6 migration path
from `0005` to `0006` and back with a legacy reconciliation row. The task is
integrated on `main` at `75619d7`; publication acceptance remains gated on hosted CI.
