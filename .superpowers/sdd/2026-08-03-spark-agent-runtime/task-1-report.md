# Task 1 report — agent configuration and fenced state

## TDD evidence

RED (after adding the configuration/state tests, before creating `dgx_agent`):

```sh
uv run --project agent pytest agent/tests/test_config.py agent/tests/test_state.py -v
```

Result: collection failed with `ModuleNotFoundError: No module named
'dgx_agent'` for both test modules (0 collected, 2 errors).

GREEN:

```sh
uv run --project agent pytest agent/tests/test_config.py agent/tests/test_state.py -v
uv run --project agent pytest agent/tests -v
uv run --project agent python -m compileall -q agent/src
git diff --check
```

Result: 60 focused tests passed; bytecode compilation and whitespace validation
completed successfully. The test suite directly inspects the configured
connection's WAL, foreign-key, `FULL` synchronous, and bounded busy-timeout
settings. A live configured connection also showed database, WAL, and SHM files
at mode `0600` under the `0700` state root.

Review-fix round 2 RED collected 97 tests with nine expected failures for
numeric URL aliases, unsafe state ancestors, mismatched protocol deadlines, and
backward-clock updates. GREEN now runs 104 tests. Four parallel repetitions of
the thread/process fresh-root initialization tests also passed (eight focused
runs total), exercising advisory-locked first initialization.

Review-fix round 3 RED collected 47 state tests with seven expected failures:
two for the missing bounded/EINTR-safe initialization lock and five for
unexpected triggers, views, tables, and indexes. GREEN now runs 112 agent tests,
including repeated-EINTR deadline coverage.
Four parallel repetitions of both fresh-root thread/process initialization
tests passed after the fix.

Review-fix round 4 RED collected 49 state tests and selected two expected
failures: repeated `EINTR` retries performed no waits, and a retry could acquire
the lock after its wait had crossed the deadline. GREEN now runs 49 state tests
and 113 agent tests. Every retryable lock error uses bounded backoff, followed
by a monotonic deadline check before another acquisition attempt. Four fresh
parallel repetitions of both thread/process initialization tests again passed
(eight focused contention cases total).

Packaging was also verified with `uv build --project agent --wheel` followed by
an isolated `uv run --no-project --with` import using that wheel and the pinned
protocol wheel. Generated wheel artifacts were removed afterward.

## Design decisions

- `AgentConfig` is a frozen dataclass with only the permitted durable fields.
  It rejects duplicate/unknown/missing JSON fields, unsafe URLs and identifiers,
  unsafe paths, oversized files, and insecure key modes without echoing values.
- Configuration and state paths are walked descriptor-relative with
  `O_DIRECTORY|O_NOFOLLOW`; unsafe owners, writable non-sticky ancestors,
  symlinks, devices, and FIFOs fail closed.
- SQLite opens through `/proc/self/fd/<root-fd>` while the verified `0700` root
  descriptor remains live. A nonblocking advisory lock permits one immediate
  attempt, then applies bounded backoff and a pre-attempt monotonic deadline
  check to every `EINTR`, `EACCES`, or `EAGAIN` retry. The five-second bound
  serializes WAL/schema initialization without allowing a post-deadline retry;
  routine connections do not renegotiate journal mode.
- Schema acceptance uses the exact table/index definitions plus an exact
  schema-object allowlist and
  column, primary-key, uniqueness, and partial-index metadata. The unresolved
  partial unique index covers either active mutation or pending terminal
  delivery until exact acknowledgment.
- Canonical protocol bytes are persisted and parsed back through the pinned
  `dgx-agent-protocol` wheel. Corrupt/noncanonical records raise a typed error.
- Exact claim/result replay is idempotent; global fence reuse, stale attempts,
  changed deadlines/identities, and conflicting terminal results fail closed.
  Mutations use nondecreasing canonical UTC timestamps and validate the updated
  row before commit.

## File inventory

- `agent/pyproject.toml`, `agent/uv.lock`: test dependency and source layout.
- `agent/src/dgx_agent/config.py`: strict immutable configuration.
- `agent/src/dgx_agent/state.py`: durable fenced SQLite state store.
- `agent/src/dgx_agent/__init__.py`: package marker.
- `agent/tests/test_config.py`, `agent/tests/test_state.py`: 113 deterministic
  configuration, security, restart, replay, corruption, and concurrency tests.

## Self-review and remaining concerns

The implementation intentionally exposes no listener, command, enrollment
token, or secret-bearing durable field. The only later-phase concern is that
enrollment and network clients are intentionally out of scope for Task 1.
The repository-wide `pytest -v` command was not used as Task 1 verification:
from the repository root it selects unrelated tests (which currently include
pre-existing supply-chain failures and a segmentation fault). The focused agent
suite above is the complete agent suite at this task boundary.
