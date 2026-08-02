# Model Profile Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the developer-machine workload catalog, whole-cluster profile model, and fail-to-stopped `sparkctl` switcher used by every local AI workload.

**Architecture:** Immutable Model Definitions describe one model service; Cluster Profiles compose the complete desired state of both Sparks. A developer-machine controller resolves stable selectors, validates content-addressed maturity and placement evidence, reconciles node state through key-only SSH, and publishes only healthy accepted loopback endpoints. Explicit restoration runs as a second ordinary transition after temporary outputs are recovered.

**Tech Stack:** Python 3.12, dataclasses, `tomllib`, JSON Schema, pytest, Bash, OpenSSH, Docker Compose, TOML.

**Approved design:** [Multi-runtime model profiles](../specs/2026-08-02-multi-runtime-model-profiles-design.md) and [model capacity overview](../../model-capacity-overview.md).

## Global Constraints

- The canonical home Cluster Profile ID is `agent-full-dual`; `default` and `agent` are convenience selectors for it, not profile IDs.
- Both DeepSeek definitions expose the stable client-facing model name `deepseek`; clients never select `single`, `dual`, `full`, `lite`, Mia, or DS4 as model names.
- A cluster profile specifies the complete state of Spark 1 and Spark 2.
- Dual-Spark workloads reserve both nodes; worker starts first and head stops first.
- Co-location is denied unless the exact cluster profile has passed measured acceptance.
- Every endpoint remains loopback-only and is consumed through SSH tunnels until the NAS control plane exists.
- Failed transitions end stopped or degraded and never silently select another model.
- Canonical artifacts and controller state live on the developer machine.
- Container images, source commits, checkpoints, and manifests are immutable.
- A cataloged production definition may remain `planned`; configuration presence never implies installation, acceptance, or activatability.
- The user-facing workload always selects the best accepted DGX Spark-optimized path; generic upstream paths are non-serving correctness references.
- Every adapter exposes `prepare`, `verify`, `start`, `health`, `infer`, `stop`, and `verify-release` operations.
- No model profile auto-starts after reboot.
- Work proceeds directly on `main` by explicit user instruction.

---

### Task 1: Reconcile workload contracts and the planned canonical home profile

This task continues the existing scaffold from commits `59ec2e6` and
`e102e75`. It closes the prior review block by representing DeepSeek as planned
intent rather than claiming that its Phase 3 adapter and manifest are already
installed or accepted.

**Files:**
- Modify: `src/spark_profiles/contracts.py`
- Modify: `schemas/cluster-profile.schema.json`
- Modify: `src/spark_profiles/schemas/cluster-profile.schema.json`
- Modify: `tests/spark_profiles/test_contracts.py`
- Rename: `config/cluster-profiles/default.toml` to `config/cluster-profiles/agent-full-dual.toml`
- Create: `config/profile-selectors.toml`

**Interfaces:**
- Produces: `load_workload(path: Path) -> WorkloadDefinition`.
- Produces: `load_cluster_profile(path: Path) -> ClusterProfile`.
- `ClusterProfile.placements` maps exactly `spark1` and `spark2` to tuples of workload IDs.
- `ClusterProfile` does not contain restoration policy; restoration is an explicit switch request.
- `CheckpointPin.manifest_sha256` is optional while a definition is planned and must match `^[0-9a-f]{64}$` when present.

- [x] **Step 1: Write failing contract tests**

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

def test_home_profile_uses_canonical_id_and_deepseek_alias():
    profile = load_cluster_profile(
        ROOT / "config/cluster-profiles/agent-full-dual.toml"
    )
    assert profile.id == "agent-full-dual"
    assert profile.endpoints == {"deepseek": "deepseek-agent-dual"}
    assert not hasattr(profile, "restore_home")
```

- [x] **Step 2: Run the focused tests and confirm failure**

Run: `uv run --with pytest --with jsonschema pytest tests/spark_profiles/test_contracts.py -v`

Expected: FAIL because `agent-full-dual.toml` is absent and the existing
contract still requires `restore_home`.

- [x] **Step 3: Implement strict typed contracts**

Keep the existing frozen strict workload dataclasses and unknown-key rejection.
Remove `restore_home` from both cluster-profile schema copies, the dataclass,
loader, and fixtures. Require a canonical profile ID, both node placements,
allowed endpoints, and the accepted-evidence path.

Add an optional `manifest_sha256` checkpoint property to both workload schema
copies and expose it as `str | None` on `CheckpointPin`. The planned production
definition omits it truthfully; Task 2 admission must reject any `accepted`
definition that lacks it. Adding the real digest later changes the definition
fingerprint and correctly invalidates prior evidence.

- [x] **Step 4: Add the initial DeepSeek home definitions**

`deepseek-agent-dual` remains a declarative planned definition; do not add an
adapter executable, local manifest, or acceptance claim in this task. Rename
the profile to `agent-full-dual.toml`, set `id = "agent-full-dual"`, place the
definition on both nodes, and expose `deepseek = "deepseek-agent-dual"`.
Create exactly:

```toml
[selectors]
default = "agent-full-dual"
agent = "agent-full-dual"
```

- [x] **Step 5: Run validation and commit**

Run: `uv run --with pytest --with jsonschema pytest tests/spark_profiles/test_contracts.py -v && git diff --check`

Expected: PASS.

```bash
git add src/spark_profiles/contracts.py src/spark_profiles/schemas \
  schemas/cluster-profile.schema.json tests/spark_profiles/test_contracts.py \
  config/cluster-profiles config/profile-selectors.toml
git commit -m "fix: reconcile canonical Spark home profile"
```

### Task 2: Build catalog and admission validation

**Files:**
- Create: `src/spark_profiles/catalog.py`
- Create: `src/spark_profiles/admission.py`
- Create: `tests/spark_profiles/test_catalog.py`
- Create: `tests/spark_profiles/test_admission.py`
- Create: `schemas/model-definitions.schema.json`
- Create: `schemas/accepted-cluster-profiles.schema.json`
- Create: `src/spark_profiles/schemas/model-definitions.schema.json`
- Create: `src/spark_profiles/schemas/accepted-cluster-profiles.schema.json`
- Modify: `pyproject.toml`
- Create: `locks/model-definitions.toml`
- Create: `inventory/reports/model-definitions.json`
- Create: `inventory/reports/accepted-cluster-profiles.json`

**Interfaces:**
- Consumes: `WorkloadDefinition` and `ClusterProfile`.
- Produces: `Catalog.load(root: Path) -> Catalog`.
- Produces: `Catalog.resolve_profile(selector: str) -> ClusterProfile`.
- Produces content SHA-256 fingerprints from normalized dataclass JSON with sorted keys and compact separators.
- Produces: `check_admission(profile, catalog, inventory) -> AdmissionReport`.

- [x] **Step 1: Write failing catalog/admission tests**

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

def test_default_selector_resolves_to_canonical_home(catalog):
    assert catalog.resolve_profile("default").id == "agent-full-dual"

def test_planned_definition_blocks_production_home(catalog, inventory):
    report = check_admission(catalog.resolve_profile("default"), catalog, inventory)
    assert "deepseek-agent-dual maturity is planned" in report.errors

def test_accepted_definition_requires_manifest_digest(catalog, inventory):
    mark_definition_accepted(catalog, "deepseek-agent-dual")
    report = check_admission(catalog.resolve_profile("default"), catalog, inventory)
    assert "accepted definition requires manifest_sha256" in report.errors

def test_definition_change_invalidates_lock(catalog_root):
    rewrite_resource_envelope(catalog_root, "deepseek-agent-dual")
    with pytest.raises(CatalogError, match="lock fingerprint"):
        Catalog.load(catalog_root)

def test_evidence_indexes_satisfy_packaged_schemas(catalog_root):
    validate_evidence_indexes(catalog_root)
```

- [x] **Step 2: Confirm tests fail**

Run: `uv run --with pytest pytest tests/spark_profiles/test_catalog.py tests/spark_profiles/test_admission.py -v`

Expected: import failure for catalog/admission modules.

- [x] **Step 3: Implement deterministic validation**

Reject missing definitions, port collisions, cache/output overlap, conflicting
workloads, partial distributed placement, insufficient measured memory/disk,
unaccepted co-location, and publication of an unhealthy/unaccepted endpoint.
Return all errors in stable sorted order without mutating state.

Also reject missing/duplicate selector targets, locks, or maturity records. The
checked-in `deepseek-agent-dual` fingerprint begins at `planned` in
`inventory/reports/model-definitions.json`; the accepted-profile index begins
with an empty `profiles` array. Generate and commit the actual fingerprint—do
not use a placeholder. Only the exact `accepted` fingerprint may satisfy
admission.

- [x] **Step 4: Add signed-off evidence indexing**

Normalize definitions and profiles as UTF-8 JSON using `sort_keys=True` and
`separators=(",", ":")`, representing paths as POSIX strings. Index acceptance
by Cluster Profile content SHA-256 plus sorted Model Definition hashes. A
changed resource envelope, runtime pin, placement, endpoint, or command
invalidates prior acceptance. TOML whitespace and comments do not.

- [x] **Step 5: Run tests and commit**

Run: `uv run --with pytest pytest tests/spark_profiles/test_catalog.py tests/spark_profiles/test_admission.py -v && git diff --check`

```bash
git add src/spark_profiles schemas pyproject.toml tests/spark_profiles \
  locks/model-definitions.toml \
  inventory/reports/model-definitions.json \
  inventory/reports/accepted-cluster-profiles.json
git commit -m "feat: validate content-addressed profile admission"
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
- Produces: `SshBackend.run_script(node, script, argv, timeout) -> CommandResult` for fixed repository-owned read-only collectors sent over stdin.
- Produces: `StateStore.acquire() -> ContextManager[ControllerState]`.
- Stores state under `.state/sparkctl/state.json` and locks
  `.state/sparkctl/switch.lock` on the developer machine.

- [x] **Step 1: Write failing backend and stale-lock tests**

```python
def test_backend_never_uses_shell(fake_exec):
    SshBackend(fake_exec).run("spark1", ("profile-status", "--json"), 10)
    assert fake_exec.shell is False
    assert "BatchMode=yes" in fake_exec.argv
    assert "ForwardAgent=no" in fake_exec.argv
    assert "IdentitiesOnly=yes" in fake_exec.argv
    assert "StrictHostKeyChecking=yes" in fake_exec.argv

def test_run_script_delivers_fixed_bytes_on_stdin(fake_exec):
    SshBackend(fake_exec).run_script(
        "spark2", b"printf '{}\\n'\n", ("--json",), 10
    )
    assert fake_exec.input_bytes == b"printf '{}\\n'\n"
    assert fake_exec.shell is False

def test_state_write_is_atomic(store, interrupted_replace):
    with pytest.raises(OSError):
        store.save({"active_profile": "broken"})
    assert store.load().active_profile == "default"
```

- [x] **Step 2: Confirm tests fail**

Run: `uv run --with pytest pytest tests/spark_profiles/test_backend.py tests/spark_profiles/test_state.py -v`

- [x] **Step 3: Implement safe command and state boundaries**

Use argv-only subprocess calls, configured SSH aliases, BatchMode, explicit
timeouts, bounded captured output, atomic same-directory replacement, PID plus
timestamp lock metadata, and an explicit `--break-stale-lock` operation that
refuses a live PID or a lock younger than the configured threshold.

`run_script` uses the same strict SSH argv with remote `bash -s --` and sends
only caller-provided repository script bytes over stdin. Reject NUL/newline
characters in remote argv. It never persists a file or turns into a general
interactive shell interface.

- [x] **Step 4: Test timeout, truncation, and stale recovery**

Add fixtures for SSH timeout, nonzero remote exit, malformed JSON, oversized
logs, live lock, old dead lock, and interrupted state replacement.

- [x] **Step 5: Run tests and commit**

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
- Produces: `switch_profile(target_id: str, *, restore_to: str | None = None, dry_run: bool = False) -> SwitchReport`.
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

`restore_to` resolves an explicit profile selector only after temporary outputs
and provenance have been recovered. `restore-default` passes `default`, which
resolves to `agent-full-dual`. Restoration is a second ordinary transition;
its failure is returned separately and never changes the recorded profile and
definition fingerprint that produced an artifact. Restoration policy never
lives inside a Cluster Profile.

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
    assert result.json["target_profile"] == "agent-full-dual"

def test_endpoint_refuses_unhealthy_workload(cli):
    result = cli("endpoint", "deepseek")
    assert result.exit_code == 3
    assert result.json["available"] is False

def test_planned_home_is_visible_but_not_activatable(cli):
    result = cli("validate", "default", "--json")
    assert result.json["profile_id"] == "agent-full-dual"
    assert result.json["valid"] is True
    assert result.json["admitted"] is False
    assert result.exit_code == 3
```

- [ ] **Step 2: Confirm tests fail**

Run: `uv run --with pytest pytest tests/spark_profiles/test_cli.py -v`

- [ ] **Step 3: Implement stable human and JSON output**

Every command supports `--json`; errors use stable exit codes and never print
secrets, environment variables, private keys, or full unbounded remote logs.
`switch --dry-run` shows the exact per-node transition without changing state.
Use exit 0 for success, 2 for arguments/configuration, 3 for admission or
endpoint denial, 6 for transition/restoration failure, and 7 for lock conflict.
Reserve 4 and 5 for live node health.

- [ ] **Step 4: Verify the local default profile**

Run: `uv run sparkctl catalog --json && uv run sparkctl validate default --json && uv run sparkctl status --json`

Expected: catalog and contracts load; `default` resolves to
`agent-full-dual`; admission is truthfully denied because
`deepseek-agent-dual` remains planned until the runtime phase; status is local
and stopped, with no remote mutation.

- [ ] **Step 5: Run the suite and commit**

Run: `uv run --with pytest pytest tests/spark_profiles -v && git diff --check`

```bash
git add src/spark_profiles/cli.py bin/sparkctl tests/spark_profiles/test_cli.py docs/runbooks/sparkctl.md
git commit -m "feat: add developer Spark profile controller"
```
