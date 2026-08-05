# Task 4 report: agent enrollment and fleet pages

## Outcome

Implemented the integrated `/agents` administration surface and expanded the current Fleet page around the authoritative generated OpenAPI contract. Administrators can inspect bounded agent and enrollment evidence, create one-time grants, confirm approvals, type the immutable node ID before rejection or certificate revocation, and follow same-origin links to native LiteLLM and Grafana surfaces.

The generated declaration was consumed without hand edits. Agent, enrollment, and fleet DTO aliases point directly at `components["schemas"]`, and all agent/fleet HTTP calls use `openapi-fetch<paths>` so path, body, response, and status drift is compiler-visible.

## TDD evidence

Each workflow test names the production regression it catches in a leading comment and asserts rendered behavior through the real page and `ApiClient`. The only fake is `fetch`; its success payloads contain every generated field. The enrollment list fake also injects forbidden extra secret/address fields to prove the page projects an explicit allowlist rather than rendering raw payloads.

### Initial RED: missing integrated page

Command:

```text
npm --prefix control/web test -- --run src/pages/agents.test.tsx
```

Observed:

```text
FAIL src/pages/agents.test.tsx
Error: Failed to resolve import "./agents" from "src/pages/agents.test.tsx".
Test Files  1 failed (1)
Tests       no tests
```

This was the intended failure: the page/component did not exist before production code was added.

### Focused GREEN: primary enrollment workflows

Command:

```text
npm --prefix control/web test -- --run src/pages/agents.test.tsx
```

Observed after the minimal implementation:

```text
Test Files  1 passed (1)
Tests       4 passed (4)
```

The suite was then expanded to cover actual application navigation and the typed current Fleet page.

### Contract-boundary RED/GREEN: grant TTL

Systematic contract comparison found that the UI allowed `3600` seconds while the authoritative API `GrantRequest` caps `ttl_seconds` at `600`.

RED command:

```text
npm --prefix control/web test -- --run src/pages/agents.test.tsx
```

RED result after correcting an unrelated fixture selector:

```text
Test Files  1 failed (1)
Tests       1 failed | 5 passed (6)
Expected max="600"; received max="3600"
```

Minimal fix: change the grant lifetime input to `min="1" max="600"` and the safe default to `300`.

GREEN result:

```text
Test Files  1 passed (1)
Tests       6 passed (6)
```

### Existing-suite integration failure

The first full-suite run exposed one obsolete test boundary fake:

```text
Test Files  1 failed | 2 passed (3)
Tests       1 failed | 5 passed (6)
TypeError: Cannot read properties of undefined (reading 'get')
```

Root cause: `openapi-fetch` correctly consumes a real `Response`, while the old fake returned a partial `{ok, json}` object. The test fake now returns a real `Response` with the complete generated Fleet response and asserts observable request URL, credentials, and CSRF behavior instead of mock existence.

## Files changed

- `control/web/src/api/client.ts`: generated `openapi-fetch` operations, same-origin credentials, and CSRF middleware for mutations.
- `control/web/src/api/client.test.ts`: real network-boundary response fakes and same-origin/CSRF coverage.
- `control/web/src/api/types.ts`: generated component aliases and typed agent API surface; no parallel agent DTOs.
- `control/web/src/app.tsx`: registered `/agents` and primary navigation entry while preserving existing routes.
- `control/web/src/components/enrollment-review.tsx`: bounded evidence/audit projection and explicit approval/rejection workflows.
- `control/web/src/pages/agents.tsx`: grant, agent status, certificate revocation, enrollment review, and native-surface links.
- `control/web/src/pages/agents.test.tsx`: six rendered workflow/integration tests plus a compile-time secret-field assertion.
- `control/web/src/pages/fleet.tsx`: generated `NodeStatus` fields for agent state, last seen, certificate expiry, and compatibility.
- `control/web/src/styles.css`: focus-visible, responsive table, evidence, status, and irreversible-action styling.

`control/web/src/api/generated.d.ts` was regenerated only for drift verification and remained byte-for-byte unchanged.

## Verification

Generated-client drift:

```text
scripts/generate-control-clients
git diff --exit-code -- control/openapi.json control/web/src/api/generated.d.ts src/spark_profiles/generated_control
```

Result: exit 0; `openapi-typescript 7.13.0` regenerated the declaration with no diff.

Pinned repository lint, diff hygiene, full web tests, and production build:

```text
uvx --from ruff==0.16.1 ruff check .
git diff --check
npm --prefix control/web test -- --run
npm --prefix control/web run build
```

Observed:

```text
All checks passed!
Test Files  3 passed (3)
Tests       9 passed (9)
tsc --noEmit: passed
vite v8.2.0: 27 modules transformed; built in 81ms
```

## Accessibility self-review

- The page uses a unique `h2`, ordered section headings, labelled regions, a captioned semantic agent table, column/row headers, definition lists for evidence, and native labelled inputs/buttons.
- Primary navigation is exercised by focusing the Agents link and activating it with Enter; the evidence checkbox is exercised with Space and approval with Enter.
- Disabled states prevent confirmation bypass, while errors use `role="alert"` and progress/success/token messages use `role="status"`.
- All interactive elements have high-contrast `:focus-visible` outlines; wide tables scroll horizontally and layout collapses on narrow screens.

## Security and bounded-output self-review

- Production renderers contain no address, private-key, certificate body/chain, CSR body, or grant-history field references. A source scan for those fields, hard-coded hosts/IPs, and arbitrary `JSON.stringify` rendering returned no matches.
- Enrollment and agent output is field-by-field allowlisted from generated bounded summaries. Only the CSR **public-key fingerprint**, never the CSR, is displayed.
- The grant token exists only in local state set from `createEnrollmentGrant`'s single successful response. Refresh and dismiss clear it; list/state payloads cannot restore it. The test injects a fake list token and proves it never renders.
- Approval requires checked evidence comparison. Rejection and certificate revocation require an exact immutable-node-ID entry and display irreversible warnings.
- All generated requests retain `credentials: "same-origin"`; mutations copy the existing `dgx_csrf` cookie into `X-CSRF-Token`.
- Native links accept only configured same-origin paths. Cross-origin configured values fall back to same-origin paths, and no hostname/IP is embedded. LiteLLM remains limited to keys/teams/spend; the page states that Git-backed model definitions remain authoritative and renders no LiteLLM records.
- The generated audit endpoint currently exposes an unbounded object rather than a bounded event schema, so this page deliberately renders only the bounded decision audit fields present in `EnrollmentSummary`.

## Concerns

- Deployments that mount the LiteLLM Admin UI somewhere other than `/litellm/ui/` must set `VITE_LITELLM_ADMIN_PATH` to the Caddy-routed same-origin path. Grafana defaults to the repository's existing `/grafana/` route and can be overridden with `VITE_GRAFANA_PATH`; cross-origin values are rejected by the page.
- No schema/client drift or implementation blocker remains.

## Fix round 1

Addressed every Important finding in `task-4-review-1.md`.

### Native LiteLLM administration

The deployed native UI now has a real same-origin route at `/litellm/ui/`.
Compose supplies `SERVER_ROOT_PATH=/litellm`, enables the UI, and gives its
runtime asset population a writable `/tmp/litellm-ui` path while retaining the
read-only container filesystem. Caddy routes `/litellm/*` to LiteLLM before the
control SPA and retains the existing `/v1/*` inference route. Mutating model,
model-group, and config paths return 403, and every bootstrap/runtime config
sets `store_model_in_db: false`, preserving the repository as model authority.

This follows the official [LiteLLM Admin UI documentation](https://docs.litellm.ai/docs/proxy/ui),
which places the UI at `<proxy_base_url>/ui` and requires the proxy master key
and database. The pinned v1.82.3 source confirms
[`SERVER_ROOT_PATH` is the supported proxy prefix](https://raw.githubusercontent.com/BerriAI/litellm/v1.82.3-stable/litellm/proxy/utils.py),
the [UI is mounted at `/ui` and `LITELLM_UI_PATH` supports a writable runtime asset directory](https://raw.githubusercontent.com/BerriAI/litellm/v1.82.3-stable/litellm/proxy/proxy_server.py),
and the pinned [model management routes](https://raw.githubusercontent.com/BerriAI/litellm/v1.82.3-stable/litellm/proxy/management_endpoints/model_management_endpoints.py)
use `/model/*`, which the Caddy guard covers.

RED:

```text
uv run --project control --frozen pytest deploy/compose/tests/test_litellm_admin.py control/tests/test_litellm.py control/tests/test_route_runtime.py -q
5 failed, 26 passed
```

The failures showed the missing environment/root path and Caddy route plus UI-disabled generated/static configs. GREEN after the deployment and publisher changes:

```text
31 passed in 0.90s
```

The final route-only check, strengthened to require every guarded path and
mutation method in adapted Caddy JSON, passes `3 passed in 0.46s`.

### CSRF padding and one-time grant lifecycle

The cookie parser now slices only at the first delimiter, preserving values
ending in one or multiple `=` characters among neighboring cookies. The grant
workflow admits one request at a time, forwards an `AbortSignal` through the
generated client, disables creation until the displayed token is dismissed,
and aborts/invalidates the request on refresh or unmount. A stale response can
therefore neither replace nor resurrect a one-time token.

CSRF RED was three failures receiving `nonce` instead of `nonce=`, `nonce==`,
or `nonce=middle==`; GREEN was `5 passed`. Grant RED was three failures proving
the button was not locked and refresh/unmount did not abort; combined grant and
client GREEN was `14 passed` at that boundary.

### Bounded rendering and snapshot-bound confirmations

Agent output is paged in chunks of 20. Each visible agent renders at most three
capabilities at a time, and each capability is capped at 80 characters. Counts
and previous/next controls are labelled for assistive technology, and only the
visible page's certificate controls are mounted. The regression fixture uses 45
agents, eight capabilities per agent, and 10,000-character capability input;
every record remains reachable without mounting the full collection.

The bounded-render RED was `1 failed, 9 passed` because no agent count existed
and the full collection was still mounted; GREEN was `10 passed`.

Approval, rejection, and revocation controls are keyed by a successful-load
revision plus the immutable enrollment/node identity and the relevant current
evidence/certificate fields. Refresh therefore clears all typed/checked values,
including when the server returns the same snapshot, while changed evidence or
certificate state necessarily creates a fresh control instance. Completed
revocation removes the control immediately.

Snapshot RED was `2 failed, 9 passed`: a changed host-key snapshot retained the
checked approval, and a revoked certificate retained an enabled action. The
combined page/client GREEN is now `2 files, 16 passed`.

### Final verification and diagnostic history

```text
npm --prefix control/web test -- --run
3 files, 17 passed

npm --prefix control/web run build
tsc --noEmit passed; vite built 27 modules in 92ms

uv run --project control --frozen pytest deploy/compose/tests/test_litellm_admin.py control/tests/test_litellm.py control/tests/test_route_runtime.py -q
31 passed in 0.90s

uvx --from ruff==0.16.1 ruff check .
All checks passed!

scripts/generate-control-clients
git diff --exit-code -- control/openapi.json control/web/src/api/generated.d.ts src/spark_profiles/generated_control
exit 0; no generated drift

git diff --check
exit 0
```

The first complete Python/Compose run reported `755 passed, 2 failed`. Both
Docker image tests failed because the image-internal `npm run build` found that
TypeScript 7 requires an explicit initial value for `useRef`; production code
was corrected to initialize the optional request ref with `undefined`, after
which the standalone production build passed.

The second complete run again reported `755 passed, 2 failed`, but both builds
now passed the web layer and died later in the same image-internal pip install
with legacy-builder exit signals 139 and 134. Systematic follow-up found no
stable product failure: each exact test passed sequentially (`1 passed in
13.22s`, then `1 passed in 0.46s`); Docker reported 11 GiB available memory,
897 GiB free storage, and no daemon OOM/error event; and an exact uncached
24-step worker build completed the formerly failing pip layer and image build.
No product change was made for those non-reproducible container exits.

The definitive third full run was green:

```text
uv run --project control --frozen pytest control/tests deploy/compose/tests -q
757 passed in 90.62s
```

## Fix round 2

Addressed both Important findings in the second review.

### Complete direct agent-summary string bounds

The capability-only helper was generalized to require an explicit maximum at
every call site. Direct agent-summary output now uses contract-appropriate
limits: the generated node-ID pattern fixes IDs at 36 characters, grant tokens
and timestamps use 64, state and compatibility use 64, capabilities retain 80,
and error/status messages use 512. Null/empty summary values still render as an
em dash. Bounding changes presentation only: records remain in their existing
20-row pages and are never filtered because a field is oversized.

The focused fixture supplies distinct 10,000-character state, last-seen,
certificate-expiry, and fleet-compatibility strings. It asserts the same node
row remains present, each field is visibly truncated, each affected cell stays
below 128 rendered characters, and no complete oversized value reaches the DOM.

### Immediate refresh confirmation invalidation

Refresh now advances the confirmation revision synchronously before beginning
the three reload requests. The evidence and certificate controls therefore
remount with empty local state on the first refresh render. All approval,
rejection, and revocation inputs/actions are also disabled while reload is in
progress. If reload fails, the prior records may remain visible for continuity,
but the new control instances contain no checked or typed confirmation that can
be reused.

The API fake gates only second and later GETs, preserving the real initial page
load while allowing pending and rejected refresh states to be tested through
the real page and generated client.

### TDD evidence

The first failed-refresh assertion initially used an ambiguous generic alert
selector because the page intentionally contains irreversible-action alerts.
That selector was corrected before production changes so the test failed on the
actual stale confirmation behavior.

Correct RED:

```text
npm --prefix control/web test -- --run src/pages/agents.test.tsx
Test Files  1 failed (1)
Tests       3 failed | 11 passed (14)
```

The three intended failures were:

- the oversized summary row contained no truncation marker and mounted the full state string;
- a pending refresh retained the checked approval confirmation;
- a failed refresh retained the checked approval confirmation.

Focused GREEN after the minimal renderer and refresh changes:

```text
npm --prefix control/web test -- --run src/pages/agents.test.tsx
Test Files  1 passed (1)
Tests       14 passed (14)
```

### Verification

```text
npm --prefix control/web test -- --run
Test Files  3 passed (3)
Tests       20 passed (20)

npm --prefix control/web run build
tsc --noEmit passed
vite v8.2.0 built 27 modules in 118ms

uvx --from ruff==0.16.1 ruff check .
All checks passed!

scripts/generate-control-clients
git diff --exit-code -- control/openapi.json control/web/src/api/generated.d.ts src/spark_profiles/generated_control
exit 0; no generated drift

git diff --check
exit 0
```

No Python or Compose production/test file changed in this round, so the
requested relevant-Python/Compose condition did not apply.
