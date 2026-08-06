# Spark Workload Package Engine Implementation Plan

> **Implementation status (2026-08-06): complete for the installed capability
> set.** W5–W8 and W10 are implemented on `main`; W9 provides the generic ABI,
> signed deployment-policy propagation, reviewed native/Python-venv execution,
> and a fail-closed OCI rootfs/runc capability boundary. Verification results are tracked in the [roadmap
> status ledger](2026-08-05-generalized-workload-package-roadmap.md#implementation-status-2026-08-06).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every Spark one durable, resumable, content-addressed package engine that can materialize and run arbitrary signed workload releases through stable unprivileged backends.

**Architecture:** The compiled agent exposes only generic package operations and a versioned unprivileged adapter ABI. A Spark-local SQLite journal coordinates capacity reservations, downloads, derived artifacts, generations, leases, rollback, repair, and garbage collection; immutable bytes enter the verified store only after complete digest/size checks. The privileged boundary applies fixed typed primitives, while release-provided adapters run as the workload identity in a restricted sandbox.

**Tech Stack:** Python 3.12, SQLite, Linux dirfd/openat2-style confinement, systemd/cgroups, OCI/ORAS, Hugging Face HTTP revisions, Python `pylock.toml`, pytest

## Global Constraints

- The agent compiles operation vocabulary, fetch protocols, execution backends, sandbox policy, and adapter ABI—not model IDs, family IDs, adapter IDs, releases, images, or checkpoints.
- Workload adapters are signed content-addressed components and run without host privilege, arbitrary shell, `apt`, host mutation, undeclared devices, or unrestricted host paths.
- Payloads download directly from immutable upstreams or approved mirrors; the NAS is never the routine payload proxy.
- Every download is ranged/resumable, journaled, capacity-reserved, cancellable, restart-safe, size/digest verified, and atomically promoted from a partial namespace.
- Activation changes one generation pointer only after the complete dependency graph is materialized and validated.
- Active, retained rollback, staged, leased, and explicitly pinned objects are never garbage-collected.
- Existing `release.install` and `spark-runtime-v1` behavior remains a legacy compatibility path until W17 migration acceptance passes.

---

### Task W5: Generic package operation protocol and capabilities

**Files:**
- Modify: `agent_protocol/src/dgx_agent_protocol/contracts.py`
- Modify: `agent_protocol/src/dgx_agent_protocol/schemas/agent-job.schema.json`
- Create: `agent_protocol/src/dgx_agent_protocol/schemas/agent-directive.schema.json`
- Modify: `agent_protocol/pyproject.toml`
- Modify: `agent_protocol/uv.lock`
- Delete: `inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl`
- Create: `inventory/wheels/dgx_agent_protocol-2.0.0-py3-none-any.whl`
- Create: `agent/src/dgx_agent/package_trust.py`
- Create: `agent/src/dgx_agent/package_operations.py`
- Modify: `agent/src/dgx_agent/operations.py`
- Modify: `agent/src/dgx_agent/client.py`
- Modify: `agent/src/dgx_agent/main.py`
- Modify: `agent/src/dgx_agent/state.py`
- Modify: `agent/pyproject.toml`
- Modify: `agent/uv.lock`
- Modify: `control/pyproject.toml`
- Modify: `control/uv.lock`
- Modify: `control/Dockerfile`
- Modify: `.dockerignore`
- Modify: `scripts/verify-supply-chain`
- Modify: `inventory/sbom/agent-protocol.spdx.json`
- Modify: `inventory/sbom/agent-python.spdx.json`
- Modify: `inventory/sbom/control-python.spdx.json`
- Modify: `inventory/sbom/manifest.json`
- Test: `agent_protocol/tests/test_contracts.py`
- Test: `agent/tests/test_package_trust.py`
- Test: `agent/tests/test_operations.py`
- Modify: `agent/tests/test_state.py`
- Modify: `agent/tests/test_slot_artifact.py`
- Modify: `control/tests/security/test_agent_protocol.py`
- Modify: `tests/scripts/test_verify_supply_chain.py`

**Interfaces:**
- Adds compiled operations `package.prepare`, `package.activate`, `package.health`, `package.stop`, `package.rollback`, `package.remove`, `package.repair`, and `package.gc`.
- Release-bound payloads contain only `schema_version`, `deployment_id`, `release_digest`, `deployment_digest`, and operation-specific bounded flags. `package.gc` contains only `schema_version`, `dry_run`, and optional positive `target_bytes`; filesystem paths and commands remain forbidden.
- Produces `PackageOperationRequest.parse(operation, payload) -> PackageOperationRequest`, `AgentDirective.parse(value) -> AgentDirective`, agent-side `WorkloadTrust.refresh()` / `trusted_lock(digest) -> PackageReleaseLock`, and adds all eight operation names to advertised capabilities.
- Packages the backward-readable job/result contracts plus the new directive, workload lock, and package operations as `dgx-agent-protocol==2.0.0`; control accepts protocol 1 and 2 during the Spark rollout, while package operations require protocol 2.

- [ ] **Step 1: Write RED protocol and closed-vocabulary tests**

Cover exact fields, invalid digest/deployment IDs, commands/paths/secrets,
operation-specific field mismatch, unknown operation, capability advertisement,
old agent rejection, workload/platform TUF non-interchangeability,
authenticated heartbeat cancellation, durable cancellation intent across
restart, and the fact that an unknown family name is never
validated by the operation parser.

```python
request = PackageOperationRequest.parse(
    AgentOperation.PACKAGE_PREPARE,
    {"schema_version": 1, "deployment_id": "future-stack", "release_digest": "a" * 64, "deployment_digest": "b" * 64},
)
assert request.deployment_id == "future-stack"
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project agent_protocol --frozen pytest agent_protocol/tests/test_contracts.py agent_protocol/tests/test_workload_packages.py -v && uv run --project agent --frozen pytest agent/tests/test_package_trust.py agent/tests/test_operations.py -v`

Expected: FAIL because generic package operations are not in the protocol or registry.

- [ ] **Step 3: Implement the stable package operation boundary**

Add enum values and exact schema alternatives. Parse only identifiers,
digests, booleans, and bounded byte counts into an immutable request; dispatch
through a new `PackageOperationsBoundary` protocol in `OperationContext`. Add
an `AgentDirective` heartbeat response carrying the renewed deadline and
`cancel_requested`; persist cancellation intent before exposing it to the
package engine. Implement `WorkloadTrust` with a separate bootstrap root,
metadata state, target prefix, and cache from platform TUF. Build the v2 wheel,
update exact agent/control wheel references and locks, regenerate SBOM evidence,
and make the merged public-image input verifier include the v2 wheel. Do not
route package operations through `_PRODUCTION_POLICIES` or accept an adapter ID
from the control-plane claim.

```python
class PackageOperationsBoundary(Protocol):
    def execute(self, request: PackageOperationRequest, binding: OperationBinding, deadline: MonotonicDeadline) -> Mapping[str, object]: ...
    def inspect(self, request: PackageOperationRequest, binding: OperationBinding, deadline: MonotonicDeadline) -> PackageInspection: ...
```

- [ ] **Step 4: Verify protocol compatibility**

Run: `uv run --project agent_protocol --frozen pytest agent_protocol/tests -q && uv run --project agent --frozen pytest agent/tests/test_package_trust.py agent/tests/test_operations.py agent/tests/test_client.py agent/tests/test_state.py agent/tests/test_slot_artifact.py -q && uv run --project control --frozen pytest control/tests/security/test_agent_protocol.py -q && scripts/verify-supply-chain --generate --json && scripts/verify-supply-chain --json`

Expected: PASS; legacy operations still parse and agents advertise both legacy and package-v1 capabilities during migration.

- [ ] **Step 5: Commit W5**

```bash
git add agent_protocol inventory/wheels/dgx_agent_protocol-2.0.0-py3-none-any.whl agent/src/dgx_agent/package_trust.py agent/src/dgx_agent/package_operations.py agent/src/dgx_agent/operations.py agent/src/dgx_agent/client.py agent/src/dgx_agent/main.py agent/src/dgx_agent/state.py agent/pyproject.toml agent/uv.lock control/pyproject.toml control/uv.lock control/Dockerfile .dockerignore scripts/verify-supply-chain inventory/sbom/agent-protocol.spdx.json inventory/sbom/agent-python.spdx.json inventory/sbom/control-python.spdx.json inventory/sbom/manifest.json agent/tests/test_package_trust.py agent/tests/test_operations.py agent/tests/test_state.py agent/tests/test_slot_artifact.py control/tests/security/test_agent_protocol.py tests/scripts/test_verify_supply_chain.py && git add -u inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl
git commit -m "feat: add generic package operation vocabulary"
```

### Task W6: Durable package store, reservations, and operation journal

**Files:**
- Create: `agent/src/dgx_agent/packages/__init__.py`
- Create: `agent/src/dgx_agent/packages/state.py`
- Create: `agent/src/dgx_agent/packages/store.py`
- Test: `agent/tests/packages/test_state.py`
- Test: `agent/tests/packages/test_store.py`

**Interfaces:**
- Produces `PackageState`, `ContentStore.reserve(operation, bytes) -> Reservation`, `begin_component(...) -> DownloadRecord`, `promote_component(record, verified_digest) -> StoreObject`, and generation/lease reachability queries.
- SQLite schema records operations, reservations, components, partials, derived objects, generations, generation objects, leases, and GC intents with exact operation fence ownership.

- [ ] **Step 1: Write RED crash-window and capacity tests**

Test concurrent reservation overcommit, same-digest sharing, partial inode substitution, crash before/after fsync/rename/commit, restart recovery, cancellation ownership, lease expiry, corrupt database/schema, symlink/hardlink/FIFO attacks, and active/rollback reachability.

```python
reservation = store.reserve(binding, bytes_required=8 * 1024**3)
record = store.begin_component(reservation, descriptor)
assert record.state == "partial"
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project agent --frozen pytest agent/tests/packages/test_state.py agent/tests/packages/test_store.py -v`

Expected: FAIL because the package store does not exist.

- [ ] **Step 3: Implement anchored immutable storage**

Use an agent-owned root, dirfd-relative no-follow opens, mode/owner/link-count checks, per-digest file locks, `BEGIN IMMEDIATE`, monotonic operation fences, exact inode capture, temp-file fsync, atomic rename, directory fsync, then transactional publication. Store only verified content below `objects/sha256/{digest}`, where `{digest}` is the validated lowercase SHA-256; keep partials and journals outside that namespace.

```python
@dataclass(frozen=True)
class StoreObject:
    digest: str
    size: int
    kind: str
    relative_name: str
```

- [ ] **Step 4: Verify concurrency and restart recovery**

Run: `uv run --project agent --frozen pytest agent/tests/packages/test_state.py agent/tests/packages/test_store.py -q`

Expected: PASS across repeated crash-window parametrization and two concurrent installers requesting the same digest.

- [ ] **Step 5: Commit W6**

```bash
git add agent/src/dgx_agent/packages agent/tests/packages/test_state.py agent/tests/packages/test_store.py
git commit -m "feat: add durable Spark package store"
```

### Task W7: Resumable multi-provider acquisition

**Files:**
- Create: `agent/src/dgx_agent/packages/fetch.py`
- Create: `agent/src/dgx_agent/packages/providers.py`
- Modify: `agent/src/dgx_agent/oci.py`
- Test: `agent/tests/packages/test_fetch.py`
- Test: `agent/tests/packages/test_providers.py`

**Interfaces:**
- Produces `AcquisitionEngine.fetch(descriptor, binding, progress, cancelled) -> StoreObject`.
- `FetchProvider` implementations initially cover verified HTTPS, Git snapshot archives/full commits, OCI blobs/manifests, Hugging Face full revisions, Python indexes/wheels, and signed HTTP indexes.
- Progress reports `phase`, `component`, `bytes_completed`, `bytes_total`, `objects_completed`, `objects_total`, `cache_hits`, and `reserved_bytes`.

- [ ] **Step 1: Write RED transfer and source-policy tests**

Cover Range/If-Range resume, servers ignoring Range, changed ETag, redirects, private-address rebinding, domain allowlists, expired credentials, declared/observed size limits, archive expansion limits, mirror failover for the same digest, checksum mismatch, cancellation, restart, cache hit, and credential/log redaction.

```python
result = engine.fetch(descriptor, binding, report.append, cancelled.is_set)
assert result.digest == descriptor.digest.removeprefix("sha256:")
assert report[-1]["bytes_completed"] == descriptor.size
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project agent --frozen pytest agent/tests/packages/test_fetch.py agent/tests/packages/test_providers.py -v`

Expected: FAIL because no shared acquisition engine exists.

- [ ] **Step 3: Implement bounded provider adapters over one journal**

Resolve credentials only at request time, validate every redirect/address, use partial offsets only when validator metadata still matches, hash all bytes including resumed prefixes, quarantine mismatches, and promote through `ContentStore` only after complete verification. Adapt existing `ORASClient`; do not add independent per-model download code.

```python
class FetchProvider(Protocol):
    def open(self, source: SourceLocation, offset: int, validators: Validators, deadline: MonotonicDeadline) -> FetchStream: ...
```

- [ ] **Step 4: Verify all provider and redaction cases**

Run: `uv run --project agent --frozen pytest agent/tests/packages/test_fetch.py agent/tests/packages/test_providers.py agent/tests/test_releases.py -q`

Expected: PASS; interrupted multi-gigabyte fixtures resume and no partial bytes appear in the verified namespace.

- [ ] **Step 5: Commit W7**

```bash
git add agent/src/dgx_agent/packages/fetch.py agent/src/dgx_agent/packages/providers.py agent/src/dgx_agent/oci.py agent/tests/packages/test_fetch.py agent/tests/packages/test_providers.py
git commit -m "feat: acquire workload components resumably"
```

### Task W8: Immutable materialization and Python environments

**Files:**
- Create: `agent/src/dgx_agent/packages/materialize.py`
- Create: `agent/src/dgx_agent/packages/python_env.py`
- Test: `agent/tests/packages/test_materialize.py`
- Test: `agent/tests/packages/test_python_env.py`

**Interfaces:**
- Produces `Materializer.materialize(lock, objects, staging) -> MaterializedGeneration` and `PythonEnvironmentBuilder.build(spec, objects, binding) -> StoreObject`.
- Supports typed snapshot/archive, OCI-content reference, configuration, native userspace archive, wheel, and `pylock.toml` environment materializations.

- [ ] **Step 1: Write RED reproducibility and sandbox tests**

Test path traversal, duplicate/archive bombs, device/FIFO entries, setuid bits, mutable environment repair, live-index resolution, missing wheel hashes, same-lock reuse, different interpreter/platform identity, network-disabled source wheel builds, failed import validation, cancellation, and restart.

```python
environment = builder.build(spec, objects, binding)
assert environment.digest == expected_environment_digest
assert store.is_immutable(environment)
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project agent --frozen pytest agent/tests/packages/test_materialize.py agent/tests/packages/test_python_env.py -v`

Expected: FAIL because materialization and immutable environments are absent.

- [ ] **Step 3: Implement typed, networkless materialization**

Derive environment identity from interpreter, platform, complete lock, source inputs, and build recipe. Download all inputs first, run permitted source-to-wheel builds as the unprivileged build identity with no network/devices/host mounts, record derivation evidence, validate imports/metadata, fsync, and publish as a store object. Never mutate a published environment.

```python
@dataclass(frozen=True)
class MaterializedGeneration:
    release_digest: str
    root_object_digest: str
    object_digests: tuple[str, ...]
    environment_digest: str | None
```

- [ ] **Step 4: Verify materialization and environment reuse**

Run: `uv run --project agent --frozen pytest agent/tests/packages/test_materialize.py agent/tests/packages/test_python_env.py -q`

Expected: PASS with identical locks sharing environments and failed staging leaving no published generation.

- [ ] **Step 5: Commit W8**

```bash
git add agent/src/dgx_agent/packages/materialize.py agent/src/dgx_agent/packages/python_env.py agent/tests/packages/test_materialize.py agent/tests/packages/test_python_env.py
git commit -m "feat: materialize immutable workload environments"
```

### Task W9: Unprivileged adapter ABI and execution backends

**Files:**
- Create: `agent/src/dgx_agent/packages/adapter.py`
- Create: `agent/src/dgx_agent/packages/backends.py`
- Create: `agent/src/dgx_agent/packages/sandbox.py`
- Create: `agent/src/dgx_agent/package_helper_protocol.py`
- Create: `agent/src/dgx_agent/package_helper.py`
- Create: `agent/systemd/dgx-forge-package-helper.service`
- Create: `agent/systemd/dgx-forge-package-helper.socket`
- Modify: `agent/pyproject.toml`
- Modify: `agent/src/dgx_agent/nvidia_tools.py`
- Modify: `nodes/bin/install-dgx-agent`
- Test: `agent/tests/packages/test_adapter.py`
- Test: `agent/tests/packages/test_backends.py`
- Test: `agent/tests/packages/test_sandbox.py`
- Test: `agent/tests/test_package_helper.py`
- Modify: `tests/nodes/test_install_dgx_agent.py`

**Interfaces:**
- Produces ABI v1 operations `prepare`, `verify`, `start`, `health`, `infer`, `stop`, and `verify-release` with canonical JSON stdin/stdout bound to job/operation/attempt/fence/release/generation.
- Backends are exactly `oci`, `python-venv`, and `native`. The socket-activated root-owned package helper accepts canonical validated backend structs from the `dgx-agent` peer UID, re-verifies signed receipts and operation fences, launches only the dedicated workload UID, and returns bounded evidence.

- [ ] **Step 1: Write RED privilege and dynamic-adapter tests**

Use two differently named adapters unknown to the agent build. Test signed
digest selection, sealed executable snapshot, Unix peer credential rejection,
helper request replay/stale fence, dedicated unprivileged UID/GID, no ambient
capabilities, `NoNewPrivileges`, bounded cgroup/devices/network/mounts,
immutable cwd, no arbitrary argv/env, timeout/output bounds, fence echo,
malformed results, and attempts to request `apt`, shell, host paths, kernel
modules, or undeclared GPUs.

```python
evidence = adapter.execute(AdapterOperation.HEALTH, invocation, deadline)
assert evidence.release_digest == invocation.release_digest
assert evidence.fence == invocation.fence
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project agent --frozen pytest agent/tests/packages/test_adapter.py agent/tests/packages/test_backends.py agent/tests/packages/test_sandbox.py agent/tests/test_package_helper.py -v && uv run --frozen pytest tests/nodes/test_install_dgx_agent.py -v`

Expected: FAIL because package adapters/backends do not exist and current `CompiledAdapterPolicy` is ID-specific.

- [ ] **Step 3: Implement the generic sandboxed executor**

Reuse sealed memfd and installed-receipt checks from `WorkloadOperations`, but
select the adapter from the trusted release lock rather than
`_PRODUCTION_POLICIES`. Install a narrow root-owned socket service separately
from the unprivileged agent. The helper validates Unix peer credentials,
canonical request bytes, backend enum, signed object receipts, relative entry
point, fixed mounts/resources/devices/network, workload UID, and operation
fence; it never parses model/family names and never executes a
definition-supplied shell. Python/native adapter logic and processes run as the
workload UID; OCI setup uses only the pinned runtime and the same validated
policy.

```python
@dataclass(frozen=True)
class BackendInvocation:
    backend: Literal["oci", "python-venv", "native"]
    release_digest: str
    generation: str
    entrypoint: str
    arguments: tuple[str, ...]
    resources: ResourcePolicy
```

- [ ] **Step 4: Verify dynamic adapter behavior and legacy isolation**

Run: `uv run --project agent --frozen pytest agent/tests/packages agent/tests/test_workloads.py agent/tests/test_runtime_policy.py -q`

Expected: PASS; new adapter digests execute within ABI v1 without adding an adapter ID to agent source, while the legacy compiled policy remains isolated.

- [ ] **Step 5: Commit W9**

```bash
git add agent/src/dgx_agent/packages/adapter.py agent/src/dgx_agent/packages/backends.py agent/src/dgx_agent/packages/sandbox.py agent/src/dgx_agent/package_helper_protocol.py agent/src/dgx_agent/package_helper.py agent/src/dgx_agent/nvidia_tools.py agent/systemd/dgx-forge-package-helper.service agent/systemd/dgx-forge-package-helper.socket agent/pyproject.toml nodes/bin/install-dgx-agent agent/tests/packages agent/tests/test_package_helper.py tests/nodes/test_install_dgx_agent.py
git commit -m "feat: run dynamic workload adapters safely"
```

### Task W10: Atomic generations, rollback, repair, and garbage collection

**Files:**
- Create: `agent/src/dgx_agent/packages/engine.py`
- Create: `agent/src/dgx_agent/packages/gc.py`
- Modify: `agent/src/dgx_agent/main.py`
- Modify: `agent/src/dgx_agent/operations.py`
- Test: `agent/tests/packages/test_engine.py`
- Test: `agent/tests/packages/test_gc.py`
- Test: `agent/tests/test_lifecycle.py`

**Interfaces:**
- Produces `PackageEngine.execute(request, binding, deadline) -> PackageEvidence` and `inspect(...) -> PackageInspection` wired as `OperationContext.packages`.
- Generation states are `staging`, `validated`, `active`, `retained`, `failed`, and `quarantined`; activation and rollback switch one authenticated pointer.

- [ ] **Step 1: Write RED lifecycle, cancellation, and GC tests**

Cover complete preflight/fetch/verify/materialize/validate/activate/health flow, insufficient capacity, compatibility failure, cancellation before activation, cancellation after activation becoming rollback intent, restart at every journal boundary, previous active preservation, offline rollback, running-process leases, repair quarantine/refetch, GC dry-run/interrupt/restart, and all reachability roots.

```python
prepared = engine.execute(prepare_request, binding, deadline)
activated = engine.execute(activate_request, binding.next(), deadline)
assert activated.active_generation == prepared.staged_generation
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project agent --frozen pytest agent/tests/packages/test_engine.py agent/tests/packages/test_gc.py agent/tests/test_lifecycle.py -v`

Expected: FAIL because the end-to-end engine is absent.

- [ ] **Step 3: Implement idempotent lifecycle orchestration**

Load the exact release lock through `WorkloadTrust`, preflight complete graph compatibility/capacity/credentials, reserve aggregate bytes, acquire all components, materialize and validate a staged generation, then atomically select it and health-check. Persist every transition before side effects and make `inspect` return `completed`, `safe-to-retry`, `compensate`, or `operator-intervention` from journals/receipts rather than process memory.

```python
@dataclass(frozen=True)
class PackageEvidence:
    operation: str
    deployment_id: str
    release_digest: str
    generation: str | None
    status: str
    evidence_digest: str
```

- [ ] **Step 4: Run full agent and protocol verification**

Run: `uv run --project agent --frozen pytest agent/tests -q && uv run --project agent_protocol --frozen pytest agent_protocol/tests -q && git diff --check`

Expected: PASS; an unknown synthetic package completes prepare/activate/rollback without `release.install`, SSH, or agent update.

- [ ] **Step 5: Commit W10**

```bash
git add agent/src/dgx_agent/packages/engine.py agent/src/dgx_agent/packages/gc.py agent/src/dgx_agent/main.py agent/src/dgx_agent/operations.py agent/tests/packages agent/tests/test_lifecycle.py
git commit -m "feat: reconcile atomic workload generations"
```
