# Unified admin clients preflight

Tasks 1–5 begin after agent-control Tasks 2–4 are accepted on `main`. Starting
earlier would duplicate API routes and regenerate clients around an unstable
reconciliation/publication contract.

## Canonical API decisions

- Keep `POST /api/v1/reconciliations/plan` with `{commit, profile_id}`.
- Keep `/api/v1/fleet` as the node-status collection.
- Add `/api/v1/endpoints/{alias}` over Task 3's activated publication marker.
- Add `/api/v1/agents` for bounded registered-agent state; retain existing
  enrollment, grant, approval, rejection, and revocation routes.
- Enrich the existing job resource with typed child operations and progress;
  do not create a second job API or expose generic arbitrary enqueue.
- `catalog` and `status` are routine API-backed commands. `prepare` becomes a
  deprecated plan-only alias; only `switch --apply` submits reconciliation.
- `break-stale-lock` belongs only in `sparkctl-legacy` or offline tooling.

## Security and typing

- Generate an admin-only OpenAPI document containing `/api/v1/*`; exclude
  `/agent/v1/*` so generated browser types never expose certificate responses.
- Use stable operation IDs and strict request/response models. Server-side
  profile listing owns canonical IDs and selector resolution.
- Agent resources exclude CSR/certificate bodies, grant digests, management
  addresses, payloads, and private evidence. Grant tokens appear once in
  ephemeral component state and never enter URLs, storage, or logs.
- Audit grant creation, enrollment decisions, revocation, reconciliation apply,
  and operator resume.
- Harden `ControlClient` origin, token-file ownership/mode/link checks, content
  type, response bounds, bounded `Retry-After`, typed failures, and explicit
  mutation request IDs. Never automatically replay an ambiguous mutation.
- Missing observations render `healthy=null`, `stale=true`, and age `null`.
  Browser output is bounded plain text with no raw HTML or unrestricted result
  objects.

## Safe execution order

1. Typed API, tracked OpenAPI, idempotent client generator, generated Python and
   TypeScript clients, CI drift/security tests.
2. In parallel: hardened Python polling client and the fleet/agents/enrollment
   web experience.
3. Cut routine CLI commands over to the API; put old direct behavior behind an
   explicit `sparkctl-legacy` launcher with no automatic fallback.
4. Add exact plan/DAG/job/operator-wait/publication UI and prove CLI/web resource
   equivalence.

Conflict hotspots are `api.py`, agent/auth/dashboard services, route/LiteLLM
publication, Docker/Compose/settings/worker, and administration documentation.
Create every implementation worktree from the newly accepted `main`; do not
merge the NAS branch wholesale.

## Acceptance matrix

- API: exact plan digest, bounded 404/409/503 behavior, activated-marker-only
  endpoints, RBAC/CSRF, secret-free agents, idempotent generator.
- Client: typed 401/403/404/409/503, malformed/oversized responses, bounded
  polling, timeout, operator wait, and no replay after ambiguous mutation.
- CLI: every routine command works while direct SSH/controller constructors are
  patched to raise; API failure has no fallback.
- Agents UI: accessible review, one-time token, evidence comparison, confirmed
  approve/reject/revoke, and no certificate bodies.
- Plan/jobs UI: byte-equivalent commit/digest/targets/graph between CLI and web,
  exact-digest apply, parent/child progress, authorized resume, and fail-closed
  unavailable nodes/routes.
- Hosted CI adds full control, web unit/build/browser, generated-client drift,
  root tests, and Ruff gates.
