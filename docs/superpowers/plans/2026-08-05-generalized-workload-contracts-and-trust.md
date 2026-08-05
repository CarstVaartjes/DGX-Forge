# Generalized Workload Contracts and Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define the immutable, application-neutral package contracts and a workload-only Git/TUF publication boundary that can authorize new workload families and releases without a DGX-Forge platform release.

**Architecture:** Git remains authoring authority for package families, promoted release locks, deployments, and fleet intent. `dgx_agent_protocol` owns only the bounded release-lock wire contract shared by control and agent; `spark_profiles` owns repository authoring contracts; workload TUF roles authorize definition/lock bytes independently from platform TUF roles. Builders may publish digest-pinned OCI artifacts and provenance, but only the NAS promotion service may authorize an exact workload release lock.

**Tech Stack:** Python 3.12, attrs/dataclasses, JSON Schema Draft 2020-12, `python-tuf` 7.0.0, securesystemslib, OCI descriptors, pytest

## Global Constraints

- Package, model, runtime, and adapter names are data and never members of a compiled DGX-Forge catalog.
- Canonical release identity is the SHA-256 digest of deterministic complete lock bytes; display versions and source tags are never installation identities.
- A release lock contains only immutable identities and exact dependency release digests; dependency graphs are acyclic, shallow, and bounded.
- Workload trust keys cannot authorize platform, agent, supervisor, node-policy, protocol, or privileged-helper updates.
- The NAS stores and serves bounded definitions, locks, desired state, and TUF metadata, not multi-gigabyte workload payloads.
- CI/build identities never receive a long-lived online workload TUF signing key.
- Unknown fields, floating tags, abbreviated commits, mutable aliases, unbounded templates, commands, shell, host paths, and embedded secrets fail closed.

---

### Task W1: Shared immutable release-lock contract

**Files:**
- Create: `agent_protocol/src/dgx_agent_protocol/workload_packages.py`
- Create: `agent_protocol/src/dgx_agent_protocol/schemas/workload-release-lock.schema.json`
- Modify: `agent_protocol/src/dgx_agent_protocol/__init__.py`
- Test: `agent_protocol/tests/test_workload_packages.py`

**Interfaces:**
- Produces `ComponentDescriptor.parse(value) -> ComponentDescriptor`, `PackageReleaseLock.parse(value) -> PackageReleaseLock`, `PackageReleaseLock.canonical_bytes`, `PackageReleaseLock.digest`, and `PackageReleaseGraph.resolve(root_digest, releases) -> PackageReleaseGraph`.
- `ComponentDescriptor` fields are exactly `name`, `kind`, `media_type`, `sources`, `digest`, `size`, `unpacked_size`, `platforms`, `materialization`, and `evidence`.
- `PackageReleaseLock` fields are exactly `schema_version`, `family_id`, `upstream_version`, `upstream_identity`, `components`, `dependency_digests`, `adapter`, `adapter_abi`, `compatibility`, `validation`, `provenance`, and `resolver`.

- [ ] **Step 1: Write strict RED contract tests**

Add table-driven tests for canonical digest stability, duplicate JSON keys, reordered maps, missing sizes, floating OCI tags, abbreviated Git commits, mutable Hugging Face revisions, dependency cycles, graph depth/component limits, digest mismatch, unknown fields, path/command/secret-shaped fields, and a valid anonymous synthetic family.

```python
lock = PackageReleaseLock.parse(document)
graph = PackageReleaseGraph.resolve(lock.digest, releases)
assert hashlib.sha256(lock.canonical_bytes).hexdigest() == lock.digest
assert tuple(item.family_id for item in graph.releases) == ("synthetic-root", "shared-model")
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project agent_protocol --frozen pytest agent_protocol/tests/test_workload_packages.py -v`

Expected: FAIL because `dgx_agent_protocol.workload_packages` and its packaged schema do not exist.

- [ ] **Step 3: Implement deterministic typed parsing**

Use `json.loads(..., object_pairs_hook=...)` for duplicate rejection, `canonical_message()` for deterministic bytes, full SHA-256/OCI digest patterns, full provider-specific immutable identities, positive bounded sizes, a maximum dependency depth of 8, and a maximum aggregate component count of 256. Freeze all maps/sequences after validation and compute the lock digest from the complete canonical document excluding no fields.

```python
@dataclass(frozen=True)
class PackageReleaseLock:
    schema_version: int
    family_id: str
    upstream_version: str
    upstream_identity: Mapping[str, object]
    components: tuple[ComponentDescriptor, ...]
    dependency_digests: tuple[str, ...]
    adapter: ComponentDescriptor
    adapter_abi: int
    compatibility: Mapping[str, object]
    validation: tuple[Mapping[str, object], ...]
    provenance: tuple[Mapping[str, object], ...]
    resolver: Mapping[str, object]
```

- [ ] **Step 4: Verify the shared contract**

Run: `uv run --project agent_protocol --frozen pytest agent_protocol/tests/test_workload_packages.py agent_protocol/tests/test_contracts.py -q`

Expected: PASS with the schema included in the built wheel.

- [ ] **Step 5: Commit W1**

```bash
git add agent_protocol/src/dgx_agent_protocol agent_protocol/tests/test_workload_packages.py
git commit -m "feat: define immutable workload release locks"
```

### Task W2: Git-backed family, deployment, and promotion contracts

**Files:**
- Create: `schemas/package-family.schema.json`
- Create: `schemas/workload-deployment.schema.json`
- Create: `src/spark_profiles/workload_packages/__init__.py`
- Create: `src/spark_profiles/workload_packages/contracts.py`
- Create: `src/spark_profiles/schemas/package-family.schema.json`
- Create: `src/spark_profiles/schemas/workload-deployment.schema.json`
- Modify: `pyproject.toml`
- Test: `tests/spark_profiles/test_workload_package_contracts.py`
- Test: `tests/spark_profiles/fleet/test_schemas.py`

**Interfaces:**
- Produces `PackageFamily.load(document) -> PackageFamily`, `WorkloadDeployment.load(document) -> WorkloadDeployment`, and `PromotionPolicy.load(document) -> PromotionPolicy`.
- Repository homes are `config/package-families/{family_id}.toml`, `config/workload-deployments/{deployment_id}.toml`, and `manifests/workload-releases/{family_id}/{release_digest}.json`; braces denote validated identifier/digest substitutions, not literal path components.
- A deployment selects exactly one `release_digest`; site placement, secrets, ports, arguments, and routing exist only in the deployment.

- [ ] **Step 1: Write RED schema and authority tests**

Cover manual promotion default, optional policy automation identity/failure budget/canary, bounded discovery recipe, SemVer/PEP-440/opaque version schemes, supported provider names, compatibility constraints, credential references without values, exact release selection, selectors for one/two/sixteen nodes, and rejection of payload URLs or secrets in deployments.

```python
family = PackageFamily.load(tomllib.loads(family_toml))
deployment = WorkloadDeployment.load(tomllib.loads(deployment_toml))
assert family.promotion.mode == "manual"
assert deployment.release_digest == "a" * 64
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --frozen pytest tests/spark_profiles/test_workload_package_contracts.py tests/spark_profiles/fleet/test_schemas.py -v`

Expected: FAIL because the schemas and typed loaders are absent.

- [ ] **Step 3: Implement and package the repository contracts**

Copy both schemas into `src/spark_profiles/schemas/` through Hatch's existing force-include convention. Implement exact-field immutable loaders, bounded recipes with typed field bindings rather than templated commands, canonical TOML/JSON serialization inputs, and cross-reference validation against a supplied mapping of lightweight release index entries. The root administration package does not import the agent protocol wheel; control joins the authoring and release-lock types after W5.

```python
def validate_deployment(
    deployment: WorkloadDeployment,
    releases: Mapping[str, ReleaseIndexEntry],
) -> ReleaseIndexEntry:
    release = releases[deployment.release_digest]
    if release.family_id != deployment.family_id:
        raise WorkloadPackageError("deployment release family does not match")
    return release
```

- [ ] **Step 4: Verify schema copies and one/two/sixteen-node fixtures**

Run: `uv run --frozen pytest tests/spark_profiles/test_workload_package_contracts.py tests/spark_profiles/fleet/test_schemas.py tests/control/test_fleet_scale.py -q`

Expected: PASS without any fixed package, node, or administrator name.

- [ ] **Step 5: Commit W2**

```bash
git add schemas/package-family.schema.json schemas/workload-deployment.schema.json src/spark_profiles/workload_packages src/spark_profiles/schemas pyproject.toml tests/spark_profiles/test_workload_package_contracts.py tests/spark_profiles/fleet/test_schemas.py
git commit -m "feat: define repository workload package state"
```

### Task W3: Separate workload TUF roles and bounded delivery

**Files:**
- Create: `control/src/dgx_control/workload_trust.py`
- Modify: `control/src/dgx_control/agent_api.py`
- Modify: `control/src/dgx_control/settings.py`
- Modify: `deploy/compose/compose.yaml`
- Test: `control/tests/test_workload_trust.py`
- Test: `control/tests/security/test_boundaries.py`

**Interfaces:**
- Produces `WorkloadTrustPublisher.publish(lock_bytes, git_commit, evidence) -> TrustedWorkloadTarget` plus the bounded agent-facing metadata/target delivery routes. Agent-side consumption is packaged with protocol v2 in W5.
- Workload roles are rooted below a workload-specific trust root and use delegated targets paths `families/*` and `releases/*`; platform target paths are structurally unreachable.
- Agent endpoints are read-only `GET /agent/v1/workload-tuf/metadata/{role}` and `GET /agent/v1/workload-tuf/targets/{digest}` behind the existing node mTLS boundary.

- [ ] **Step 1: Write RED separation and replay tests**

Prove root rotation, expiry, rollback/freeze/mix-and-match protection, exact target length/hash, bounded response size, active-node authorization, and that a workload key cannot sign any `platform/`, `agent/`, `supervisor/`, `protocol/`, or `node-policy/` target.

```python
with pytest.raises(WorkloadTrustError, match="outside workload delegation"):
    publisher.publish_as("agent/slots/arm64", artifact, commit, evidence)
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --project control --frozen pytest control/tests/test_workload_trust.py control/tests/security/test_boundaries.py -v`

Expected: FAIL because workload-specific trust and delivery routes are absent.

- [ ] **Step 3: Implement trust separation and atomic publication**

Reuse the maintained TUF metadata/signing primitives without sharing platform
root state or target prefixes. Require an eligible merged Git commit, canonical
release-lock digest, verified referenced evidence, and an
administrator/automation identity allowed by the family policy before
atomically publishing targets then snapshot/timestamp metadata. Mount workload
online signing material as a separate read-only Compose secret in the merged
pull-only NAS deployment; never expose it to image builders or add a Compose
`build:` path.

```python
@dataclass(frozen=True)
class TrustedWorkloadTarget:
    digest: str
    length: int
    git_commit: str
    tuf_snapshot_version: int
```

- [ ] **Step 4: Verify workload/platform trust non-interchangeability**

Run: `uv run --project control --frozen pytest control/tests/test_workload_trust.py control/tests/security/test_boundaries.py -q && uv run pytest deploy/compose/tests/test_networking.py tests/runbooks/test_nas_compose.py -q`

Expected: PASS; platform fixtures fail under workload trust and workload fixtures fail under platform trust.

- [ ] **Step 5: Commit W3**

```bash
git add control/src/dgx_control/workload_trust.py control/src/dgx_control/agent_api.py control/src/dgx_control/settings.py deploy/compose/compose.yaml control/tests/test_workload_trust.py control/tests/security/test_boundaries.py
git commit -m "feat: isolate workload release trust"
```

### Task W4: Workload artifact build handoff and supply-chain evidence

**Files:**
- Create: `schemas/workload-artifact-build.schema.json`
- Create: `.github/workflows/workload-artifacts.yml`
- Create: `scripts/workload-artifact-metadata`
- Create: `tests/scripts/test_workload_artifact_metadata.py`
- Create: `tests/test_workload_artifact_workflow.py`
- Modify: `docs/runbooks/supply-chain.md`
- Modify: `scripts/verify-supply-chain`
- Test: `tests/scripts/test_verify_supply_chain.py`

**Interfaces:**
- Produces `WorkloadArtifactBuild.parse(document) -> WorkloadArtifactBuild` and a digest/provenance/SBOM `WorkloadArtifactResult` that W12 discovery can resolve and W13 promotion can verify.
- The separate workload-artifact workflow consumes a validated build request from an eligible Git commit and publishes only digest-pinned OCI artifacts, SBOMs, and provenance. It extends but does not modify the incoming fixed API/worker/Hermes image release workflow.
- The build job has no workload TUF credential and cannot promote desired state; W13 verifies its output before the NAS signs a lock.

- [ ] **Step 1: Write RED build-boundary tests**

Test unsafe/unbounded build requests, missing exact source commit/context
digest, path escape, mutable base image, PR or branch publication, credentials
in build inputs, missing SBOM/provenance, publication without prior read-only CI
gates, and any workflow reference to workload TUF credentials.

```python
request = WorkloadArtifactBuild.parse(document)
assert request.source_commit == "0123456789abcdef0123456789abcdef01234567"
assert request.output_repository == "ghcr.io/carstvaartjes/dgx-forge-workloads"
```

- [ ] **Step 2: Run the RED tests**

Run: `uv run --frozen pytest tests/scripts/test_workload_artifact_metadata.py tests/test_workload_artifact_workflow.py tests/scripts/test_verify_supply_chain.py -v`

Expected: FAIL because there is no workload artifact schema, metadata command, workflow, or supply-chain gate.

- [ ] **Step 3: Implement build-only publication**

Define a bounded build request containing exact Git commit, context and
Dockerfile below reviewed workload-source roots, target, architecture, output
repository, expected source digest, and required SBOM/provenance policy. The
workflow runs only for eligible merged/tagged requests, uses a job-scoped
registry token without passing credentials into BuildKit, publishes by digest,
and emits a signed provenance/SBOM result for discovery. It never receives a
workload TUF key and never changes NAS desired state. Keep its job names,
permissions, packages, and release metadata distinct from the incoming fixed
API/worker/Hermes workflow.

```python
@dataclass(frozen=True)
class WorkloadArtifactResult:
    build_request_digest: str
    source_commit: str
    oci_manifest_digest: str
    sbom_digest: str
    provenance_digest: str
```

- [ ] **Step 4: Verify workflow permissions, evidence, and docs**

Run: `uv run --frozen pytest tests/scripts/test_workload_artifact_metadata.py tests/test_workload_artifact_workflow.py tests/scripts/test_verify_supply_chain.py -q && scripts/verify-supply-chain`

Expected: PASS and the runbook explicitly separates build, Git review, NAS promotion, workload TUF, and platform TUF authorities.

- [ ] **Step 5: Commit W4**

```bash
git add schemas/workload-artifact-build.schema.json .github/workflows/workload-artifacts.yml scripts/workload-artifact-metadata tests/scripts/test_workload_artifact_metadata.py tests/test_workload_artifact_workflow.py docs/runbooks/supply-chain.md scripts/verify-supply-chain tests/scripts/test_verify_supply_chain.py
git commit -m "feat: publish workload artifacts for promotion"
```
