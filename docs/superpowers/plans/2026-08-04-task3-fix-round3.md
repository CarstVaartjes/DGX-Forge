# Task 3 Fix Round 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven Task 3 review findings without widening scope beyond signed release installation and its evidence.

**Architecture:** Extend the single authenticated monotonic deadline into every remaining ORAS, python-tuf, and staging-transaction boundary. Make restart recovery a typed, inode-bound state machine: durable intent, complete temp, exact-inode promotion/removal, and atomic quarantine for every leaf or substituted entry.

**Tech Stack:** Python 3.12, pytest, python-tuf 7.0.0, Linux dirfd APIs, `renameat2(RENAME_NOREPLACE)`, sealed memfds, Docker Compose.

## Global Constraints

- Work only from base `983a8ceacb9562b15b761b4ef3366982e427af19` in `.worktrees/outbound-agent`.
- Preserve the controller-owned progress ledger line.
- Add and run deterministic RED tests before production edits.
- Preserve held-parent descriptor publication and do not touch Task 4.

---

### Task 1: Remaining ORAS and python-tuf deadline boundaries

**Files:**
- Modify: `agent/tests/test_releases.py`
- Modify: `agent/src/dgx_agent/nvidia_tools.py`
- Modify: `agent/src/dgx_agent/update_trust.py`

**Interfaces:**
- Consumes: `Callable[[], None]` deadline callbacks and `MonotonicDeadline`.
- Produces: one-crossing-operation checks for executable ancestry/open/stat/setup and interruptible signed/target parsing plus target memfd creation.

- [x] Add tests that advance the fake monotonic clock inside ancestry open, executable open/stat, hash/memfd setup, signed custom parsing, target memfd creation, and target JSON parsing; assert no following syscall/state creation.
- [x] Run the named tests and record expected failures showing the unchecked next operation.
- [x] Thread the existing callback around every blocking or state-creating ORAS operation and wrap/check all TUF parsing/memfd phases while retaining per-thread trace restoration.
- [x] Re-run the tests and the prior credential, canonical receipt, rollback/freeze, trace-restoration, and thread-isolation tests.

### Task 2: Deadline-safe typed staging ownership transaction

**Files:**
- Modify: `agent/tests/test_releases.py`
- Modify: `agent/src/dgx_agent/releases.py`

**Interfaces:**
- Consumes: `_write_recovery_intent_fd`, `_complete_recovery_record_fd`, and the request deadline callback.
- Produces: helpers that check before and after every open/write/fsync/stat/replace and stop claim work after the first crossing operation.

- [x] Add phase-parametrized tests for intent open/write/fsync, mkdir/open/fstat, temp open/write/fsync, replace/parent fsync, and fchmod; assert no subsequent claim syscall begins.
- [x] Add public `install()` tests proving recovery-budget expiry and pre-identity setup failures surface only as `ReleaseInstallError` and leave restart-safe state.
- [x] Run the tests and verify raw `TimeoutError`/`AssertionError` or extra post-expiry calls are the RED causes.
- [x] Add callback parameters and typed exception conversion; replace reservation finalization assertions with safe typed bookkeeping for intent-only states.
- [x] Re-run restart inspection and all crash-window tests.

### Task 3: Inode-bound recovery completion and leaf cleanup

**Files:**
- Modify: `agent/tests/test_releases.py`
- Modify: `agent/src/dgx_agent/releases.py`

**Interfaces:**
- Consumes: captured intent/leaf `(st_dev, st_ino)` identities.
- Produces: atomic quarantine-and-verify helpers that never delete a changed name.

- [x] Add a same-name intent substitution test around completion; require the replacement to survive and completion to fail closed.
- [x] Add file, symlink, and hardlink cleanup substitution tests; require foreign bytes/inodes to survive.
- [x] Run the tests and record `os.replace` overwrite and stat-then-unlink as the RED causes.
- [x] Complete records only after atomically quarantining and verifying the captured intent inode; remove all leaves through atomic quarantine, opened/fstat identity comparison, and exact-type policy.
- [x] Re-run publication/root swap, sidecar swap, empty-directory swap, and foreign staging tests.

### Task 4: Durable `.recovery-*.new` restart resolution

**Files:**
- Modify: `agent/tests/test_releases.py`
- Modify: `agent/src/dgx_agent/releases.py`

**Interfaces:**
- Consumes: canonical complete temp record bytes and exact temp inode metadata.
- Produces: preflight/inspection promotion or authenticated cleanup without permanent temp/quarantine accumulation.

- [x] Add fresh-installer tests for a valid durable temp before replace, repeated restart cycles, corrupt temp, and substituted temp.
- [x] Run tests and record that current preflight ignores `.new` and accumulates it.
- [x] Parse temp records with the same exact owner/mode/canonical rules, bind them to staging identity, and promote/use or exact-inode-remove them; preserve invalid/substituted temp entries fail closed.
- [x] Re-run all recovery backlog and inspection classifications.

### Task 5: Evidence, gates, and commit

**Files:**
- Modify: `.superpowers/sdd/2026-08-03-spark-agent-runtime/task-3-report.md`
- Modify: `.superpowers/sdd/2026-08-03-spark-agent-runtime/progress.md`

**Interfaces:**
- Consumes: final command output.
- Produces: reproducible round-3 evidence and one commit SHA.

- [x] Run focused release; release+workload; release+workload+operations; full agent; Compose; Compose config with `--env-file deploy/compose/tests/test.env`; scoped Ruff; compileall; build; fresh installed-wheel typed parsing; and diff-check.
- [x] Update the report’s deadline statement and exact Compose command, append the delivered ledger line while preserving the controller line, and record exact counts.
- [x] Commit with a Task 3 round-3 fix message and verify clean status plus exact HEAD.
