# Shared inference and observability overlap audit

Audit bases: `main` at `4261b1a`, NAS worktree at `e79cee1`, and agent
observability worktree at `6fb9a86`. This was a read-only review; no audited
worktree was modified.

## Task 1 — atomic routes

Main already has canonical atomic `RoutePublisher` generations, maintenance,
allowlisted upstream validation, snapshots, and focused tests. Remaining work is
to bind publication to the accepted endpoint evidence produced by agent-control
Task 3, define the chosen static-Caddy/dynamic-LiteLLM contract explicitly, and
prove known-good boot maintenance plus public 503 behavior. NAS presence/probe
evidence alone must not become route authority.

## Task 2 — LiteLLM

Main has a pinned private service, secret references, bounded policy, repository
alias authority, and immutable config rendering. NAS adds the missing leased live
config bridge and supervisor. Remaining work is to port that bridge through the
final orchestrator, expose the native LiteLLM Admin UI only through Caddy, and
validate generations with LiteLLM itself. Both audited configurations currently
disable the Admin UI, so this task is not accepted.

## Task 3 — metrics transport

Main has authenticated bounded control-plane metrics, a private Prometheus
scrape, and retention limits. It does not have the Spark-side exporter pipeline:
pinned node exporter and DCGM exporter, loopback-only services, Alloy, outbound
mTLS remote-write, aggressive label removal, installer integration, the Caddy
receiver route, or acceptance tests. These are material missing deliverables.

## Task 4 — dashboards and alerts

The pinned private Grafana/Prometheus foundation, provisioning, dashboards,
runbook alerts, and Compose tests exist. The active agent-observability branch
adds agent lifecycle/certificate/lease/rollout and standard node/DCGM views, but
it is in a review fix loop and temporarily derives rollouts from reconciliation
jobs. Acceptance waits for the final reconciliation schema and live Task 3
telemetry series.

## Task 5 — bounded logs

Redaction, bounded content-addressed evidence, authorized job-log resources,
Docker log rotation, and the runbook exist. Production code does not yet call the
structured logger, so request/job/actor correlation is not live. Final API
authorization and every post-NAS service also require verification.

## Conflict-safe integration order

1. Complete agent-control Tasks 2-4.
2. Rebase the agent-observability changes and use the final reconciliation schema.
3. Split and rebase NAS work; do not merge its worker/runtime/routes changes wholesale.
4. Close route and LiteLLM production/UI/maintenance gaps.
5. Implement the per-Spark Alloy/node/DCGM pipeline.
6. Revalidate dashboards and alerts, then integrate production structured logging.

The audit found strong reusable foundations, but recommends accepting none of
Tasks 1-5 yet.
