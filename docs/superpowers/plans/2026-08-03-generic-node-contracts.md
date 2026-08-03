# Generic Node Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add versioned, N-node identity, inventory, topology, installation, and job contracts while preserving read compatibility with the current two-Spark inventory.

**Architecture:** New immutable Python domain types live in `spark_profiles.fleet` and parse schema-versioned TOML/JSON through focused loaders. Current `inventory/cluster.toml` is adapted into the generic model by a compatibility reader; current runtime, backend, health, and adapter files remain untouched.

**Tech Stack:** Python 3.12, dataclasses, `tomllib`, JSON Schema 2020-12, jsonschema, pytest.

## Global Constraints

- Node identity is a generated immutable ID, never a hostname, alias, role, or address.
- Collections accept one or more nodes and have no maximum count.
- Management addresses, SSH users, aliases, interfaces, and fabric addresses are data, not constants.
- Existing two-node files remain readable and are never silently rewritten.
- Do not modify the active roadmap agent's SSH transport, backend, fabric validator, release deployer, DS4 adapter, or their tests.
- Every parser rejects unknown schema versions and malformed or duplicate identity data.
- Keep both schema copies (`schemas/` and package schemas) byte-identical.

---

### Task 1: Define generic node and fleet domain types

**Files:**
- Create: `src/spark_profiles/fleet/__init__.py`
- Create: `src/spark_profiles/fleet/types.py`
- Test: `tests/spark_profiles/fleet/test_types.py`

**Interfaces:**
- Produces: `NodeId.parse(value: str) -> NodeId`, `ManagementEndpoint`, `NodeRecord`, `Fleet`.
- Produces: `Fleet.node(node_id: NodeId) -> NodeRecord` and `Fleet.ready_nodes() -> tuple[NodeRecord, ...]`.

- [ ] **Step 1: Write failing immutable identity and unbounded fleet tests**

```python
from dataclasses import FrozenInstanceError
import pytest
from spark_profiles.fleet import Fleet, ManagementEndpoint, NodeId, NodeRecord


def node(index: int) -> NodeRecord:
    return NodeRecord(
        id=NodeId.parse(f"spk_{index:032x}"),
        display_name=f"lab-{index}",
        hostname=f"spark-{index}",
        management=ManagementEndpoint(host=f"10.0.0.{index + 1}", user="admin"),
        labels={"rack": "lab"},
        lifecycle="ready",
    )


def test_fleet_has_no_fixed_node_names_or_maximum():
    fleet = Fleet(schema_version=2, nodes={item.id: item for item in map(node, range(32))})
    assert len(fleet.ready_nodes()) == 32
    assert all(record.display_name.startswith("lab-") for record in fleet.ready_nodes())


def test_node_identity_is_immutable():
    record = node(1)
    with pytest.raises(FrozenInstanceError):
        record.id = NodeId.parse("spk_ffffffffffffffffffffffffffffffff")


@pytest.mark.parametrize("value", ["spark1", "", "spk_1", "spk_" + "g" * 32])
def test_node_id_rejects_names_and_malformed_ids(value):
    with pytest.raises(ValueError):
        NodeId.parse(value)
```

- [ ] **Step 2: Run the tests and verify the package is absent**

Run: `uv run pytest tests/spark_profiles/fleet/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError: spark_profiles.fleet`.

- [ ] **Step 3: Implement frozen focused types**

```python
@dataclass(frozen=True, order=True)
class NodeId:
    value: str

    @classmethod
    def parse(cls, value: str) -> "NodeId":
        if re.fullmatch(r"spk_[0-9a-f]{32}", value) is None:
            raise ValueError("node id must match spk_<32 lowercase hex characters>")
        return cls(value)


@dataclass(frozen=True)
class ManagementEndpoint:
    host: str
    user: str
    port: int = 22
    credential_ref: str | None = None


@dataclass(frozen=True)
class NodeRecord:
    id: NodeId
    display_name: str
    hostname: str
    management: ManagementEndpoint
    labels: Mapping[str, str]
    lifecycle: Literal["discovered", "installing", "ready", "quarantined", "draining", "retired"]


@dataclass(frozen=True)
class Fleet:
    schema_version: int
    nodes: Mapping[NodeId, NodeRecord]
```

Validate nonblank display name, hostname, host, and user; port range 1–65535; unique display names; copied immutable mappings; supported schema version `2`; and nonempty node maps.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/spark_profiles/fleet/test_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit the domain types**

```bash
git add src/spark_profiles/fleet tests/spark_profiles/fleet/test_types.py
git commit -m "feat: define generic Spark fleet types"
```

### Task 2: Add generic fleet and topology schemas

**Files:**
- Create: `schemas/fleet.schema.json`
- Create: `schemas/topology.schema.json`
- Create: `src/spark_profiles/schemas/fleet.schema.json`
- Create: `src/spark_profiles/schemas/topology.schema.json`
- Modify: `pyproject.toml`
- Test: `tests/spark_profiles/fleet/test_schemas.py`

**Interfaces:**
- Consumes: node ID syntax `^spk_[0-9a-f]{32}$` and lifecycle values from Task 1.
- Produces: JSON Schema contracts `dgx-forge/fleet/v2` and `dgx-forge/topology/v1`.

- [ ] **Step 1: Write failing schema tests with one and sixteen nodes**

```python
def test_fleet_schema_accepts_one_and_sixteen_nodes(fleet_validator, fleet_document):
    fleet_validator.validate(fleet_document(1))
    fleet_validator.validate(fleet_document(16))


def test_fleet_schema_rejects_fixed_name_identity(fleet_validator, fleet_document):
    document = fleet_document(1)
    node = document["nodes"].pop(next(iter(document["nodes"])))
    document["nodes"]["spark1"] = node
    with pytest.raises(jsonschema.ValidationError):
        fleet_validator.validate(document)


def test_topology_rejects_unknown_node(topology_validator, topology_document):
    document = topology_document()
    document["links"][0]["endpoints"][1]["node_id"] = "spk_ffffffffffffffffffffffffffffffff"
    with pytest.raises(TopologyValidationError, match="unknown node"):
        validate_topology_references(document)
```

- [ ] **Step 2: Run tests and verify schema files are absent**

Run: `uv run pytest tests/spark_profiles/fleet/test_schemas.py -v`
Expected: FAIL because `schemas/fleet.schema.json` is absent.

- [ ] **Step 3: Implement schemas and reference validation**

Fleet documents contain `schema_version`, a nonempty `nodes` object keyed by node ID, and node fields `display_name`, `hostname`, `management`, `labels`, and `lifecycle`. Topology documents contain `schema_version`, `nodes`, and `links`; each link has a stable ID, `kind` (`management`, `direct-rdma`, or `switched-rdma`), and at least two endpoint objects. Implement `validate_topology_references(document)` in `fleet/loaders.py` to reject endpoint IDs missing from the topology's declared node set.

- [ ] **Step 4: Verify package and repository schemas match**

Run: `cmp schemas/fleet.schema.json src/spark_profiles/schemas/fleet.schema.json && cmp schemas/topology.schema.json src/spark_profiles/schemas/topology.schema.json && uv run pytest tests/spark_profiles/fleet/test_schemas.py -v`
Expected: both `cmp` commands and all tests PASS.

- [ ] **Step 5: Commit schemas**

```bash
git add schemas src/spark_profiles/schemas pyproject.toml src/spark_profiles/fleet/loaders.py tests/spark_profiles/fleet/test_schemas.py
git commit -m "feat: add generic fleet and topology schemas"
```

### Task 3: Load generic fleet TOML

**Files:**
- Modify: `src/spark_profiles/fleet/loaders.py`
- Test: `tests/spark_profiles/fleet/test_loaders.py`
- Create: `tests/fixtures/fleet/generic.toml`

**Interfaces:**
- Produces: `load_fleet(path: Path) -> Fleet`.
- Raises: `FleetLoadError` with a safe path and reason, never secret values.

- [ ] **Step 1: Write failing loader tests**

```python
def test_load_fleet_uses_generated_ids_and_preserves_addresses(fixtures):
    fleet = load_fleet(fixtures / "generic.toml")
    assert tuple(node.display_name for node in fleet.ready_nodes()) == ("alpha", "beta")
    assert fleet.ready_nodes()[1].management.host == "spark-beta.local"


def test_load_fleet_rejects_duplicate_display_names(tmp_path):
    path = write_fleet(tmp_path, display_names=("same", "same"))
    with pytest.raises(FleetLoadError, match="display name"):
        load_fleet(path)
```

- [ ] **Step 2: Run tests and observe missing loader behavior**

Run: `uv run pytest tests/spark_profiles/fleet/test_loaders.py -v`
Expected: FAIL because `load_fleet` is undefined.

- [ ] **Step 3: Implement strict TOML mapping**

Use `tomllib.load`, require `schema_version = 2`, reject unknown top-level and node keys, parse every ID through `NodeId.parse`, construct `ManagementEndpoint` and `NodeRecord`, and let `Fleet` enforce cross-node uniqueness. Do not resolve DNS or contact a node while loading configuration.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/spark_profiles/fleet/test_loaders.py -v`
Expected: PASS.

- [ ] **Step 5: Commit loader**

```bash
git add src/spark_profiles/fleet tests/spark_profiles/fleet/test_loaders.py tests/fixtures/fleet/generic.toml
git commit -m "feat: load generic fleet configuration"
```

### Task 4: Add legacy two-Spark compatibility reader

**Files:**
- Create: `src/spark_profiles/fleet/legacy.py`
- Test: `tests/spark_profiles/fleet/test_legacy.py`

**Interfaces:**
- Produces: `load_legacy_cluster(path: Path) -> Fleet`.
- Uses: deterministic UUIDv5-derived node IDs scoped by canonical repository identity and legacy key, never LAN address or hostname.

- [ ] **Step 1: Write failing compatibility tests**

```python
def test_current_cluster_loads_without_rewrite(repository_root):
    before = (repository_root / "inventory/cluster.toml").read_bytes()
    fleet = load_legacy_cluster(repository_root / "inventory/cluster.toml")
    assert len(fleet.nodes) == 2
    assert {node.management.host for node in fleet.nodes.values()} == {
        "dgx-spark-1", "dgx-spark-2"
    }
    assert (repository_root / "inventory/cluster.toml").read_bytes() == before


def test_legacy_identity_does_not_change_with_address(tmp_path):
    first = load_legacy_cluster(write_legacy(tmp_path, lan_ip="10.0.0.10"))
    second = load_legacy_cluster(write_legacy(tmp_path, lan_ip="10.0.0.99"))
    assert tuple(first.nodes) == tuple(second.nodes)
```

- [ ] **Step 2: Run tests and verify reader is absent**

Run: `uv run pytest tests/spark_profiles/fleet/test_legacy.py -v`
Expected: FAIL with import error for `spark_profiles.fleet.legacy`.

- [ ] **Step 3: Implement deterministic read-only adaptation**

Parse `[hosts.<legacy-name>]`, map `ssh_alias` to management host, retain hostname and role as labels, derive `NodeId` from `uuid.uuid5(PROJECT_NAMESPACE, f"legacy:{legacy_name}").hex`, and set lifecycle to `ready`. Reject empty host maps and duplicate aliases. Do not expose a write function.

- [ ] **Step 4: Run compatibility and full contract tests**

Run: `uv run pytest tests/spark_profiles/fleet -v`
Expected: PASS.

- [ ] **Step 5: Commit compatibility seam**

```bash
git add src/spark_profiles/fleet/legacy.py tests/spark_profiles/fleet/test_legacy.py
git commit -m "feat: adapt legacy two-Spark inventory"
```

### Task 5: Define installation and durable-job contracts

**Files:**
- Create: `src/spark_profiles/fleet/install_contracts.py`
- Create: `src/spark_profiles/fleet/job_contracts.py`
- Test: `tests/spark_profiles/fleet/test_install_contracts.py`
- Test: `tests/spark_profiles/fleet/test_job_contracts.py`

**Interfaces:**
- Produces: `InstallationRequest`, `InstallationStep`, `InstallationJournal`, `JobId`, `JobAttempt`, and legal transition functions.
- Installation states: `discovered`, `identity-gated`, `inventoried`, `key-installed`, `hardened`, `policy-applied`, `accepted`, `failed`.
- Job states: `queued`, `running`, `waiting-for-operator`, `succeeded`, `failed`, `cancelled`.

- [ ] **Step 1: Write failing transition and redaction tests**

```python
def test_installation_cannot_skip_identity_gate(request):
    journal = InstallationJournal.start(request)
    with pytest.raises(InvalidTransition):
        journal.advance("inventoried", evidence_digest="a" * 64)


def test_job_attempt_fences_stale_worker(job):
    active = job.claim(worker_id="worker-a", now=NOW)
    replacement = active.expire_and_requeue(now=LATER).claim(worker_id="worker-b", now=LATER)
    with pytest.raises(StaleAttempt):
        replacement.complete(attempt=active.attempt, now=LATER)


def test_serialized_request_uses_credential_reference_only(request):
    payload = request.as_public_dict()
    assert payload["credential_ref"] == "secret://ssh/admin"
    assert "private" not in repr(payload).lower()
```

- [ ] **Step 2: Run tests and confirm contracts are missing**

Run: `uv run pytest tests/spark_profiles/fleet/test_install_contracts.py tests/spark_profiles/fleet/test_job_contracts.py -v`
Expected: FAIL with missing modules.

- [ ] **Step 3: Implement frozen state machines**

Use explicit transition maps, SHA-256 evidence digest validation, UTC timestamps, monotonically increasing attempt integers, lease expiry, and fenced mutation methods returning new immutable objects. Reject raw credentials and unknown payload fields at construction.

- [ ] **Step 4: Run phase tests and full baseline**

Run: `uv run pytest tests/spark_profiles/fleet -v && uv run pytest -q`
Expected: all tests PASS with the existing single skip unchanged.

- [ ] **Step 5: Commit contracts**

```bash
git add src/spark_profiles/fleet tests/spark_profiles/fleet
git commit -m "feat: define installation and job contracts"
```

### Task 6: Document the migration seam

**Files:**
- Create: `docs/runbooks/fleet-migration.md`
- Modify: `README.md`
- Test: `tests/runbooks/test_fleet_migration.py`

**Interfaces:**
- Documents: read-only legacy behavior, generic schema location, generated IDs, rollback, and Phase 2 ownership gate.

- [ ] **Step 1: Write a failing documentation-link test**

```python
def test_readme_links_generic_fleet_migration(repository_root):
    readme = (repository_root / "README.md").read_text()
    assert "docs/runbooks/fleet-migration.md" in readme
    assert "no fixed node count" in readme.lower()
```

- [ ] **Step 2: Run and observe failure**

Run: `uv run pytest tests/runbooks/test_fleet_migration.py -v`
Expected: FAIL because the link is absent.

- [ ] **Step 3: Write the runbook and README entry**

Document that Phase 0 adds no live-node mutation, show `load_fleet` and legacy inspection commands, explain deterministic legacy IDs, state that generic serialization is one-way and explicit, and identify the active-roadmap files that remain untouched until integration.

- [ ] **Step 4: Verify docs and full suite**

Run: `uv run pytest tests/runbooks/test_fleet_migration.py tests/test_shell_suites.py -v && git diff --check`
Expected: PASS and no whitespace errors.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/runbooks/fleet-migration.md tests/runbooks/test_fleet_migration.py
git commit -m "docs: explain generic fleet migration"
```
