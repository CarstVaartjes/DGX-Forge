# Task 5 report — agent metrics and operational visibility

Date: 2026-08-05
Branch: `feature/agent-observability`
Base: `64e19e9952d5b449ee2e0a6f255501580a71a911`
Brief: `.superpowers/sdd/2026-08-03-agent-control-reconciliation/task-5-brief.md`

## Outcome

Task 5 projects outbound-agent operations from the durable tables that already exist.
It does not introduce or assume the Task 1–4 reconciliation graph schema. Production
metrics refresh now reads `AgentNode`, active unrevoked `AgentCertificate`,
`AgentOperation`, the current running `AgentOperationAttempt`, and reconciliation
`Job` rows.

The OpenMetrics output adds:

- `dgx_agent_state` by stable node ID and bounded state;
- `dgx_agent_version_compatibility` by stable node ID and one of `supported`, `old`,
  `new`, or `incompatible`;
- `dgx_agent_last_seen_age_seconds` and
  `dgx_agent_certificate_expiry_seconds` by stable node ID;
- `dgx_agent_operations` by closed protocol operation and bounded state;
- `dgx_agent_operation_lease_age_seconds` by stable node ID and closed operation;
- `dgx_agent_rollouts` by the state of existing durable reconciliation jobs.

Unknown operation/state values collapse to `other`, missing or invalid protocol
versions collapse to `incompatible`, and every refresh replaces the previous snapshot
so removed or changed rows do not leave stale series. Labels do not contain job IDs,
certificate material or serials, addresses, errors, actors, or payload content.

The fleet projection adds agent state, ISO last-seen time, last-seen age, active
certificate expiry time and remaining seconds, and protocol compatibility without
returning certificate serials or fingerprints. The production metrics endpoint invokes
the durable projection during its existing authenticated refresh.

Prometheus adds stale-agent, expiring-certificate, repeated-operation-failure, and
paused-rollout alerts, each with an existing HTTPS runbook destination. The versioned
fleet dashboard covers every new agent series. Its host memory and GPU panels consume
the standard `node_memory_MemAvailable_bytes` and `DCGM_FI_DEV_GPU_UTIL` series; no
host/GPU collector or measurement was added to the agent protocol.

## Strict TDD evidence

Before any production edit:

- `uv run --project control pytest control/tests/test_agent_metrics.py -v` exited 2
  during collection because `OperationalMetricsCollector` did not exist.
- `uv run pytest deploy/compose/tests/test_observability.py -v` completed as 3 passed
  and 3 failed. The failures named the four missing agent alerts, the absent agent
  lifecycle dashboard series, and the absent standard node/DCGM dashboard queries.

After the minimum implementation, the same focused commands completed as 3 passed and
6 passed respectively.

## Verification

- `uv run --project control pytest control/tests/test_agent_metrics.py
  control/tests/test_metrics.py control/tests/test_dashboard.py
  control/tests/test_agent_jobs.py control/tests/test_agent_api.py -q`: **79 passed**.
- `uv run --project control pytest control/tests -q`: **292 passed**.
- `uv run pytest deploy/compose/tests/test_observability.py -q`: **6 passed**.
- Focused Ruff 0.16.1 over the changed Python source and tests: **All checks passed**.
- Direct `py_compile` over the changed Python source and tests exited zero.
- Both changed observability documents parsed as JSON (and therefore valid YAML for
  the Prometheus rules document); `git diff --check` was clean.

## Boundaries and limitations

- The plan's exact combined Phase 4 command could not run on this parallel branch:
  pytest exited 4 because `control/tests/test_desired_state.py`,
  `control/tests/test_agent_reconciliation.py`, and
  `control/tests/test_production_worker.py` do not exist. No tests ran in that command.
  This task did not fabricate those files or their future persistence tables.
- Rollout state currently projects the compatible existing `Job(kind="reconcile")`
  state. A later orchestration graph may supply the same bounded state interface
  without changing metric labels or dashboards.
- Protocol version 1 is the current supported range. Collector and dashboard
  constructors accept an explicit future minimum/maximum, but this task does not add
  an unapproved runtime policy source.
- The Grafana panels consume the standard node-exporter and DCGM series expected from
  Alloy remote write. Provisioning those Spark-side exporters/Alloy pipelines is not
  part of this task, so physical host/GPU data is not claimed here.
- The repository has no local `promtool`; rule artifacts received structural JSON/YAML
  and behavioral Compose-test validation, not a local Prometheus binary check.

Task 5 implementation is complete for its scoped interfaces. Independent review and
integration with Tasks 1–4 remain pending.
