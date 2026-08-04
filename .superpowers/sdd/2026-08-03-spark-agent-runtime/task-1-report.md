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

## Design decisions

- `AgentConfig` is a frozen dataclass with only the permitted durable fields.
  It rejects duplicate/unknown/missing JSON fields, unsafe URLs and identifiers,
  unsafe paths, oversized files, and insecure key modes without echoing values.
- File checks use `lstat` path-component validation and `O_NOFOLLOW` for the
  final configuration read, so FIFO/device inputs are rejected without reads.
- `AgentStateStore` creates a restrictive root and database, initializes WAL
  SQLite with explicit immediate transactions, and lets a partial unique index
  enforce one active attempt across instances.
- Canonical protocol bytes are persisted and parsed back through the pinned
  `dgx-agent-protocol` wheel. Corrupt/noncanonical records raise a typed error.
- Exact claim/result replay is idempotent; altered claims, fences, messages, or
  terminal results fail closed. Progress sequence numbers are incremented in
  SQLite, not memory.

## File inventory

- `agent/pyproject.toml`, `agent/uv.lock`: test dependency and source layout.
- `agent/src/dgx_agent/config.py`: strict immutable configuration.
- `agent/src/dgx_agent/state.py`: durable fenced SQLite state store.
- `agent/src/dgx_agent/__init__.py`: package marker.
- `agent/tests/test_config.py`, `agent/tests/test_state.py`: 60 deterministic
  configuration, security, restart, replay, corruption, and concurrency tests.

## Self-review and remaining concerns

The implementation intentionally exposes no listener, command, enrollment
token, or secret-bearing durable field. The only later-phase concern is that
enrollment and network clients are intentionally out of scope for Task 1.
The repository-wide `pytest -v` command was not used as Task 1 verification:
from the repository root it selects unrelated tests (which currently include
pre-existing supply-chain failures and a segmentation fault). The focused agent
suite above is the complete agent suite at this task boundary.
