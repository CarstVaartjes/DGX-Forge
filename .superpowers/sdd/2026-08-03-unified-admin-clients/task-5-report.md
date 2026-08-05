# Task 5 report: Unified plan and job experience

## Status

DONE. The web profile workflow previews the canonical server-issued plan, joins it to live fleet/agent acceptance evidence, and submits only the exact confirmed digest. The jobs workflow displays bounded parent and node-operation progress and exposes resume only for `waiting-for-operator` jobs. The CLI and browser contract equivalence test uses one disposable FastAPI application.

## TDD evidence

### Initial RED

Command:

```text
npm --prefix control/web test -- --run && uv run pytest tests/e2e/test_admin_equivalence.py -v
```

The `&&` correctly stopped after Vitest failed. Exact summary and primary failure:

```text
FAIL  src/components/reconciliation-plan.test.tsx [ src/components/reconciliation-plan.test.tsx ]
Error: Failed to resolve import "./reconciliation-plan" from "src/components/reconciliation-plan.test.tsx". Does the file exist?

Test Files  3 failed | 3 passed (6)
Tests  4 failed | 20 passed (24)
EXIT_CODE=1
```

The profile tests failed because `Profile ID to reconcile` and `Preview exact plan` did not exist. The job tests failed because `View job`, parent/operation progress, pagination, and resume did not exist. These failures were the intended missing behaviors.

The newly created Python integration was also run independently. Its first collection run exposed a test-environment problem rather than a product behavior:

```text
ModuleNotFoundError: No module named 'dgx_control'
```

The final harness does not depend on machine-local site paths. Root pytest launches the live check through the pinned `control` project using `uv run --project control --frozen`, so it is portable to a clean root CI environment.

### Initial GREEN

Command:

```text
npm --prefix control/web test -- --run src/components/reconciliation-plan.test.tsx src/pages/profiles.test.tsx src/pages/jobs.test.tsx
```

Exact output:

```text
Test Files  3 passed (3)
Tests  6 passed (6)
EXIT_CODE=0
```

### Stale-digest hardening RED

Self-review identified that an apply 409 displayed an alert but left the old exact confirmation reusable. A regression test was added before the lockout implementation.

Command:

```text
npm --prefix control/web test -- --run src/components/reconciliation-plan.test.tsx
```

Exact failure:

```text
FAIL  src/components/reconciliation-plan.test.tsx > locks a rejected stale digest until the operator previews a new plan
Expected element to have text content:
  /preview a new plan/i
Received:
  Control API returned 409: reconciliation plan digest is stale

Test Files  1 failed (1)
Tests  1 failed | 2 passed (3)
EXIT_CODE=1
```

After implementation, the same command produced:

```text
Test Files  1 passed (1)
Tests  3 passed (3)
EXIT_CODE=0
```

## Implementation

- `control/web/src/api/types.ts`
  - Replaced the handwritten job summary with generated schema aliases.
  - Added generated aliases for job detail/resume and reconciliation plan/acceptance.
  - Extended `ControlApi` with typed plan, apply, job detail, and resume operations.
- `control/web/src/api/client.ts`
  - Added `openapi-fetch` wrappers for profile plan, exact-digest apply, job list/detail, and resume.
  - Preserved same-origin credentials and the existing CSRF middleware.
  - Added bounded typed API error detail so 409 conflicts are explicit without rendering arbitrary objects.
- `control/web/src/components/reconciliation-plan.tsx`
  - Displays repository commit/digest, affected nodes, placement, operation kinds and dependencies, immutable release hashes, routes/quotas, pinned inputs, protocol compatibility, and live node acceptance gates.
  - Fails closed for missing, unhealthy/unknown, stale, offline, inactive, incompatible, commit-mismatched, or invalid-protocol evidence.
  - Requires the operator to type the exact digest.
  - Locks a rejected apply until a fresh plan is previewed.
  - Paginates every dynamic rendered collection and bounds every server-controlled string.
- `control/web/src/pages/profiles.tsx`
  - Adds profile-scoped canonical planning beside the existing Git-backed editor.
  - Fetches the server plan and live fleet together; it contains no browser planner or local fallback.
- `control/web/src/pages/jobs.tsx`
  - Adds paginated job, affected-node, and node-operation views.
  - Bounds progress counters and all strings; never renders payload/result objects or HTML.
  - Exposes resume only while the server reports `waiting-for-operator`.
- `control/web/src/styles.css`
  - Adds focused layout styles for plan/job evidence and native progress.
- `control/web/src/components/reconciliation-plan.test.tsx`
  - Covers canonical evidence, exact digest apply, unavailable-node fail-closed gates, secret hostname non-rendering, and stale 409 lockout.
- `control/web/src/pages/profiles.test.tsx`
  - Covers profile-scoped planning, live fleet gates, accessible errors, and removal of an old digest after conflict.
- `control/web/src/pages/jobs.test.tsx`
  - Covers parent/operation progress, pagination, and operator resume.
- `control/web/e2e/admin.spec.ts`
  - Covers keyboard navigation and the real browser submission body `{plan_digest: digest}` after typed confirmation.
- `tests/e2e/test_admin_equivalence.py`
  - Against one disposable real FastAPI app, compares CLI and browser-route commit, digest, targets, and operations; verifies stale 409, exact digest acceptance, and unavailable-node evidence.

## Final verification

### Required Phase 5 command

Command:

```text
uv run pytest tests/spark_profiles/test_agent_cli.py tests/e2e/test_admin_equivalence.py -q && npm --prefix control/web test -- --run && npm --prefix control/web run build && git diff --check
```

Exact output summary:

```text
...........................                                              [100%]
27 passed in 13.12s

Test Files  6 passed (6)
Tests  27 passed (27)

vite v8.2.0 building client environment for production...
✓ 28 modules transformed.
dist/index.html                   0.35 kB │ gzip:  0.26 kB
dist/assets/index-OUIY8fDB.css    3.67 kB │ gzip:  1.35 kB
dist/assets/index--244cx5r.js   235.16 kB │ gzip: 71.23 kB
✓ built in 94ms
EXIT_CODE=0
```

`git diff --check` emitted no output and exited zero.

### Relevant API and generated-client tests

Command:

```text
uv run --project control --frozen pytest control/tests/test_operation_api.py control/tests/test_admin_api.py tests/control/test_openapi_clients.py -q && uv run pytest tests/spark_profiles/test_control_client.py -q && git diff --exit-code -- control/openapi.json src/spark_profiles/generated_control control/web/src/api/generated.d.ts && uvx --from ruff==0.16.1 ruff check .
```

Exact output:

```text
........................                                                 [100%]
24 passed in 4.69s
........................................................................ [ 78%]
....................                                                     [100%]
92 passed in 1.32s
All checks passed!
EXIT_CODE=0
```

The generation drift command emitted no output and exited zero.

### Playwright

The host Chromium initially failed before test execution because `libnspr4.so` was absent. NSS/NSPR Debian packages were downloaded and extracted into a temporary directory, and `LD_LIBRARY_PATH` was set only for the test command. No system or repository packages were changed.

Exact final output:

```text
Running 2 tests using 1 worker
✓  1 e2e/admin.spec.ts:7:1 › admin shell is keyboard navigable
✓  2 e2e/admin.spec.ts:17:1 › profile apply confirms and posts the exact server digest
2 passed (1.7s)
EXIT_CODE=0
```

## Self-review

- Security: no raw HTML, arbitrary result object, job payload/result, hostname, secret, certificate body, or private key is rendered. API errors and all remote strings are bounded. Exact digest is sent through the generated mutation with existing CSRF/session behavior.
- Fail closed: missing/unknown/unhealthy/stale/offline/inactive/incompatible target state, fleet/plan commit mismatch, invalid protocol range, stale 409, and request errors never enable mutation.
- Bounded output: jobs, targets, operations, plan targets, dependencies, placements, release entries, route entries/nodes, and input digests are paginated. Numeric progress is finite, non-negative, and capped for rendering. There is no hard total fleet limit in the UI.
- Accessibility: semantic headings, sections, tables, row/column scopes, labels, alerts/status, native progress, disabled states, and keyboard-operable controls are present. Playwright confirms keyboard navigation and the confirmation flow.
- API-only invariant: routine CLI remains on generated HTTPS operations; web uses generated types/operations. No SSH, local controller, controller-side planner, host/IP constant, Spark name, or fleet-size assumption was added.
- Test quality: assertions exercise real React components, generated client boundaries, one disposable FastAPI app, and a browser flow. No mock-existence or source-grep assertions were added.

## Concerns

None in the implementation. The only environment concern was the host's missing NSS/NSPR shared libraries for Chromium; the browser suite passed with temporary extracted libraries and CI runners normally provide Playwright's documented dependencies.

---

## Fix round 1/5 (review findings)

### Status

DONE. The critical real-client equivalence finding and all four Important findings are fixed. The route-display Minor was intentionally ledgered and not changed in this round.

### RED evidence

Authoritative live evidence was first specified at the API boundary:

```text
uv run --project control pytest control/tests/test_operation_api.py -q
3 failed, 9 passed
```

The exact failures were two `KeyError: 'fleet_evidence_digest'` results on plan/apply and one `KeyError: 'evidence_digest'` on fleet projection. The UI evidence/invariant slice then failed as intended:

```text
npm --prefix control/web test -- --run src/components/reconciliation-plan.test.tsx
Test Files  1 failed (1)
Tests  10 failed | 2 passed (12)
```

All seven malformed-authority cases remained applyable (empty-target commit mismatch, graph commit mismatch, graph target omission, duplicate targets, zero/fractional/reversed protocol bounds), and both evidence-refresh tests submitted instead of locking out on changed/unavailable evidence.

Keyset pagination RED was exact:

```text
uv run --project control pytest control/tests/test_jobs.py::test_job_list_keyset_pages_reach_every_job_in_stable_order -q
AttributeError: 'JobService' object has no attribute 'list_page'

npm --prefix control/web test -- --run src/pages/jobs.test.tsx
Tests  1 failed | 1 passed (2)
```

The browser showed `Showing jobs 1–20 of 20` for a first page whose authoritative response was `{total: 23, next_cursor: "jobs-2"}`, leaving the later page unreachable.

The resume-conflict component regression failed because a 409 left the stale `waiting-for-operator` detail and Resume button visible and performed no authoritative detail refetch. For the server race, the first conditional-update edit preceded its test. This was corrected transparently with an explicit regression cycle: only the atomic implementation was temporarily reverted to the prior read-then-write behavior and the isolated SQLite race was run:

```text
uv run --project control pytest control/tests/test_operation_api.py::test_durable_resume_has_one_atomic_winner -q
FAILED: assert 4 == 1
```

Four of eight callers reported success. Restoring the single conditional update and rerunning the identical command produced `1 passed` with one winner and seven conflicts.

The mandatory real-client harness RED crossed the live API and generated CLI boundary, then failed at the missing browser runner:

```text
uv run pytest tests/spark_profiles/test_admin_equivalence.py -q
No test files found, exiting with code 1
filter: src/admin-equivalence.live.test.tsx
```

The root test was subsequently renamed to `test_live_admin_client_equivalence.py` to avoid Python's duplicate-basename import collision with the legacy `tests/e2e` module.

### GREEN evidence

- Evidence/API contract: `control/tests/test_operation_api.py` — 12/12 focused evidence tests passed; the final affected operation/jobs group passed 25/25.
- Plan integrity and refresh: `reconciliation-plan.test.tsx` — 12/12 passed.
- Server-cursor job UI and 409 authority refresh: `jobs.test.tsx` — 3/3 passed.
- Generated Python client and CLI caller reconciliation: 92/92 control-client tests and 60/60 CLI tests passed.
- Real equivalence: `test_live_admin_client_equivalence.py` — 1/1 passed against one disposable HTTP FastAPI server.
- SQLite atomic resume and operation traversal: 2/2 focused tests passed; the old transition admitted four winners, the fixed transition exactly one.
- Disposable PostgreSQL conditional resume: 1/1 focused and the complete `test_agent_jobs_postgres.py` suite 22/22 passed.

### Fix details

- Added a deterministic SHA-256 digest over the validated public fleet acceptance projection. Both fleet endpoints expose the same `evidence_digest`; every plan pins it as `fleet_evidence_digest`; apply requires and rechecks both the exact plan digest and exact current fleet evidence before enqueue/audit.
- Regenerated the pinned Python and TypeScript clients. CLI apply and browser apply now send the generated `ReconciliationRequest` with both exact digests.
- Added authority-independent plan checks for fleet/plan commit and evidence, positive integral ordered protocol bounds, graph/base commit equality, duplicate-free top-level/graph targets, exact graph-target equality, and operation-node membership.
- Refreshes fleet immediately before apply. Changed or unavailable evidence clears confirmation and locks the old plan. A server 409 also locks the plan.
- Replaced fixed job/operation caps with bounded cursor APIs. Jobs use newest-first `(created_at,id)` keysets; node operations use oldest-first `(created_at,id)` keysets; targets use bounded opaque cursors. Responses expose authoritative totals and next cursors, while operation progress is aggregated over all durable operations rather than the visible page.
- Bounded list query `cursor`/`limit` plus target/status filters and bounded all generated job, target, operation, cursor, reason, state, and identifier fields.
- Replaced both durable resume paths with one conditional `waiting-for-operator -> queued` update. Only the row-count winner succeeds and reaches audit; losers return 409. The web client invalidates stale detail on any resume error and refetches authority.
- Added a live test server and Node/jsdom runner. The generated Python `ControlClient` and actual browser `ApiClient` plus rendered `ProfilesPage` use the same disposable real API. The test compares commit, digest, targets, and operation graph; captures the exact generated apply JSON; induces a real evidence race yielding 409; and asserts an unavailable node is visibly blocked without rendering its hostname.

### Final verification

Required Phase-5 group:

```text
uv run pytest tests/spark_profiles/test_agent_cli.py tests/e2e/test_admin_equivalence.py tests/spark_profiles/test_live_admin_client_equivalence.py -q
28 passed in 15.74s
```

Full affected control and PostgreSQL verification:

```text
uv run --project control pytest control/tests -q
697 passed in 96.44s

uv run --project control pytest control/tests/test_agent_jobs_postgres.py -q
22 passed in 7.28s

uv run --project control pytest control/tests/test_admin_api.py tests/control/test_openapi_clients.py -q
14 passed in 4.33s
```

Web and build:

```text
npm --prefix control/web test -- --run
Test Files  6 passed | 1 skipped (7)
Tests  37 passed | 1 skipped (38)

npm --prefix control/web run build
✓ built in 89ms
```

The one skipped Vitest case is the environment-gated live runner; root pytest executes it with a disposable API and it passes. Playwright initially could not launch because the host lacks `libnspr4.so`. Using the already extracted temporary NSS/NSPR directory, without changing the system or repository:

```text
LD_LIBRARY_PATH=/tmp/tmp.Pyl8UnvzDJ/extracted/usr/lib/x86_64-linux-gnu npm --prefix control/web run test:e2e
2 passed (1.8s)
```

Deterministic regeneration produced identical before/after diff digests:

```text
drift_digest_before=21f646c087a502b8a20467e596cf2e49d52d8d90cc9dd402eaf8ef56eb7db3d8
drift_digest_after=21f646c087a502b8a20467e596cf2e49d52d8d90cc9dd402eaf8ef56eb7db3d8
```

Repository Ruff reported `All checks passed!`. `git diff --check` is part of the final commit gate below.

### Fix-round concerns

No implementation concern. The only environment concern remains the host's missing Chromium NSS/NSPR libraries; Playwright passed with the existing temporary extracted libraries. The server race test was added after the first atomic edit, so the report explicitly records the requested revert/RED/restore/GREEN cycle rather than implying a sequence that did not occur.

During post-commit verification, `control/web/.pypirc` appeared transiently as an untracked test artifact and was already absent at the next status check. Its contents were never staged, inspected, or committed. Final status contains only the controller-owned `.superpowers/.../progress.md` modification.

---

## Fix round 2/5 (canonical fleet authority and authenticated cursors)

### RED evidence

The initial focused contract established the two missing authorities:

```text
uv run --project control pytest -q \
  control/tests/test_jobs.py::test_job_list_cursor_is_authenticated_and_filter_bound \
  control/tests/test_jobs.py::test_job_list_rejects_syntactically_valid_cursor_forged_with_other_key \
  control/tests/test_reconcile.py::test_live_fleet_evidence_is_part_of_canonical_resolved_plan_digest \
  control/tests/test_reconcile.py::test_enqueue_rejects_fresh_evidence_paired_with_old_plan \
  control/tests/test_reconcile.py::test_evidence_change_at_enqueue_barrier_cannot_create_job \
  control/tests/test_agent_reconciliation.py::test_authority_change_after_enqueue_stops_before_first_route_side_effect

4 failed, 2 errors in 1.73s
```

The failures were exact: `TokenCodec` had no cursor codec, `Reconciler.plan` did not accept canonical fleet evidence, `Reconciler.enqueue` had no exact-pair/guarded authority, and a queued reconciliation did not enter the explicit safe state before withdrawal.

The first full control pass after implementation exposed one further authenticity edge: changing only unused base64url padding bits could decode to the same HMAC bytes.

```text
1 failed, 706 passed in 93.09s
```

The decoder was tightened to require exact decode/re-encode equality for payload and signature. The tamper regression then passed 20 consecutive isolated runs.

### Fix details

- Fleet evidence is validated before desired-state planning, is a canonical field in the resolved plan document, changes the canonical plan digest, and is persisted in both `Reconciliation.resolved_plan` and the parent reconciliation job payload.
- Apply resolves the stored plan/evidence pair, rejects a current digest paired with a different old plan, and uses a database transaction that checks live evidence before insert and after flush. A barrier change rolls the transaction back without creating a job.
- The authenticated worker authority now compares the stored plan-bound fleet digest with live fleet evidence twice, signs `fleet_evidence_current` in its bounded response, and caches that exact decision. Loss becomes the explicit reason `fleet acceptance evidence changed since planning`.
- The worker continuous-authority gate runs before initial withdrawal, dispatch, node mutation, or publication. A new queued reconciliation with changed evidence enters `waiting-for-operator` with no route or node effect. Existing predecessor-publication handoffs retain their fail-closed maintenance withdrawal before waiting.
- Cursor keys are derived with HMAC domain separation from the configured durable control token signing key; no new or hard-coded secret was introduced.
- Job, operation, and target cursors are versioned, bounded, authenticated, canonical base64url tokens. They bind resource kind, ordering version, normalized status/target context, job identity where applicable, and the exact keyset/offset boundary.
- Service/API/generated-client tests reject tampering, alternate encodings, oversized tokens, different-key forgeries, cross-filter replay, cross-job replay, and cross-resource replay while preserving keyset completeness and concurrent-newer-insert semantics.

### Final verification

Focused canonical authority and cursor boundary:

```text
6 passed in 1.17s
25 passed in 0.71s
124 passed in 12.08s
```

Final full control suite:

```text
uv run --project control pytest control/tests -q --tb=short
708 passed in 92.15s
```

Standalone disposable PostgreSQL job suite:

```text
uv run --project control pytest control/tests/test_agent_jobs_postgres.py -q
22 passed in 6.87s
```

Required Phase 5 and live equivalence group:

```text
uv run pytest tests/spark_profiles/test_agent_cli.py tests/e2e/test_admin_equivalence.py tests/spark_profiles/test_live_admin_client_equivalence.py -q
28 passed in 15.82s
```

Generated/OpenAPI and CLI callers:

```text
uv run --project control pytest control/tests/test_admin_api.py tests/control/test_openapi_clients.py -q
15 passed in 4.30s

uv run pytest tests/spark_profiles/test_control_client.py -q
92 passed in 1.33s
```

Web and browser:

```text
npm --prefix control/web test -- --run
Test Files  6 passed | 1 skipped (7)
Tests  37 passed | 1 skipped (38)

npm --prefix control/web run build
✓ built in 108ms

LD_LIBRARY_PATH=/tmp/tmp.Pyl8UnvzDJ/extracted/usr/lib/x86_64-linux-gnu npm --prefix control/web run test:e2e
2 passed (1.8s)
```

Deterministic generation, repository-wide Ruff, and diff validation:

```text
scripts/generate-control-clients
git diff --exit-code -- control/openapi.json src/spark_profiles/generated_control control/web/src/api/generated.d.ts
uvx --from ruff==0.16.1 ruff check .
git diff --check

All checks passed; generated drift and diff checks emitted no differences.
```

### Concerns

None. Chromium still needs the host's previously documented temporary NSS/NSPR library path; no system or repository dependency was changed.

---

## Publication fix after hosted CI run 31033481023

### Ancestry and scope

The publication fix began from integrated commit `a527ebe`; `git merge-base --is-ancestor a527ebe HEAD` exited zero. Only the unified-admin-clients worktree was changed. The controller-owned `progress.md` remained untouched and unstaged.

### RED evidence

The strengthened lifecycle regression required two distinct canonical 64-character fleet evidence digests to be reported by the real two-reconciliation acceptance path. Before implementation:

```text
uv run pytest tests/e2e/test_platform_lifecycle.py::test_repository_to_running_profile_and_safe_withdrawal -q

FAILED tests/e2e/test_platform_lifecycle.py::test_repository_to_running_profile_and_safe_withdrawal
ValueError: desired-state planning requires fleet evidence
1 failed in 0.68s
```

The workflow regression required both locked JavaScript workspaces before the shared matrix pytest step and asserted that the job covers `ubuntu-latest` and `macos-latest`. Before implementation:

```text
uv run pytest tests/test_ci_platform_boundaries.py::test_full_matrix_installs_locked_javascript_workspaces_before_pytest -q

FAILED tests/test_ci_platform_boundaries.py::test_full_matrix_installs_locked_javascript_workspaces_before_pytest
AssertionError: Install locked admin web dependencies step was absent
1 failed in 0.02s
```

### Implementation

- `scripts/accept-platform-lifecycle` now derives current evidence through the production `DashboardService` and validated `fleet_response` projection for both the initial and replacement repository revisions.
- Each plan receives that exact digest, each guarded enqueue rechecks the same live projection, and the acceptance script verifies the resolved plan and persisted parent job payload carry the same bound digest.
- The lifecycle report exposes the two evidence digests; the behavioral test verifies they are canonical and distinct across repository revisions.
- The shared Linux/macOS test matrix now runs frozen `npm ci --prefix control/web` before root pytest, in addition to the pinned generator workspace install.
- The workflow boundary test executes in pytest, verifies exact commands and ordering, and confirms both hosted matrix operating systems remain covered.

### GREEN evidence

Focused regressions:

```text
uv run pytest tests/e2e/test_platform_lifecycle.py::test_repository_to_running_profile_and_safe_withdrawal tests/test_ci_platform_boundaries.py::test_full_matrix_installs_locked_javascript_workspaces_before_pytest -q
2 passed in 1.33s
```

Exact locked web install plus complete workflow/platform boundary group:

```text
npm ci --prefix control/web
added 122 packages; found 0 vulnerabilities

uv run pytest tests/test_ci_platform_boundaries.py tests/e2e/test_platform_lifecycle.py -q
6 passed in 2.24s
```

Phase 5 and live equivalence group:

```text
uv run pytest tests/spark_profiles/test_agent_cli.py tests/e2e/test_admin_equivalence.py tests/spark_profiles/test_live_admin_client_equivalence.py -q
28 passed in 15.76s
```

Deterministic clients, Ruff, and diff gates:

```text
scripts/generate-control-clients
git diff --exit-code -- control/openapi.json src/spark_profiles/generated_control control/web/src/api/generated.d.ts
uvx --from ruff==0.16.1 ruff check .
git diff --check

All checks passed; generated drift and diff checks emitted no differences.
```

Final combined publication gate:

```text
uv run pytest tests/e2e/test_platform_lifecycle.py tests/test_ci_platform_boundaries.py tests/spark_profiles/test_agent_cli.py tests/e2e/test_admin_equivalence.py tests/spark_profiles/test_live_admin_client_equivalence.py -q
34 passed in 18.48s
```

### Concerns

None.
