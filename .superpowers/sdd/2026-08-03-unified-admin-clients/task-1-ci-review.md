### Spec/Fix Verdict

- ADDRESSED — the `test` matrix covers Ubuntu and macOS (`.github/workflows/ci.yml:45`), runs exact locked `npm ci --prefix tools/openapi-client` before root pytest (`.github/workflows/ci.yml:63`, `.github/workflows/ci.yml:94`), and dependencies are exact in both manifest and lockfile (`tools/openapi-client/package.json:5`, `tools/openapi-client/package-lock.json:4`).
- The regression test structurally isolates the named `test` job, matches exact step names and commands, and asserts ordering (`tests/test_ci_platform_boundaries.py:12`, `tests/test_ci_platform_boundaries.py:33`). Reported RED/GREEN confirms it failed on the prior workflow and passed after the fix (`task-1-report.md:549`). Generator tests were not weakened and pass separately (`task-1-report.md:564`).

### New Breakage

- None.

### Verdict

**Publication fix:** Approved.
