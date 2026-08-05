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

## Independent review correction

The initial independent review found two Important issues and one Minor issue:

1. Enrollment left `AgentNode.last_seen_at` and `protocol_version` unset, while the
   authenticated claim, heartbeat, and result paths only read identity state. The
   operational projection therefore rendered real production nodes with no last-seen
   series and the `incompatible` version bucket.
2. The stale-agent rule filtered the missing-series branch to active nodes but allowed
   an old last-seen series for a retired node to fire.
3. The standard node-exporter and DCGM panels displayed the transport-oriented
   `instance` label rather than the stable `node_id` identity.

The correction adds protocol version 1 to the real agent claim request. The control
claim model accepts a strict positive integer version and rejects booleans. After the
API has authenticated the certificate and bound the body to that identity,
`AgentJobService.claim` records contact in the same transaction as its active
certificate check and claim decision. Heartbeat and result record contact only after
the exact operation, attempt, fence, lease, certificate, and node have passed `_active`
validation, in the same transaction as progress/result persistence. The stored
last-seen timestamp never moves backward. Unauthenticated requests, invalid protocol
advertisements, cross-node bodies, and stale fences do not update either field.

The corrected `SparkAgentStale` expression builds the old-age and active-without-series
candidates, then applies an outer `and on(node_id)` active-state filter. Its behavioral
test parses that structure and evaluates active/retired stale, fresh, and never-seen
fixtures; only active-stale and active-never nodes fire. The standard exporter legends
now use `{{node_id}}`, retaining bounded `{{gpu}}` for DCGM series.

### Correction RED/GREEN evidence

Before the correction's production edits:

- The exact agent-client claim test failed because `protocol_version` was absent.
- Four selected production API tests completed as 3 failed and 1 passed: claim rejected
  the new version field, valid heartbeat/result calls left both durable fields unset,
  and the existing unauthenticated/stale non-update behavior remained intact.
- Two selected observability tests failed because the alert had no outer active-state
  join and both exporter legends used `instance`.
- A follow-up strict-type regression failed because Pydantic coerced boolean `true` to
  protocol version 1 and recorded contact.

The same regressions then passed as 1 agent-client, 4 production-path, 2 observability,
and 1 strict-type test.

### Correction verification

- `agent/tests/test_client.py`: **39 passed**.
- Focused control API/job/metrics/dashboard suites: **84 passed**.
- Complete control suite: **297 passed**.
- Compose observability suite: **8 passed**.
- Focused pinned Ruff, direct `py_compile`, JSON validation, and diff checks passed.
- The first complete-control attempt recorded 296 passes and one existing Docker image
  build failure while pip fetched dependencies. Replaying the exact pinned Docker build
  succeeded, and the immediate complete-control rerun passed all 297 tests.
- `promtool` is not installed locally, so the existing limitation remains: the rule is
  structurally parsed and behaviorally exercised in Python, but no local Prometheus
  binary check is claimed.

The correction addresses all submitted findings. Independent rereview remains pending,
as does integration with Tasks 1–4.

## Independent rereview correction

The independent rereview found two Important transaction-boundary defects:

1. Heartbeat and result copied their protocol message `schema_version` into
   `AgentNode.protocol_version`. A node that advertised agent protocol version 2 on
   claim was therefore downgraded to version 1 by its next schema-version-1 progress
   or result message.
2. Agent identity state was read before the node row was locked. Revocation could
   retire the node after that read but before contact or claim/heartbeat/result state
   was written, allowing post-retirement contact and work mutation.

Heartbeat and result now update last-seen recency only; claim remains the sole writer
of the advertised agent protocol version. The identity transaction order is now
`AgentNode`, relevant `AgentCertificate`, operation, attempt, then parent job. Both
identity rows are locked before active/not-revoked/time-valid state is evaluated and
before contact or operation state can change. `revoke_node` uses the same leading
order and locks all of a node's certificate rows in deterministic serial order before
retiring the node and certificates.

The PostgreSQL regression runs the real `EnrollmentService.revoke_node`, pauses it
after its node `FOR UPDATE` lock, and starts each of claim, heartbeat, and result in a
separate service thread. Each operation is proven to wait for revocation, then reject
the committed retired identity without changing last-seen, lease/progress/result, or
operation state. SQLite unit coverage independently proves retired identities cannot
mutate an active attempt or record contact. Existing invalid-authentication and stale
fence no-write coverage remains green.

### Rereview RED/GREEN evidence

Before the production correction:

- both protocol-retention API regressions failed because heartbeat/result changed
  advertised version 2 to schema version 1; and
- all three PostgreSQL revocation races failed because claim issued work, heartbeat
  renewed work, and result completed work after their stale pre-lock identity read.

After the correction, the same protocol regressions passed 2/2 and the exact
PostgreSQL races passed 3/3. The new SQLite retired-identity no-write cases passed 2/2.

### Rereview correction verification

- `agent/tests/test_client.py`: **39 passed**.
- Focused API/job/PostgreSQL/metrics/dashboard suites: **106 passed**, including all
  **20 PostgreSQL locking tests**.
- Complete control suite: **302 passed**.
- Compose observability suite: **8 passed**.
- Focused pinned Ruff 0.16.1, direct `py_compile`, JSON parsing, and
  `git diff --check` passed.
- `promtool` remains unavailable locally; the rules continue to receive structural
  parsing and behavioral observability-test coverage.

The rereview correction addresses both submitted findings. Independent rereview and
integration with Tasks 1–4 remain pending.

## Rereview round 3 correction

Round 3 identified two remaining lock-order and post-issuance retirement defects:

1. `AgentJobService.enqueue` locked the parent `Job` before it even read the target
   `AgentNode`. Its eventual operation insert also acquired implicit foreign-key
   protection on the node, inverting completion's node-first transaction order.
2. Rotation persistence locked `AgentCertificateRotation` before its certificate
   insert's implicit node dependency. If the CA had completed renewal while a
   concurrent revocation retired the node, persistence could commit the newly issued
   serial as staged after revocation had already selected the serials to revoke.

Enqueue now locks and validates an active node before locking the parent job. The
deterministic PostgreSQL regression pauses enqueue immediately after that first node
lock, starts same-node completion, and proves completion orders behind enqueue without
deadlock. Enqueue commits its sibling before completion aggregates the parent, so the
completed first operation is durable while the parent correctly remains queued.

The agent-job transaction audit now has no Job-before-Node path:

- enqueue is node, parent job, then operation insert;
- claim is node, certificate, operation, attempt, then optional parent aggregation;
- heartbeat/result are node, certificate, operation, attempt, then optional parent
  aggregation.

Rotation claim, persistence, uncertainty annotation, revocation, and remote-revocation
confirmation now share node, serial-ordered certificate rows, then rotation-intent
ordering. Activation already began with the node and certificate rows. Initial
enrollment remains a separate absent-node issuance domain protected by its durable
enrollment row and PostgreSQL node-specific advisory lock; it does not lock an
existing job or agent node in reverse order.

When retirement wins after the CA has issued a renewal, persistence now records the
new certificate as locally revoked with a revocation timestamp and retains the
rotation intent as `revocation-pending`. Only after that denied audit state commits is
the new serial sent to the CA revocation boundary. Provider confirmation records
`ca_revoked_at` and advances the intent to `revoked`; the serial is never active or
staged. If the process dies during the provider call, the durable pending certificate
is included by the normal administrator `revoke_node` reconciliation. Repeated retry
calls invoke the provider once for the still-unconfirmed serial and then become a
no-op, preserving the CA interface's idempotent contract.

### Round 3 RED/GREEN evidence

Before production edits:

- the SQLite retired-node enqueue regression failed because enqueue accepted work;
- the PostgreSQL enqueue-order regression failed because enqueue never acquired the
  expected node row lock; and
- both completed-CA rotation races failed because renewal returned serial 2 as staged
  after retirement, without invoking its remote revocation path.

After the correction, those regressions passed 1/1, 1/1, and 2/2 respectively. The
crash variant proves the pending local disposition survives `SystemExit`, the first
administrator retry confirms the new serial, and the second retry performs no remote
call.

### Round 3 correction verification

- Focused job/API/enrollment/PostgreSQL suites: **122 passed**.
- Complete control suite: **306 passed**.
- `agent/tests/test_client.py`: **39 passed**.
- Compose observability suite: **8 passed**.
- Focused pinned Ruff 0.16.1, direct `py_compile`, JSON parsing, and
  `git diff --check` passed.
- `promtool` remains unavailable locally; rules retain structural parsing and
  behavioral observability-test coverage.

Round 3's submitted findings are addressed. Independent rereview and integration with
Tasks 1–4 remain pending.

## Rereview round 4 correction

Round 4 identified the final corrupt-state gap in post-issuance recovery: if all
dependent rows and then `AgentNode` disappeared after the CA returned a renewal but
before rotation persistence, `_persist_rotation` raised and uncertainty annotation
had no surviving node-owned intent to update. The new CA serial was therefore neither
locally recorded nor explicitly revoked.

The deletion audit found no application or administrator service that deletes
`AgentNode`; supported lifecycle removal is retirement. The schema's NO ACTION
foreign keys from approved certificates and agent operations prevent normal deletion,
and a PostgreSQL regression proves an approved node cannot be deleted while its
certificate exists. The new migration additionally installs a database trigger that
rejects every `AgentNode` delete and directs callers to retirement, including nodes
that currently have no protecting dependent row. Rotation intent is the only
node-owned identity row with cascade deletion. The missing-node scenario therefore
requires corruption, migration bypass, or direct removal before the retire-only
trigger exists, but is handled defensively rather than assumed impossible.

The bounded recovery table `agent_issued_certificate_revocations` is deliberately
independent of `AgentNode`. Each row is uniquely bounded by CA serial and provider
request ID and stores only node ID, certificate fingerprint, generation, state, and
timestamps. It stores no private key, certificate or chain PEM, CSR, token, payload,
or other unbounded document. Confirmed rows remain as the audit proof that the serial
was revoked; exceptional pending rows are directly queryable by state and node.

When persistence observes a missing node, it commits `revocation-pending` evidence
before calling the CA. Successful CA revocation marks that evidence `revoked` with
`ca_revoked_at`. `RuntimeError` and process death leave the pending row intact. The
normal administrator `revoke_node` path now reconciles node-independent evidence even
when the node no longer exists: its first retry calls the CA for each unconfirmed
serial, while later retries see confirmation and perform no remote call. The CA
boundary remains idempotent if a process died after the provider acted but before the
local confirmation committed.

### Round 4 migration integration

This isolated branch predates the incoming reconciliation migrations. The recovery
migration therefore uses the non-conflicting provisional revision
`round4_issued_revocations`, based on this branch's `0005_certificate_rotation` head.
During linear integration it must be reparented to the final incoming migration head
and numerically renamed after `0006`/`0007`; it must not be merged as a parallel
production head. Both the current SQLite chain and an actual PostgreSQL
upgrade-to-head, downgrade-to-0005, upgrade-to-head sequence pass with exact model
parity.

### Round 4 RED/GREEN evidence

Before production edits, the targeted missing-node recovery suite failed during
collection because `AgentIssuedCertificateRevocation` did not exist. That structural
RED captured the underlying defect: there was no node-independent durable location
for the returned CA serial. A subsequent PostgreSQL migration regression also failed
because a dependency-free node could still be deleted rather than retired.

After the correction, deterministic PostgreSQL cases pass for immediate CA success,
provider `RuntimeError`, and `SystemExit`. Each deletes the dependent certificate and
node only after the CA has completed serial 2 issuance. Success records remote
confirmation immediately; both failures retain pending evidence, and administrator
retry confirms the serial exactly once before becoming a no-op. Both the separate
approved-node foreign-key guard and the migrated PostgreSQL retire-only trigger pass.

### Round 4 correction verification

- Focused migration/enrollment/job/PostgreSQL/API suites: **134 passed**.
- Complete control suite: **312 passed**.
- `agent/tests/test_client.py`: **39 passed**.
- Compose observability suite: **8 passed**.
- SQLite migration reversibility/model parity and real PostgreSQL
  upgrade/downgrade/upgrade verification passed.
- Focused pinned Ruff 0.16.1, direct `py_compile`, JSON parsing, and
  `git diff --check` passed.
- `promtool` remains unavailable locally; rules retain structural parsing and
  behavioral observability-test coverage.

Round 4's submitted finding is addressed. Independent rereview and linear integration
with the incoming migration chain remain pending.
