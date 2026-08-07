# Task 1 report — identity regression guard

Status: DONE_WITH_CONCERNS

Commit: `ee30804e22fcfbb9e36a175443ec8bce67929dc7` (`test: add Vonk identity regression guard`)

## Delivered

- `scripts/vonk_identity.py` exports `verify(root: Path)` and produces a
  deterministic report with `status`, `owned_matches`, and
  `external_matches`.
- `scripts/verify-vonk-identity` is an executable CLI wrapper.  `--json`
  emits stable, key-sorted compact JSON and returns 1 for owned matches.
- The scanner omits Git metadata, common caches, symlinks, and binary/non-UTF-8
  files.  It classifies the three explicit evidence roots as external instead
  of allowing them to fail the guard: `manifests/`, `inventory/raw/`, and
  `tests/fixtures/external/`.
- `tests/scripts/test_verify_vonk_identity.py` is the specified regression
  test for an owned `sparkctl` token.

## Verification

- Red test: `uv run --isolated --with pytest==9.1.1 pytest tests/scripts/test_verify_vonk_identity.py -q`
  before implementation failed during collection with
  `ModuleNotFoundError: No module named 'scripts.vonk_identity'`.
- Focused test after implementation: the same command passed: `1 passed in
  0.01s`.
- `python3 -m py_compile scripts/vonk_identity.py scripts/verify-vonk-identity`
  exited 0.
- `git diff --check` exited 0 before the commit.
- Inventory: `scripts/verify-vonk-identity --json` completed with expected
  exit status 1, `status=failed`, `owned_matches=97098`, and
  `external_matches=6`.  The bounded JSON SHA-256 was
  `b7611accd348b1daf27a6a262f4ee3621dd99ee0a58264f2c4c335580bba5496` on two
  consecutive runs, confirming deterministic ordering.  The full JSON is
  intentionally not copied here because current one-line evidence and source
  files make it about 19 MiB.

## Concerns

- The brief's literal command, `uv run pytest tests/scripts/test_verify_vonk_identity.py -q`, could not spawn `pytest` in this checkout (`No such file or directory`).  Its existing `.venv` points at the different `/home/carst/DGX-Forge/.venv` location.  The isolated `uv` invocation above supplied the pinned declared pytest version and exercised the requested test successfully without modifying the worktree environment.
- The future all-clean pass needs an explicit policy for task-history files and
  the regression test source itself: both contain intentional legacy tokens,
  but neither is under the stated external-evidence roots.  As written, the
  guard will continue to report them until later cleanup work either removes
  those occurrences or gives them an explicit, documented handling rule.

## Fix round 1 — 2026-08-07

Status: COMPLETE

Commit: `0c4ea4f` (`test: harden Vonk identity guard`)

- Generalized skipped generated directories now cover cache/build naming,
  bytecode caches, and virtual environments.
- The scan now inventories directory and regular-text file names, while
  excluding binary or encoded artifact names before they can create a false
  match. Known suffixes, common file signatures, NUL bytes, and control-byte
  content identify those artifacts.
- Tests cover all external roots, path scanning with deterministic ordering,
  generic cache/build and binary/encoded exclusions, compact sorted JSON, and
  the CLI's nonzero owned-match exit. Test probes construct retired identities
  at runtime; `docs/identity-verifier.md` records that policy.

### Verification output

```text
$ uv run --isolated --with pytest==9.1.1 pytest tests/scripts/test_verify_vonk_identity.py -q
.......                                                                  [100%]
7 passed in 0.05s

$ uv run --isolated --with ruff==0.16.1 ruff check scripts/vonk_identity.py tests/scripts/test_verify_vonk_identity.py
All checks passed!

$ python3 -m py_compile scripts/vonk_identity.py scripts/verify-vonk-identity
exit 0

$ scripts/verify-vonk-identity --json .
exit 1 (expected while the repository cleanup is incomplete)
identity guard: status=failed owned=8621 external=10
```

`git diff --check` passed before committing. The focused owned test, verifier
module, CLI wrapper, and policy document contain no literal retired tokens.

## Fix round 2 — 2026-08-07

Status: COMPLETE

Commit: current round-2 commit (hash reported in the parent handoff)

- Removed the named scan exemptions for `.worktrees/` and `.superpowers/`.
- A Git checkout now enumerates exactly the tracked and untracked,
  non-ignored paths returned by
  `git ls-files --cached --others --exclude-standard -z`. Parent directories
  are derived from those paths so visible directory names remain covered.
- Arbitrary non-Git roots retain filesystem walking with the generic
  cache/build, dependency, metadata, and virtual-environment exclusions.
- The regression test covers cached content, an ordinary untracked path,
  non-ignored dot-directories, and an ignored scratch path. Probe identities
  remain runtime-constructed rather than literal test content.
- `docs/identity-verifier.md` now documents Git visibility as the checkout
  policy instead of describing special work-record or sibling-checkout
  exemptions.

### Red/green and verification output

```text
$ uv run --isolated --with pytest==9.1.1 pytest tests/scripts/test_verify_vonk_identity.py::test_identity_verifier_uses_git_visibility_for_checkout_roots -q
1 failed in 0.03s
# Expected red: visible dot-directory paths were absent and ignored scratch
# content was incorrectly reported.

$ uv run --isolated --with pytest==9.1.1 pytest tests/scripts/test_verify_vonk_identity.py -q
........                                                                 [100%]
8 passed in 0.06s

$ uv run --isolated --with ruff==0.16.1 ruff check scripts/vonk_identity.py tests/scripts/test_verify_vonk_identity.py
All checks passed!

$ python3 -m py_compile scripts/vonk_identity.py scripts/verify-vonk-identity
exit 0

$ scripts/verify-vonk-identity --json .
exit=1 status=failed owned=8942 external=10
# Expected while the repository identity cleanup is incomplete.

$ git diff --check
exit 0
```

## Fix round 3 — 2026-08-07

Status: COMPLETE

- Git-visible paths now apply the same generic skipped-directory policy as
  filesystem walking before files or derived parent directory names are
  inspected. Tracked or unignored cache/build, dependency, bytecode-cache, and
  virtual-environment paths therefore cannot bypass the exclusion.
- The Git checkout regression tracks representative `build/`,
  `compiler-cache/`, `__pycache__/`, `node_modules/`, and `.venv/` paths and
  confirms none are reported. It retains coverage for a visible
  `.superpowers/` path and an ignored scratch path; its temporary checkout is
  pytest-managed and self-cleaning.
- `docs/identity-verifier.md` now states that Git ignores control ignored-file
  visibility only; generic cache/build, dependency, virtual-environment, and
  binary-artifact exclusions remain unconditional.

### Verification output

```text
$ uv run --isolated --with pytest==9.1.1 pytest tests/scripts/test_verify_vonk_identity.py -q
........                                                                 [100%]
8 passed in 0.06s

$ uv run --isolated --with ruff==0.16.1 ruff check scripts/vonk_identity.py tests/scripts/test_verify_vonk_identity.py docs/identity-verifier.md
All checks passed!

$ python3 -m py_compile scripts/vonk_identity.py scripts/verify-vonk-identity
exit 0

$ scripts/verify-vonk-identity --json . >/dev/null
exit 1 (expected while repository identity cleanup remains incomplete)

$ git diff --check
exit 0
```
