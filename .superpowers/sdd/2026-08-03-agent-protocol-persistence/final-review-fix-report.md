# Plan 1 final-review fix report

## Status

DONE

Implementation commit:

`dd626308e7e394a91ae679558c070fed41435633 fix: gate agent operation retries`

## Root-cause analysis

### 1. Unsafe automatic reclaim

`AgentJobService.claim` treated every running operation whose current lease had
expired as reclaimable. Operation kind never entered the eligibility predicate.
A new fence prevents the stale agent from publishing a result, but it cannot
reverse a node mutation already performed by an expired `release.install`,
`workload.start`, or `agent.update` attempt. The operation row also had no
persisted, attempt-specific retry decision, so the queue could not distinguish
automatic safe recovery from an inspected operator-authorized retry.

The required invariant is now explicit: only `node.probe`, `workload.health`,
and `workload.verify` automatically reclaim. Every other expired running
operation is moved to `waiting-for-operator`, its attempt is persisted as
`expired`, and it is claimable only when `retry_disposition == "retry"` is
persisted for that exact `current_attempt`. Incrementing the attempt consumes
the decision because the stored disposition-attempt no longer matches; another
expiry gates again. No Plan 2 API was introduced.

### 2. Parent boundary and enqueue/finalization race

`enqueue` used unlocked `session.get` existence checks for the parent and node.
It never checked parent state, base commit, or membership of the node in the
parent targets. Parent aggregation did lock the parent, but an enqueue did not,
so it could insert a queued child after aggregation had read its sibling set and
before aggregation committed a terminal parent state.

`enqueue` now loads the parent with `SELECT ... FOR UPDATE`, rejects
`succeeded`, `failed`, `waiting-for-operator`, and `expired` parents, requires
an exact base-commit match, and requires the node to be in `parent.targets`.
The shared parent row lock serializes PostgreSQL enqueue with aggregation: an
enqueue that loses the race observes the terminal state and is rejected; an
enqueue that wins is visible to aggregation. SQLite retains the same validation
behavior, while its lack of inter-service row locking remains explicit in the
service comments and concurrency proof is PostgreSQL-only.

### 3. First-pip-command false negative

`docker_installs_protocol_wheel` used `next(...)` to select the first
`python -m pip install` token sequence in each logical `RUN`. If a harmless pip
install came first and the exact wheel/control install came second, the verifier
never inspected the valid segment.

The verifier now iterates every pip-install sequence and independently bounds
each segment at shell punctuation. A `RUN` is accepted when any real pip-install
segment consumes both the exact wheel and the control project. Existing
post-operator wheel-mention negatives remain rejected.

## RED evidence

### Unsafe reclaim

Command:

```text
uv run --project control pytest control/tests/test_agent_jobs_postgres.py::test_postgres_expired_safe_operation_is_automatically_reclaimed control/tests/test_agent_jobs_postgres.py::test_postgres_expired_mutating_operation_requires_persisted_retry_disposition -v
```

Output:

```text
collected 2 items
test_postgres_expired_safe_operation_is_automatically_reclaimed PASSED
test_postgres_expired_mutating_operation_requires_persisted_retry_disposition FAILED
E AssertionError: assert AgentClaim(... operation=<AgentOperation.RELEASE_INSTALL: 'release.install'> ... attempt=2 ...) is None
1 failed, 1 passed in 1.75s
```

This proves the safe control already reclaimed and the mutating operation was
wrongly issued as attempt 2.

### Parent validation and locking

Commands:

```text
uv run --project control pytest control/tests/test_agent_jobs.py -k sqlite_enqueue -v
uv run --project control pytest control/tests/test_agent_jobs.py -k 'sqlite_enqueue' control/tests/test_agent_jobs_postgres.py -k 'postgres_enqueue' -v
```

The first output was:

```text
collected 16 items / 11 deselected / 5 selected
test_sqlite_enqueue_rejects_terminal_parent[succeeded] FAILED
test_sqlite_enqueue_rejects_terminal_parent[failed] FAILED
test_sqlite_enqueue_rejects_terminal_parent[waiting-for-operator] FAILED
test_sqlite_enqueue_rejects_terminal_parent[expired] FAILED
test_sqlite_enqueue_enforces_parent_commit_and_target FAILED
E Failed: DID NOT RAISE ValueError
5 failed, 11 deselected in 0.55s
```

The second command's final `-k` selected the seven PostgreSQL cases; output was:

```text
collected 29 items / 22 deselected / 7 selected
test_postgres_enqueue_rejects_terminal_parent[succeeded] FAILED
test_postgres_enqueue_rejects_terminal_parent[failed] FAILED
test_postgres_enqueue_rejects_terminal_parent[waiting-for-operator] FAILED
test_postgres_enqueue_rejects_terminal_parent[expired] FAILED
test_postgres_enqueue_rejects_parent_commit_mismatch FAILED
test_postgres_enqueue_rejects_node_outside_parent_targets FAILED
test_postgres_enqueue_cannot_race_parent_finalization FAILED
E Failed: DID NOT RAISE ValueError
E assert 0 == 1  (enqueue_errors was empty)
7 failed, 22 deselected in 2.21s
```

### Retry-disposition migration

Command:

```text
uv run --project control pytest control/tests/test_agent_migrations.py::test_agent_models_capture_fenced_operation_state control/tests/test_agent_migrations.py::test_retry_disposition_migration_is_reversible -v
```

Output:

```text
collected 2 items
test_agent_models_capture_fenced_operation_state FAILED
test_retry_disposition_migration_is_reversible FAILED
E AttributeError: retry_disposition
E alembic.util.exc.CommandError: Can't locate revision identified by '0003_agent_operation_retry_disposition'
2 failed in 0.35s
```

The first disposable-PostgreSQL rehearsal additionally failed with:

```text
psycopg.errors.StringDataRightTruncation: value too long for type character varying(32)
[SQL: UPDATE alembic_version SET version_num='0003_agent_operation_retry_disposition' WHERE alembic_version.version_num = '0002_agent_operations']
```

That PostgreSQL-only RED identified Alembic's 32-character revision storage;
the new revision was shortened to `0003_retry_disposition` before the final
upgrade/downgrade proof. Frozen revisions `0001` and `0002` were not edited.

### Verifier second pip command

Command:

```text
uv run pytest tests/scripts/test_verify_supply_chain.py::test_verifier_accepts_exact_wheel_in_second_pip_install_command -v
```

Output:

```text
collected 1 item
test_verifier_accepts_exact_wheel_in_second_pip_install_command FAILED
E AssertionError: verify-supply-chain: control image does not install the standalone protocol wheel from root context
E assert 1 == 0
1 failed in 0.56s
```

## Implementation summary

- Added reversible migration `0003_retry_disposition` with nullable
  `agent_operations.retry_disposition` and
  `agent_operations.retry_disposition_attempt` columns.
- Added an explicit three-operation automatic-reclaim allowlist.
- Gated all other expired operations in `waiting-for-operator`; retry
  authorization is persisted and bound to the expired attempt number.
- Prevented `state = queued` from bypassing the retry gate after attempt 0.
- Locked the parent row during enqueue and enforced terminal-state,
  base-commit, and parent-target boundaries.
- Added real PostgreSQL tests for all three safe operation kinds, three named
  mutating kinds, terminal/commit/target rejection, and the enqueue/aggregation
  race. Added SQLite validation coverage where row locking is not supported.
- Iterated all pip-install command segments without weakening shell-operator
  displacement rejection.

## GREEN evidence

Focused migration and reclaim run before expanding the operation matrix:

```text
uv run --project control pytest control/tests/test_agent_migrations.py::test_agent_models_capture_fenced_operation_state control/tests/test_agent_migrations.py::test_retry_disposition_migration_is_reversible control/tests/test_agent_jobs_postgres.py::test_postgres_expired_safe_operation_is_automatically_reclaimed control/tests/test_agent_jobs_postgres.py::test_postgres_expired_mutating_operation_requires_persisted_retry_disposition -v

4 passed in 1.63s
```

SQLite and PostgreSQL parent-boundary runs:

```text
uv run --project control pytest control/tests/test_agent_jobs.py -k sqlite_enqueue -v
5 passed, 11 deselected in 0.50s

uv run --project control pytest control/tests/test_agent_jobs_postgres.py -k postgres_enqueue -v
7 passed, 6 deselected in 2.10s
```

Verifier parser runs:

```text
uv run pytest tests/scripts/test_verify_supply_chain.py::test_verifier_accepts_exact_wheel_in_second_pip_install_command tests/scripts/test_verify_supply_chain.py::test_verifier_rejects_a_dockerfile_that_copies_but_does_not_install_the_protocol_wheel -v
2 passed in 0.94s

uv run pytest tests/scripts/test_verify_supply_chain.py -q
15 passed in 6.86s
```

Final relevant and full project suites:

```text
uv run --project control pytest control/tests/test_agent_jobs.py control/tests/test_agent_jobs_postgres.py control/tests/test_agent_migrations.py control/tests/test_jobs.py control/tests/security/test_agent_protocol.py -q
59 passed in 7.49s

uv run --project control pytest control/tests -q
133 passed in 8.17s

uv run --project agent_protocol pytest agent_protocol/tests -q
321 passed in 0.29s

scripts/verify-supply-chain --json
{"errors":[],"images":5,"manifest_sha256":"1979857e866152810413be5522ef461d9888d2dff9b47d3669a78588b8e200e0","ok":true,"sboms":["inventory/sbom/agent-protocol.spdx.json","inventory/sbom/agent-python.spdx.json","inventory/sbom/control-python.spdx.json","inventory/sbom/control-web.spdx.json"]}

uv run --project control python -m compileall -q control/src
uv run python -m py_compile scripts/verify-supply-chain
git diff --check
all exited 0 with no output
```

## Migration upgrade/downgrade and PostgreSQL evidence

A disposable `postgres:16` database was upgraded from `0002` to the new
revision, downgraded to `0002`, then upgraded to `head` using the control
Alembic configuration. Exact output:

```text
postgres upgrade added: ['retry_disposition', 'retry_disposition_attempt']
postgres downgrade restored 0002: True
postgres head contains retry columns: True
```

The real PostgreSQL behavioral matrix is included in the 59-test relevant run.
It covered separate service instances and actual PostgreSQL row locks for safe
reclaim, denied mutating reclaim, attempt-bound retry consumption,
terminal/commit/target validation, enqueue-versus-finalization, duplicate claim,
reclaim-versus-completion, and concurrent aggregation.

## Self-review and concerns

- Mutation check: removing any safe kind prevents its parameterized reclaim
  test; adding a mutating kind to the allowlist makes its denial test fail;
  removing attempt binding permits the second expiry to reclaim; removing the
  parent lock makes the concurrency test observe a terminal parent with a new
  queued child; reverting `next` makes the second-pip positive fail; weakening
  command boundaries makes the four operator negatives fail.
- The retry disposition is intentionally persistence-only in Plan 1. A future
  authenticated operator workflow may write it, but this fix does not speculate
  about Plan 2 endpoints or authorization APIs.
- SQLite validates parent state/commit/target but cannot prove cross-service row
  locking because SQLite ignores `FOR UPDATE`; production locking is covered on
  PostgreSQL.
- The environment intermittently produced native crashes unrelated to this
  diff: one Docker `pip install` exited 139 and passed on exact rerun; one
  verifier subprocess returned `-11` with empty stderr and the full 15-test
  verifier suite passed on exact rerun; a monolithic repository `uv run pytest
  -q` crashed in `jsonschema` at 44%, while the implicated
  `tests/spark_profiles/test_admission.py` passed separately (`21 passed in
  4.55s`). The scoped full control/protocol/verifier suites above are clean.
