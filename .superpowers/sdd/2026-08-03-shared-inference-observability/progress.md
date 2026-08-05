# SDD ledger — plan: docs/superpowers/plans/2026-08-03-shared-inference-observability.md

Overlap audit: complete at main 4261b1a, NAS e79cee1, and agent-observability 6fb9a86; no Tasks 1-5 are accepted yet
Task 1: partial (main has atomic canonical RoutePublisher generations; final accepted-endpoint binding, explicit boot maintenance/503 behavior, and chosen static-Caddy contract remain)
Task 2: partial (main has pinned private LiteLLM and repository-authority rendering; NAS has the live bridge; native Caddy-protected Admin UI and real config validation remain)
Task 3: incomplete (main control metrics exist; per-Spark pinned node exporter/DCGM/Alloy units, loopback bindings, mTLS remote-write, relabeling, installer integration, Caddy receiver route, and acceptance tests remain)
Task 4: near-complete but unaccepted (agent lifecycle extensions are in active reviewed/fix-loop work; final reconciliation schema and live node/DCGM series remain)
Task 5: near-complete but unaccepted (redaction/evidence/rotation exist; production correlated structured-log callers and final-service verification remain)
Execution order: finish agent-control Tasks 2-4; adapt agent observability to final reconciliation schema; split/rebase NAS route work; close routes/LiteLLM; implement per-Spark telemetry; revalidate dashboards/alerts; integrate structured logging
