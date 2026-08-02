# Model Profile Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the developer-machine workload catalog, whole-cluster profile model, and fail-to-stopped `sparkctl` switcher used by every local AI workload.

**Architecture:** Immutable workload definitions describe one model service; cluster profiles compose the complete desired state of both Sparks. A developer-machine controller validates explicit placement, reconciles node state through key-only SSH, publishes only healthy loopback endpoints, and returns to the full dual-Spark DeepSeek home profile after temporary generation jobs.

**Tech Stack:** Python 3.12, dataclasses, `tomllib`, JSON Schema, pytest, Bash, OpenSSH, Docker Compose, TOML.

**Approved design:** [Multi-runtime model profiles](../specs/2026-08-02-multi-runtime-model-profiles-design.md) and [model capacity overview](../../model-capacity-overview.md).

## Global Constraints

- Full dual-Spark DeepSeek 0731 is the `default` and `agent` home profile.
- A cluster profile specifies the complete state of Spark 1 and Spark 2.
- Dual-Spark workloads reserve both nodes; worker starts first and head stops first.
- Co-location is denied unless the exact cluster profile has passed measured acceptance.
- Every endpoint remains loopback-only and is consumed through SSH tunnels until the NAS control plane exists.
- Failed transitions end stopped or degraded and never silently select another model.
- Canonical artifacts and controller state live on the developer machine.
- Container images, source commits, checkpoints, and manifests are immutable.
- The user-facing workload always selects the best accepted DGX Spark-optimized path; generic upstream paths are non-serving correctness references.
- Every adapter exposes `prepare`, `verify`, `start`, `health`, `infer`, `stop`, and `verify-release` operations.
- No model profile auto-starts after reboot.

---

### Task 1: Define workload and cluster-profile contracts

**Files:**
- Create: `pyproject.toml`
- Create: `src/spark_profiles/__init__.py`
- Create: `src/spark_profiles/contracts.py`
- Create: `schemas/workload.schema.json`
- Create: `schemas/cluster-profile.schema.json`
- Create: `tests/spark_profiles/test_contracts.py`
- Create: `config/workloads/deepseek-agent-dual.toml`
- Create: `config/cluster-profiles/default.toml`

**Interfaces:**
- Produces: `load_workload(path: Path) -> WorkloadDefinition`.
- Produces: `load_cluster_profile(path: Path) -> ClusterProfile`.
- `ClusterProfile.placements` maps exactly `spark1` and `spark2` to tuples of workload IDs.

- [ ] **Step 1: Write failing contract tests**

```python
def test_cluster_profile_requires_both_nodes(tmp_path):
    path = write_profile(tmp_path, {"spark1": ["deepseek-agent-dual"]})
    with pytest.raises(ProfileValidationError, match="spark2"):
        load_cluster_profile(path)

def test_distributed_workload_declares_rank_order(workload_path):
    workload = load_workload(workload_path)
    assert workload.topology == "distributed"
    assert workload.start_order == ("spark2", "spark1")
    assert workload.stop_order == ("spark1", "spark2")
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run --with pytest --with jsonschema pytest tests/spark_profiles/test_contracts.py -v`

Expected: FAIL because `spark_profiles.contracts` and both schemas are absent.

- [ ] **Step 3: Implement strict typed contracts**

Use frozen dataclasses and reject unknown keys. Require workload ID, topology,
nodes, source/checkpoint/image pins, cache/scratch/output paths, loopback port,
start/stop/health commands, resource envelope, conflicts, and co-location
status. Require cluster profile ID, both node placements, allowed endpoints,
accepted-evidence path, and home-profile restoration policy.

- [ ] **Step 4: Add the initial DeepSeek home definitions**

`deepseek-agent-dual` consumes both nodes and references the already accepted
DeepSeek profile scripts. `default.toml` places that workload on both nodes
and exposes the alias `agent`; it must not duplicate model runtime commands.

- [ ] **Step 5: Run validation and commit**

Run: `uv run --with pytest --with jsonschema pytest tests/spark_profiles/test_contracts.py -v && git diff --check`

Expected: PASS.

```bash
git add pyproject.toml src schemas tests/spark_profiles config/workloads/deepseek-agent-dual.toml config/cluster-profiles/default.toml
git commit -m "feat: define Spark workload and cluster profiles"
```

### Task 2: Build catalog and admission validation

**Files:**
- Create: `src/spark_profiles/catalog.py`
- Create: `src/spark_profiles/admission.py`
- Create: `tests/spark_profiles/test_catalog.py`
- Create: `tests/spark_profiles/test_admission.py`
- Create: `inventory/reports/accepted-cluster-profiles.json`

**Interfaces:**
- Consumes: `WorkloadDefinition` and `ClusterProfile`.
- Produces: `Catalog.load(root: Path) -> Catalog`.
- Produces: `check_admission(profile, catalog, inventory) -> AdmissionReport`.

- [ ] **Step 1: Write failing catalog/admission tests**

```python
def test_unknown_workload_is_rejected(catalog, profile):
    profile = replace(profile, placements={"spark1": ("missing",), "spark2": ()})
    assert check_admission(profile, catalog, inventory()).ok is False

def test_colocation_requires_exact_acceptance(catalog, colocated_profile):
    report = check_admission(colocated_profile, catalog, inventory(), accepted={})
    assert report.errors == ("profile has no accepted co-location evidence",)

def test_distributed_workload_reserves_both_nodes(catalog, invalid_dual_profile):
    assert "distributed reservation" in check_admission(
        invalid_dual_profile, catalog, inventory()
    ).errors[0]
```

- [ ] **Step 2: Confirm tests fail**

Run: `uv run --with pytest pytest tests/spark_profiles/test_catalog.py tests/spark_profiles/test_admission.py -v`

Expected: import failure for catalog/admission modules.

- [ ] **Step 3: Implement deterministic validation**

Reject missing definitions, port collisions, cache/output overlap, conflicting
workloads, partial distributed placement, insufficient measured memory/disk,
unaccepted co-location, and publication of an unhealthy/unaccepted endpoint.
Return all errors in stable sorted order without mutating state.

- [ ] **Step 4: Add signed-off evidence indexing**

Index acceptance by cluster-profile content SHA-256 plus workload definition
hashes. A changed resource envelope, runtime pin, placement, or command
invalidates prior acceptance.

- [ ] **Step 5: Run tests and commit**

Run: `uv run --with pytest pytest tests/spark_profiles/test_catalog.py tests/spark_profiles/test_admission.py -v && git diff --check`

```bash
git add src/spark_profiles tests/spark_profiles inventory/reports/accepted-cluster-profiles.json
git commit -m "feat: validate model profile admission"
```

### Task 3: Implement SSH node backend and persisted state

**Files:**
- Create: `src/spark_profiles/backend.py`
- Create: `src/spark_profiles/state.py`
- Create: `tests/spark_profiles/test_backend.py`
- Create: `tests/spark_profiles/test_state.py`
- Create: `config/controller.toml`

**Interfaces:**
- Produces: `SshBackend.run(node, argv, timeout) -> CommandResult`.
- Produces: `StateStore.acquire() -> ContextManager[ControllerState]`.
- Stores state under `.state/sparkctl/state.json` and locks
  `.state/sparkctl/switch.lock` on the developer machine.

- [ ] **Step 1: Write failing backend and stale-lock tests**

```python
def test_backend_never_uses_shell(fake_exec):
    SshBackend(fake_exec).run("spark1", ("profile-status", "--json"), 10)
    assert fake_exec.argv[:4] == ("ssh", "-o", "BatchMode=yes", "dgx-spark-1")
    assert fake_exec.shell is False

def test_state_write_is_atomic(store, interrupted_replace):
    with pytest.raises(OSError):
        store.save({"active_profile": "broken"})
    assert store.load().active_profile == "default"
```

- [ ] **Step 2: Confirm tests fail**

Run: `uv run --with pytest pytest tests/spark_profiles/test_backend.py tests/spark_profiles/test_state.py -v`

- [ ] **Step 3: Implement safe command and state boundaries**

Use argv-only subprocess calls, configured SSH aliases, BatchMode, explicit
timeouts, bounded captured output, atomic same-directory replacement, PID plus
timestamp lock metadata, and an explicit `--break-stale-lock` operation that
refuses a live PID or a lock younger than the configured threshold.

- [ ] **Step 4: Test timeout, truncation, and stale recovery**

Add fixtures for SSH timeout, nonzero remote exit, malformed JSON, oversized
logs, live lock, old dead lock, and interrupted state replacement.

- [ ] **Step 5: Run tests and commit**

Run: `uv run --with pytest pytest tests/spark_profiles/test_backend.py tests/spark_profiles/test_state.py -v`

```bash
git add src/spark_profiles config/controller.toml tests/spark_profiles
git commit -m "feat: add Spark controller backend and state"
```

### Task 4: Implement fail-to-stopped profile switching

**Files:**
- Create: `src/spark_profiles/switcher.py`
- Create: `tests/spark_profiles/test_switcher.py`
- Create: `docs/runbooks/model-switching.md`

**Interfaces:**
- Produces: `switch_profile(target_id: str, *, restore_home: bool = False) -> SwitchReport`.
- Consumes workload commands only through `SshBackend`.

- [ ] **Step 1: Write failing transition-order tests**

```python
def test_distributed_stop_is_head_first(harness):
    harness.switch("default", "maintenance")
    assert harness.remote_calls[:2] == [
        ("spark1", ("profile-stop", "deepseek-agent-dual", "head")),
        ("spark2", ("profile-stop", "deepseek-agent-dual", "worker")),
    ]

def test_failed_start_finishes_stopped(harness):
    harness.fail_health("generator")
    report = harness.switch("maintenance", "generator-only")
    assert report.status == "stopped"
    assert harness.published_endpoints == ()
```

- [ ] **Step 2: Confirm tests fail**

Run: `uv run --with pytest pytest tests/spark_profiles/test_switcher.py -v`

- [ ] **Step 3: Implement reconciliation**

Acquire the host-level lock, validate admission, drain changed endpoints, stop
heads before workers, verify memory/scratch recovery, retain unchanged healthy
workloads, start workers before heads, run health then quality gates, and
publish only accepted endpoints. On failure, stop partially started workloads
and persist the stopped/degraded result with log paths.

- [ ] **Step 4: Implement explicit home restoration**

`restore_home=True` requests `default` only after temporary outputs have
been recovered. Restoration failure is returned separately and never changes
the recorded identity of the model that produced an artifact.

- [ ] **Step 5: Run tests and commit**

Run: `uv run --with pytest pytest tests/spark_profiles/test_switcher.py -v && git diff --check`

```bash
git add src/spark_profiles/switcher.py tests/spark_profiles/test_switcher.py docs/runbooks/model-switching.md
git commit -m "feat: reconcile Spark cluster profiles"
```

### Task 5: Expose the developer-machine `sparkctl` CLI

**Files:**
- Create: `src/spark_profiles/cli.py`
- Create: `bin/sparkctl`
- Create: `tests/spark_profiles/test_cli.py`
- Create: `docs/runbooks/sparkctl.md`

**Interfaces:**
- Produces commands: `catalog`, `validate`, `status`, `switch`,
  `restore-default`, `endpoint`, and `break-stale-lock`.
- `status --json` is the stable interface consumed by the future NAS controller.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_agent_alias_resolves_to_full_default(cli):
    result = cli("switch", "agent", "--dry-run")
    assert result.json["target_profile"] == "default"

def test_endpoint_refuses_unhealthy_workload(cli):
    result = cli("endpoint", "qwen-image")
    assert result.exit_code == 3
    assert result.json["available"] is False
```

- [ ] **Step 2: Confirm tests fail**

Run: `uv run --with pytest pytest tests/spark_profiles/test_cli.py -v`

- [ ] **Step 3: Implement stable human and JSON output**

Every command supports `--json`; errors use stable exit codes and never print
secrets, environment variables, private keys, or full unbounded remote logs.
`switch --dry-run` shows the exact per-node transition without changing state.

- [ ] **Step 4: Verify the local default profile**

Run: `uv run sparkctl validate default && uv run sparkctl switch default --dry-run && uv run sparkctl status --json`

Expected: contracts and admission pass; the dry run shows Spark 2 before Spark
1 for start and no actual remote mutation.

- [ ] **Step 5: Run the suite and commit**

Run: `uv run --with pytest pytest tests/spark_profiles -v && git diff --check`

```bash
git add src/spark_profiles/cli.py bin/sparkctl tests/spark_profiles/test_cli.py docs/runbooks/sparkctl.md
git commit -m "feat: add developer Spark profile controller"
```
