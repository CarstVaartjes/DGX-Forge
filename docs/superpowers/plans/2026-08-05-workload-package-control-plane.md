# Workload Package Control Plane Implementation Plan

> **Implementation status (2026-08-06): core surfaces complete.** W11–W16 are
> implemented on `main`, including migration `0014_package_action_plans`,
> discovery/resolution, API/CLI parity, web administration, bounded metrics,
> digest-bound removal/GC dispatch, and TUF-authorized rollout dispatch through
> the existing agent-job queue. Release publication, validation, and promotion
> remain explicitly trust/worker-gated until their signer and runner boundaries
> are installed. See
> the [roadmap status ledger](2026-08-05-generalized-workload-package-roadmap.md#implementation-status-2026-08-06).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover, resolve, validate, promote, plan, and reconcile generic workload packages from the NAS through the outbound Spark agent, with equivalent API, CLI, and web administration.

**Architecture:** Git remains desired-state authority while PostgreSQL records candidates, resolver/validation results, rollout attempts, progress, and observations. The control plane resolves metadata into immutable locks, publishes authorized locks through workload TUF, and sends only exact package/deployment digests through the existing fenced agent queue. API, CLI, and web are thin views over the same services and canonical plan bytes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, HTTPX, generated OpenAPI clients, React/TypeScript, Prometheus, pytest, Vitest

## Global Constraints

- PostgreSQL cannot create or modify a deployable package family, promoted lock, deployment, or fleet desired state.
- Discovery is automatic according to family policy; execution of newly discovered code is not.
- Resolution is metadata-only on the NAS and never downloads multi-gigabyte payloads to invent identities.
- Candidates that are unsupported, incompatible, quarantined, or rejected remain visible with structured reasons.
- Manual promotion is the default; automation uses the same Git, trust, validation, canary, audit, and rollback gates.
- Reconciliation sends an exact promoted release digest and deployment digest and never an adapter name, command, path, floating version, or mutable tag.
- No production workload operation falls back to SSH.
- Existing platform `agent.update` remains exclusively for DGX-Forge agent/platform releases.

---

### Task W11: Operational package state and migration

**Files:**
- Modify: `control/src/dgx_control/models.py`
- Create: `control/migrations/versions/0013_workload_packages.py`
- Test: `control/tests/test_workload_package_migration.py`
- Modify: `control/tests/test_migrations.py`

**Interfaces:**
- Adds `PackageCandidate`, `PackageResolution`, `PackageValidationRun`, `PackageRollout`, `PackageRolloutNode`, and `PackageObservation`.
- Unique identities bind candidate family/upstream identity/metadata digest, resolution candidate/resolver/schema, and rollout deployment/release/base commit/plan digest.
- Immutable promoted release bytes remain in Git/TUF; database rows store their digest and operational projection only.

- [ ] **Step 1: Write RED migration and authority tests**

Test upgrade/downgrade, sole Alembic head, uniqueness/concurrency, exact check constraints, cascade boundaries, redacted failure size, no payload blobs, and inability to represent a promoted release without a Git commit plus TUF target digest.

```python
assert candidate.state in {"discovered", "resolving", "resolved", "unsupported", "quarantined", "rejected"}
assert rollout.release_digest == "a" * 64
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project control --frozen pytest control/tests/test_workload_package_migration.py control/tests/test_migrations.py -v`

Expected: FAIL because revision `0013` and package models are absent.

- [ ] **Step 3: Implement normalized operational state**

Use foreign keys and uniqueness for retry-safe discovery/resolution/validation/rollout. Store bounded JSON evidence/progress, typed state columns, timestamps, actor/automation identity, exact Git/TUF/release/policy digests, retry disposition, and node operation IDs. Do not store source credentials, signed URLs, lock payloads, model bytes, image layers, wheels, or arbitrary logs.
Set `down_revision = "0012_control_process_heartbeats"`; do not branch from the
superseded pre-update migration number.

```python
class PackageCandidate(Base):
    __tablename__ = "package_candidates"
    id: Mapped[str]
    family_id: Mapped[str]
    upstream_identity_digest: Mapped[str]
    metadata_digest: Mapped[str]
    state: Mapped[str]
    reason_code: Mapped[str | None]
```

- [ ] **Step 4: Verify both database engines and migration cycles**

Run: `uv run --project control --frozen pytest control/tests/test_workload_package_migration.py control/tests/test_migrations.py control/tests/test_agent_migrations.py -q`

Expected: PASS on SQLite contract tests and configured PostgreSQL migration tests, with `0013` as the only head after platform-update revision `0012_control_process_heartbeats`.

- [ ] **Step 5: Commit W11**

```bash
git add control/src/dgx_control/models.py control/migrations/versions/0013_workload_packages.py control/tests/test_workload_package_migration.py control/tests/test_migrations.py
git commit -m "feat: persist workload package operations"
```

### Task W12: Generic discovery and deterministic resolution

**Files:**
- Create: `control/src/dgx_control/package_discovery.py`
- Create: `control/src/dgx_control/package_resolution.py`
- Create: `control/src/dgx_control/package_providers.py`
- Modify: `control/src/dgx_control/worker.py`
- Test: `control/tests/test_package_discovery.py`
- Test: `control/tests/test_package_resolution.py`
- Test: `control/tests/test_package_providers.py`

**Interfaces:**
- Produces `DiscoveryProvider.discover(family, cursor) -> DiscoveryPage`, `CandidateService.poll(family_id)`, and `PackageResolver.resolve(candidate_id, family, dependencies) -> ResolutionResult`.
- Initial provider protocols are Git releases/tags, OCI tags/indexes, Hugging Face repositories/full revisions, Python indexes, and signed HTTP indexes.
- A successful `ResolutionResult.lock` is a `PackageReleaseLock`; failure contains one stable taxonomy code and bounded redacted context.

- [ ] **Step 1: Write RED provider and resolution tests**

Cover durable cursors/conditional requests, rate limits, retries, tag/version reuse, moved tags, SemVer/PEP-440/opaque ordering, prerelease/channel policy, full immutable identity extraction, incomplete checksum metadata, deterministic dependencies, cycles, incompatible graph, unsupported layout, and repeat-safe candidate creation.

```python
page = provider.discover(family, cursor=None)
result = resolver.resolve(page.candidates[0].id, family, dependencies)
assert result.lock.digest == expected_digest
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project control --frozen pytest control/tests/test_package_discovery.py control/tests/test_package_resolution.py control/tests/test_package_providers.py -v`

Expected: FAIL because discovery providers and resolver are absent.

- [ ] **Step 3: Implement metadata-only providers and resolver**

Use bounded HTTP clients with conditional requests, redirect/address/domain policy, credential references, response size limits, and durable cursors. Bind only validated provider fields through the family recipe; resolve every component/dependency to an immutable identity, compute aggregate size/compatibility, canonicalize the complete lock, and quarantine upstream mutation under a previously observed identity.

```python
@dataclass(frozen=True)
class ResolutionResult:
    state: Literal["resolved", "unsupported", "incompatible", "quarantined"]
    lock: PackageReleaseLock | None
    reason_code: str | None
    detail: Mapping[str, object]
```

- [ ] **Step 4: Verify all providers without payload transport**

Run: `uv run --project control --frozen pytest control/tests/test_package_discovery.py control/tests/test_package_resolution.py control/tests/test_package_providers.py control/tests/security/test_untrusted_repository.py -q`

Expected: PASS and tests assert no provider response body matching a declared payload is fetched during resolution.

- [ ] **Step 5: Commit W12**

```bash
git add control/src/dgx_control/package_discovery.py control/src/dgx_control/package_resolution.py control/src/dgx_control/package_providers.py control/src/dgx_control/worker.py control/tests/test_package_discovery.py control/tests/test_package_resolution.py control/tests/test_package_providers.py
git commit -m "feat: discover and resolve workload packages"
```

### Task W13: Validation, compatibility, and promotion controller

**Files:**
- Create: `control/src/dgx_control/package_validation.py`
- Create: `control/src/dgx_control/package_compatibility.py`
- Create: `control/src/dgx_control/package_publication.py`
- Modify: `control/src/dgx_control/proposals.py`
- Modify: `control/src/dgx_control/jobs.py`
- Test: `control/tests/test_package_publication.py`
- Test: `control/tests/test_package_validation.py`
- Test: `control/tests/test_package_compatibility.py`

**Interfaces:**
- Produces `CompatibilityEvaluator.evaluate(lock, fleet) -> CompatibilityReport`, `ValidationController.plan(candidate_id) -> ValidationPlan`, `advance(run_id) -> ValidationState`, `PackagePublicationService.preview(candidate_id, commit) -> PublicationPreview`, and `promote(preview_digest, actor) -> TrustedWorkloadTarget`.
- Validation may schedule package prepare/verify on an explicit disposable or canary Spark but cannot activate fleet desired state.

- [ ] **Step 1: Write RED policy and validation tests**

Cover architecture/OS/driver/CUDA/storage/backend/ABI constraints, no compatible
nodes, missing signature/provenance/license acceptance, validation fixture
failure, retryable upstream outage, canary loss, stale Git base, changed
candidate bytes, builder identity attempting TUF publication, manual-policy
automation attempt, automation failure budget, idempotent repeated promotion,
rollback selection, and manual approval remaining required by default.

```python
report = evaluator.evaluate(lock, fleet_snapshot)
assert report.compatible_node_ids == ("spk_" + "1" * 32,)
assert report.required_platform_capabilities == ("package-abi-v1",)
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project control --frozen pytest control/tests/test_package_validation.py control/tests/test_package_compatibility.py control/tests/test_package_publication.py -v`

Expected: FAIL because package validation/compatibility services are absent.

- [ ] **Step 3: Implement explicit gates and promotion eligibility**

Evaluate the complete dependency graph against authenticated agent observations.
Persist validation jobs through existing fenced `Job`/`AgentOperation`
services, bind results to exact release/policy/fleet/platform capability
digests, and expose a promotion-eligible state only after all required trust,
provenance, license, compatibility, package validation, and family policy gates
pass. Bind the promotion preview to candidate/lock/Git/policy/evidence digests,
actor, and expiry; revalidate every binding, create the canonical Git proposal
or PR through `ProposalService`, and call `WorkloadTrustPublisher` only after
the desired-state commit is eligible.

```python
@dataclass(frozen=True)
class CompatibilityReport:
    release_digest: str
    compatible_node_ids: tuple[str, ...]
    incompatible: Mapping[str, tuple[str, ...]]
    required_platform_capabilities: tuple[str, ...]
    digest: str

@dataclass(frozen=True)
class PublicationPreview:
    digest: str
    candidate_id: str
    release_digest: str
    base_commit: str
    policy_digest: str
    evidence_digests: tuple[str, ...]
    expires_at: datetime
```

- [ ] **Step 4: Verify validation and publication integration**

Run: `uv run --project control --frozen pytest control/tests/test_package_validation.py control/tests/test_package_compatibility.py control/tests/test_package_publication.py control/tests/test_agent_jobs.py -q`

Expected: PASS; promotion rejects stale or incomplete validation evidence.

- [ ] **Step 5: Commit W13**

```bash
git add control/src/dgx_control/package_validation.py control/src/dgx_control/package_compatibility.py control/src/dgx_control/package_publication.py control/src/dgx_control/proposals.py control/src/dgx_control/jobs.py control/tests/test_package_validation.py control/tests/test_package_compatibility.py control/tests/test_package_publication.py
git commit -m "feat: validate workload package candidates"
```

### Task W14: Package-aware desired-state reconciliation and rollout

**Files:**
- Modify: `control/src/dgx_control/desired_state.py`
- Modify: `control/src/dgx_control/agent_reconciliation.py`
- Create: `control/src/dgx_control/package_rollouts.py`
- Modify: `control/src/dgx_control/orchestration.py`
- Test: `control/tests/test_package_desired_state.py`
- Test: `control/tests/test_package_rollouts.py`
- Modify: `control/tests/test_agent_reconciliation.py`

**Interfaces:**
- Produces `PackageDesiredStateResolver.resolve(commit, deployment_ids, observations) -> ReconciliationPlan` and `PackageRolloutOrchestrator.advance(rollout_id)`.
- Operation graphs use `package.prepare`, `package.activate`, `package.health`, `package.stop`, and compensation `package.rollback`; they contain no `_SUPPORTED_ADAPTERS` check or fixed five-action release graph.

- [ ] **Step 1: Write RED dynamic-plan and rollout tests**

Start from an installed agent build, add a family/release/deployment name unknown to that build, and assert deterministic one/two/sixteen-node plans. Cover exact TUF authorization, current/candidate/previous generations, canary then stable batches, topology/distributed availability, route withdrawal, prepare failure preserving active, activation failure rollback, offline pending nodes, cancellation, stale commit/plan digest, and no SSH handler calls.

```python
plan = resolver.resolve(commit, ("future-stack",), observations)
assert {node.kind for node in plan.operation_graph.nodes} >= {"package.prepare", "package.activate", "package.health"}
assert "agent.update" not in {node.kind for node in plan.operation_graph.nodes}
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project control --frozen pytest control/tests/test_package_desired_state.py control/tests/test_package_rollouts.py control/tests/test_agent_reconciliation.py -v`

Expected: FAIL because desired state still requires `spark-runtime-v1` and fixed release/workload requests.

- [ ] **Step 3: Implement digest-driven package graphs**

Read package family/release/deployment documents only from the eligible commit, verify the lock through workload TUF, plan placement from complete graph compatibility/resources, and persist canonical graph/payload/plan bytes. Prepare and verify all selected nodes (and all gang ranks) before any route withdrawal or stop; activation then uses an explicit group barrier, drain/withdrawal, and node leases. Accept only exact fenced agent evidence, switch routes after fleet health, and compensate to recorded previous generation on failure. A scheduler must not reorder a stop ahead of preparation merely because operation IDs sort lexically.

```python
def package_operation_payload(deployment: WorkloadDeployment, operation: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment_id": deployment.id,
        "release_digest": deployment.release_digest,
        "deployment_digest": deployment.digest,
    }
```

- [ ] **Step 4: Verify reconciliation, persistence, and no-SSH boundaries**

Run: `uv run --project control --frozen pytest control/tests/test_package_desired_state.py control/tests/test_package_rollouts.py control/tests/test_agent_reconciliation.py control/tests/test_agent_reconciliation_postgres.py control/tests/security/test_no_routine_ssh.py -q`

Expected: PASS; legacy fixed-plan behavior is available only through an explicit migration reader.

- [ ] **Step 5: Commit W14**

```bash
git add control/src/dgx_control/desired_state.py control/src/dgx_control/agent_reconciliation.py control/src/dgx_control/package_rollouts.py control/src/dgx_control/orchestration.py control/tests/test_package_desired_state.py control/tests/test_package_rollouts.py control/tests/test_agent_reconciliation.py
git commit -m "feat: reconcile generic workload packages"
```

### Task W15: Package API and CLI equivalence

**Files:**
- Modify: `control/src/dgx_control/api.py`
- Create: `control/src/dgx_control/package_api.py`
- Modify: `src/spark_profiles/control_client.py`
- Modify: `src/spark_profiles/cli.py`
- Modify: `scripts/generate-control-clients`
- Test: `control/tests/test_package_api.py`
- Test: `tests/spark_profiles/test_package_cli.py`
- Modify: `tests/control/test_openapi_clients.py`

**Interfaces:**
- API resources cover families, candidates, resolutions, validation, promotion preview/apply, deployments, rollouts, rollback, repair, GC preview/apply, per-Spark package inventory/removal, and bounded progress.  Deployment projections expose the signed resource envelope and typed topology (`single`, `replicated`, or `gang`, including placement group, role/rank, world size, and fabric requirements).
- CLI surface is `sparkctl admin packages ...` and `sparkctl admin deployments ...`; every apply command consumes an exact preview/plan digest.

- [ ] **Step 1: Write RED authorization, idempotency, and equivalence tests**

Cover viewer/operator/admin roles, manual promotion admin-only, operator rollout of already approved state, stale preview/commit, duplicate request IDs, bounded pagination/errors, redaction, API/CLI canonical digest equality, and absence of payload proxy/upload endpoints.  Include resource-envelope admission (resident/auxiliary/activation/workspace/KV peak, staging/storage headroom, declared-vs-measured evidence), topology validation (gang rank/world-size/fabric and barrier fencing), and remove-vs-deactivate semantics.  A remove preview must show affected Sparks, active/leased blockers, shared-object reference counts, and reclaimable bytes; applying it cannot delete bytes still reachable from another deployment.

```python
preview = client.preview_package_promotion(candidate_id)
result = client.promote_package(candidate_id, preview.digest)
assert result.release_digest == preview.release_digest
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project control --frozen pytest control/tests/test_package_api.py -v && uv run --frozen pytest tests/spark_profiles/test_package_cli.py tests/control/test_openapi_clients.py -v`

Expected: FAIL because package routes, commands, and generated models are absent.

- [ ] **Step 3: Implement thin typed adapters and regenerate clients**

Mount `package_api` under `/api/v1`, use existing auth/audit/request-ID/job services, return typed candidate/compatibility/plan/progress/failure documents, and add matching CLI JSON/table views. Regenerate OpenAPI, Python client, and TypeScript declarations with `scripts/generate-control-clients`; never hand-edit generated files.

```python
@router.post("/packages/candidates/{candidate_id}/promote", response_model=PackagePromotionResponse)
def promote_package(candidate_id: str, request: PackagePromotionRequest, principal: AdminPrincipal) -> PackagePromotionResponse: ...
```

- [ ] **Step 4: Verify generated drift and CLI/API parity**

Run: `uv run --project control --frozen pytest control/tests/test_package_api.py -q && uv run --frozen pytest tests/spark_profiles/test_package_cli.py tests/control/test_openapi_clients.py -q && git add control/openapi.json src/spark_profiles/generated_control control/web/src/api/generated.d.ts && scripts/generate-control-clients && git diff --exit-code -- control/openapi.json src/spark_profiles/generated_control control/web/src/api/generated.d.ts`

Expected: PASS after committing the freshly generated artifacts; a second generator run produces no diff.

- [ ] **Step 5: Commit W15**

```bash
git add control/src/dgx_control/api.py control/src/dgx_control/package_api.py src/spark_profiles/control_client.py src/spark_profiles/cli.py scripts/generate-control-clients control/openapi.json src/spark_profiles/generated_control control/web/src/api/generated.d.ts control/tests/test_package_api.py tests/spark_profiles/test_package_cli.py tests/control/test_openapi_clients.py
git commit -m "feat: administer workload packages by API and CLI"
```

### Task W16: Web administration, progress, and observability

**Files:**
- Create: `control/web/src/pages/packages.tsx`
- Create: `control/web/src/pages/package-candidate.tsx`
- Create: `control/web/src/pages/deployments.tsx`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/api/client.ts`
- Modify: `control/src/dgx_control/dashboard.py`
- Modify: `control/src/dgx_control/metrics.py`
- Modify: `deploy/compose/grafana/dashboards/fleet.json`
- Test: `control/web/src/pages/packages.test.tsx`
- Test: `control/web/src/pages/deployments.test.tsx`
- Test: `control/tests/test_package_metrics.py`

**Interfaces:**
- Web shows family/channel, upstream candidate, structured unsupported reason, immutable lock/components/dependencies/provenance, compatibility, signed resource envelope, topology and co-residency fit, validation, promotion diff, rollout/canary/progress, retained rollback generation, and repair/GC previews.
- Web exposes a per-Spark package inventory: downloaded/verified/staged/active/retained/deletable state, free/used/reserved storage and memory, installation headroom, leases, and a safe remove/deactivate flow.  Removing a deployment is distinct from deleting shared cache objects; cache deletion remains an explicit GC preview/apply operation.
- Metrics use bounded labels only: state/reason/backend/provider/phase, never family/model/component/digest/source URL or credential.

- [ ] **Step 1: Write RED UI and cardinality tests**

Test manual promotion confirmation with exact digest, unsupported candidate visibility, aggregate download/storage plan, resource-envelope and co-residency fit, gang rank/barrier status, canary failure stop, rollback selection, offline pending node, safe progress rendering, per-Spark inventory and remove confirmation, role restrictions, secret/source redaction, keyboard/accessibility checks, and metric label bounds.

```tsx
expect(screen.getByText("Awaiting administrator approval")).toBeVisible()
expect(screen.getByText(/8\.0 GiB remaining/)).toBeVisible()
```

- [ ] **Step 2: Run the RED tests**

Run: `npm --prefix control/web test -- --run src/pages/packages.test.tsx src/pages/deployments.test.tsx && uv run --project control --frozen pytest control/tests/test_package_metrics.py -v`

Expected: FAIL because package pages/projections/metrics are absent.

- [ ] **Step 3: Implement the web workflow and bounded projections**

Use only generated TypeScript API types and the shared client. Require preview digest confirmation for promote/rollout/rollback/repair/GC, poll bounded job progress, show exact affected Sparks and previous generation, and link audit/job evidence. Add fleet summary counts and alerts for stuck acquisition, trust failure, canary failure, capacity rejection, and rollback failure.

```tsx
<PackagePromotionDialog candidate={candidate} preview={preview} onConfirm={(digest) => api.promote(candidate.id, digest)} />
```

- [ ] **Step 4: Verify web build and observability**

Run: `npm --prefix control/web test -- --run && npm --prefix control/web run build && uv run --project control --frozen pytest control/tests/test_package_metrics.py control/tests/test_dashboard.py -q && uv run pytest deploy/compose/tests/test_observability.py -q`

Expected: PASS with no unbounded or secret-bearing metrics/log/UI fields.

- [ ] **Step 5: Commit W16**

```bash
git add control/web/src/pages/packages.tsx control/web/src/pages/package-candidate.tsx control/web/src/pages/deployments.tsx control/web/src/app.tsx control/web/src/api/client.ts control/src/dgx_control/dashboard.py control/src/dgx_control/metrics.py deploy/compose/grafana/dashboards/fleet.json control/web/src/pages/packages.test.tsx control/web/src/pages/deployments.test.tsx control/tests/test_package_metrics.py
git commit -m "feat: operate workload packages from the web"
```
