# N-Node Controller Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize inventory, health, placement, backend targeting, profiles, and deployment from fixed `spark1`/`spark2` assumptions to configured node IDs without changing accepted two-Spark runtime behavior.

**Architecture:** A `FleetProvider` supplies ordered generic nodes to controller services. Versioned profile requirements are resolved by a deterministic planner into concrete placements; compatibility adapters project legacy profiles into the generic contract. This phase begins only after the in-flight SSH transport and runtime-release changes land and are merged into this worktree.

**Tech Stack:** Python 3.12, JSON Schema, TOML, concurrent futures, pytest.

## Global Constraints

- First merge/rebase the completed roadmap transport work and run its tests; do not recreate or overwrite it.
- Node iteration comes from `Fleet`, never tuple literals or schema property names.
- Preserve current two-Spark profile behavior and evidence hashes through compatibility readers.
- Distributed placement requires explicit accepted topology and model-definition support.
- Concrete placement is deterministic for the same commit, fleet observation, and policy.
- Existing external command safety, fail-to-stopped behavior, and output bounds remain intact.

---

### Task 1: Integrate and freeze the shared SSH transport boundary

**Files:**
- Modify only after upstream lands: `src/spark_profiles/ssh_transport.py`
- Modify: `src/spark_profiles/install/remote.py`
- Test: `tests/spark_profiles/install/test_remote.py`
- Test: `tests/spark_profiles/test_ssh_transport.py`

**Interfaces:**
- Consumes: landed `select_transport_binary(kind: str) -> str` or its final reviewed signature.
- Produces: one shared selection path for controller, release deployer, fabric validator, and onboarding.

- [ ] **Step 1: Bring the roadmap commit into the worktree and inspect its final API**

Run: `git log --all -- src/spark_profiles/ssh_transport.py && git status --short`
Expected: the roadmap implementation is committed and the worktree is clean before integration.

- [ ] **Step 2: Run the landed transport regression tests before editing**

Run: `uv run pytest tests/spark_profiles/test_ssh_transport.py tests/spark_profiles/test_backend.py tests/scripts/test_deploy_runtime_release.py tests/scripts/test_validate_fabric.py -v`
Expected: PASS. Stop and resolve upstream failures before proceeding.

- [ ] **Step 3: Write a failing onboarding integration test**

```python
def test_install_transport_uses_shared_binary_selector(monkeypatch, fake_exec, endpoint):
    monkeypatch.setenv("SPARK_SSH_BIN", "/opt/shared/ssh")
    OpenSshInstallTransport(exec=fake_exec).run(endpoint, ("true",), b"", 10)
    assert fake_exec.calls[0][0] == "/opt/shared/ssh"
```

- [ ] **Step 4: Wire onboarding to the landed selector and run all transport tests**

Run: `uv run pytest tests/spark_profiles/install/test_remote.py tests/spark_profiles/test_ssh_transport.py tests/spark_profiles/test_backend.py tests/scripts/test_deploy_runtime_release.py tests/scripts/test_validate_fabric.py -v`
Expected: PASS.

- [ ] **Step 5: Commit only the integration seam**

```bash
git add src/spark_profiles/install/remote.py tests/spark_profiles/install/test_remote.py
git commit -m "refactor: share SSH transport selection with onboarding"
```

### Task 2: Generalize backend aliases to fleet endpoints

**Files:**
- Modify: `src/spark_profiles/backend.py`
- Test: `tests/spark_profiles/test_backend.py`

**Interfaces:**
- Produces: `SshBackend.from_fleet(fleet: Fleet, ...) -> SshBackend`.
- `run(node: NodeId | str, argv, timeout)` accepts canonical IDs; legacy names work only through an explicit compatibility alias map.

- [ ] **Step 1: Write failing 16-node and unknown-node tests**

```python
def test_backend_targets_every_configured_node(fleet_16, fake_exec):
    backend = SshBackend.from_fleet(fleet_16, exec=fake_exec)
    for node_id in fleet_16.nodes:
        backend.run(node_id, ("true",), 10)
    assert len(fake_exec.calls) == 16


def test_backend_rejects_unknown_node_without_default_alias(fleet_16):
    with pytest.raises(ValueError, match="unknown node"):
        SshBackend.from_fleet(fleet_16).run("spark1", ("true",), 10)
```

- [ ] **Step 2: Run and verify current two-alias default fails**

Run: `uv run pytest tests/spark_profiles/test_backend.py -v`
Expected: new tests FAIL because `_DEFAULT_ALIASES` is fixed.

- [ ] **Step 3: Implement fleet-derived endpoint resolution**

Remove production fallback aliases, resolve host/user/port from `ManagementEndpoint`, preserve injected legacy aliases for compatibility tests, and keep strict SSH/output behavior unchanged.

- [ ] **Step 4: Run backend and transport tests**

Run: `uv run pytest tests/spark_profiles/test_backend.py tests/spark_profiles/test_ssh_transport.py -v`
Expected: PASS.

- [ ] **Step 5: Commit backend generalization**

```bash
git add src/spark_profiles/backend.py tests/spark_profiles/test_backend.py
git commit -m "feat: target fleet-defined Spark endpoints"
```

### Task 3: Generalize health collection and schemas

**Files:**
- Modify: `src/spark_profiles/health.py`
- Modify: `schemas/node-health.schema.json`
- Modify: `src/spark_profiles/schemas/node-health.schema.json`
- Test: `tests/spark_profiles/test_health.py`

**Interfaces:**
- `NodeHealthService(fleet, backend, ...)` returns health keyed by node ID.
- Aggregate fabric validation consumes topology links instead of `function100`/`function101` names.

- [ ] **Step 1: Write failing one/three/sixteen-node collection tests**

```python
@pytest.mark.parametrize("count", [1, 3, 16])
def test_health_collects_configured_nodes_only(fleet_factory, healthy_probe, count):
    fleet = fleet_factory(count)
    result = NodeHealthService(fleet, healthy_probe).collect()
    assert set(result.nodes) == {node_id.value for node_id in fleet.nodes}
```

- [ ] **Step 2: Run and observe exact-two-node failure**

Run: `uv run pytest tests/spark_profiles/test_health.py -v`
Expected: FAIL on the current `exactly spark1 and spark2` requirement.

- [ ] **Step 3: Replace fixed iteration and topology assumptions**

Submit probes for every non-retired fleet node, preserve stable output ordering, bound concurrency with `min(configured_limit, len(nodes))`, validate each topology link by its endpoints/rails, and update health schema nodes to an ID-keyed object with no maximum.

- [ ] **Step 4: Verify schemas and health regressions**

Run: `cmp schemas/node-health.schema.json src/spark_profiles/schemas/node-health.schema.json && uv run pytest tests/spark_profiles/test_health.py -v`
Expected: PASS.

- [ ] **Step 5: Commit health generalization**

```bash
git add src/spark_profiles/health.py schemas/node-health.schema.json src/spark_profiles/schemas/node-health.schema.json tests/spark_profiles/test_health.py
git commit -m "feat: collect health across generic Spark fleets"
```

### Task 4: Add requirement-based deterministic placement

**Files:**
- Create: `src/spark_profiles/placement.py`
- Create: `schemas/placement-requirements.schema.json`
- Create: `src/spark_profiles/schemas/placement-requirements.schema.json`
- Test: `tests/spark_profiles/test_placement.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `PlacementPlanner.plan(requirements, fleet, topology, observations) -> PlacementPlan`.
- Plan contains definition hash, ordered node IDs, reasons, and input digest.

- [ ] **Step 1: Write failing deterministic and rejection tests**

```python
def test_single_node_placement_is_deterministic(planner, four_ready_nodes, requirement):
    first = planner.plan(requirement, *four_ready_nodes)
    second = planner.plan(requirement, *reversed_observation_order(four_ready_nodes))
    assert first == second


def test_distributed_requirement_rejects_unaccepted_link(planner, two_nodes_no_accepted_rdma):
    with pytest.raises(PlacementError, match="accepted topology"):
        planner.plan(distributed_requirement(nodes=2), *two_nodes_no_accepted_rdma)
```

- [ ] **Step 2: Run and confirm planner is absent**

Run: `uv run pytest tests/spark_profiles/test_placement.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement requirement filtering and stable tie-breaking**

Filter lifecycle, labels, measured memory/disk, exclusivity, and topology; require definition-declared distributed support; sort eligible nodes by explicit preference then node ID; hash canonical inputs; return reasons for accepted and rejected candidates.

- [ ] **Step 4: Run placement tests and schema comparison**

Run: `cmp schemas/placement-requirements.schema.json src/spark_profiles/schemas/placement-requirements.schema.json && uv run pytest tests/spark_profiles/test_placement.py -v`
Expected: PASS.

- [ ] **Step 5: Commit planner**

```bash
git add src/spark_profiles/placement.py schemas/placement-requirements.schema.json src/spark_profiles/schemas/placement-requirements.schema.json tests/spark_profiles/test_placement.py pyproject.toml
git commit -m "feat: plan deterministic fleet placements"
```

### Task 5: Version generic workload/profile contracts with legacy adapters

**Files:**
- Modify: `src/spark_profiles/contracts.py`
- Modify: `src/spark_profiles/admission.py`
- Create: `src/spark_profiles/profile_compat.py`
- Create: `schemas/cluster-profile-v2.schema.json`
- Create: `schemas/workload-v2.schema.json`
- Mirror schemas under: `src/spark_profiles/schemas/`
- Test: `tests/spark_profiles/test_profile_compat.py`
- Modify: `pyproject.toml`

**Interfaces:**
- V2 profiles declare workloads plus placement requirements; concrete node IDs live in `PlacementPlan`, not profile property names.
- Produces: `adapt_legacy_profile(profile, fleet) -> GenericProfile`.

- [ ] **Step 1: Write failing legacy-equivalence and generic-profile tests**

```python
def test_current_dual_profile_adapts_to_same_start_stop_order(catalog, legacy_fleet):
    generic = adapt_legacy_profile(catalog.profiles["agent-full-dual"], legacy_fleet)
    assert generic.requirements[0].node_count == 2
    assert generic.lifecycle.start_order == "workers-before-entrypoint"


def test_v2_profile_has_no_spark_named_properties(v2_schema):
    encoded = json.dumps(v2_schema)
    assert '"spark1"' not in encoded and '"spark2"' not in encoded
```

- [ ] **Step 2: Run and observe absent compatibility layer**

Run: `uv run pytest tests/spark_profiles/test_profile_compat.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement V2 types, schemas, and explicit legacy adaptation**

Preserve legacy load paths, map placements to pinned generated IDs for current evidence, translate distributed lifecycle order into role-independent constraints, and make all new writes V2-only.

- [ ] **Step 4: Run catalog, contracts, admission, and compatibility tests**

Run: `uv run pytest tests/spark_profiles/test_contracts.py tests/spark_profiles/test_admission.py tests/spark_profiles/test_catalog.py tests/spark_profiles/test_profile_compat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit contract migration**

```bash
git add src/spark_profiles schemas src/spark_profiles/schemas tests/spark_profiles/test_profile_compat.py pyproject.toml
git commit -m "feat: add generic profile contracts"
```

### Task 6: Generalize controller dependencies and switching

**Files:**
- Modify: `src/spark_profiles/cli.py`
- Modify: `src/spark_profiles/switcher.py`
- Test: `tests/spark_profiles/test_cli.py`
- Test: `tests/spark_profiles/test_switcher.py`

**Interfaces:**
- `build_dependencies` loads generic fleet when present and legacy fleet otherwise.
- Switcher consumes concrete `PlacementPlan`; it never creates node tuples.

- [ ] **Step 1: Write failing N-node dry-run and one-node switch tests**

```python
def test_dry_run_reports_generated_node_ids(generic_repository, run_cli):
    result = run_cli(generic_repository, "switch", "creative", "--dry-run", "--json")
    assert result.returncode == 0
    assert all(node.startswith("spk_") for node in json.loads(result.stdout)["nodes"])


def test_switcher_touches_only_planned_nodes(three_node_switcher):
    three_node_switcher.switch("single", dry_run=False)
    assert three_node_switcher.backend.touched == [three_node_switcher.plan.nodes[0]]
```

- [ ] **Step 2: Run and observe fixed tuple failures**

Run: `uv run pytest tests/spark_profiles/test_cli.py tests/spark_profiles/test_switcher.py -v`
Expected: FAIL where code iterates `spark1`, `spark2`.

- [ ] **Step 3: Thread Fleet and PlacementPlan through controller services**

Replace fixed loops, preserve fail-to-stopped ordering from the concrete plan, include plan/input digests in reports/state, and keep legacy output fields during a documented compatibility window.

- [ ] **Step 4: Run controller and full tests**

Run: `uv run pytest tests/spark_profiles -v && uv run pytest -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit N-node controller**

```bash
git add src/spark_profiles/cli.py src/spark_profiles/switcher.py tests/spark_profiles/test_cli.py tests/spark_profiles/test_switcher.py
git commit -m "feat: control planned N-node placements"
```

### Task 7: Generalize runtime release target resolution

**Files:**
- Modify after roadmap merge: `scripts/deploy-runtime-release`
- Modify: `tests/scripts/test_deploy_runtime_release.py`
- Modify: `docs/runbooks/runtime-release.md`

**Interfaces:**
- Release deployer resolves concrete target node IDs through fleet inventory and a placement plan; legacy workload nodes remain supported through compatibility loading.

- [ ] **Step 1: Write failing generated-ID release tests**

```python
def test_release_targets_resolved_generic_nodes(generic_release_root, run_release):
    result = run_release(generic_release_root, "--json", "example")
    assert json.loads(result.stdout)["node_ids"] == generic_release_root.expected_ids
    assert all(alias not in result.stdout for alias in ("dgx-spark-1", "dgx-spark-2"))
```

- [ ] **Step 2: Run all release tests before editing**

Run: `uv run pytest tests/scripts/test_deploy_runtime_release.py -v`
Expected: existing tests PASS; new generic test FAIL.

- [ ] **Step 3: Resolve targets through generic inventory without changing immutable install safety**

Keep landed transport and remote mode/hash verification byte-for-byte where possible. Replace only workload-node validation/alias lookup with compatibility-aware fleet resolution.

- [ ] **Step 4: Run release, adapter, and full tests**

Run: `uv run pytest tests/scripts/test_deploy_runtime_release.py tests/adapters -v && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit integration**

```bash
git add scripts/deploy-runtime-release tests/scripts/test_deploy_runtime_release.py docs/runbooks/runtime-release.md
git commit -m "feat: deploy releases to generic fleet placements"
```
