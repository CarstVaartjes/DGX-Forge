# Agent Protocol and Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define the versioned, node-scoped agent protocol and durable database state required for outbound fenced work.

**Architecture:** Pure contracts validate every message before persistence. PostgreSQL stores agent identity/session state and node-operation attempts, while Git remains desired-state authority and the existing parent job remains the operator-visible unit.

**Tech Stack:** Python 3.12, standalone protocol wheel, dataclasses, JSON Schema 2020-12, SQLAlchemy 2, Alembic, PostgreSQL, pytest

## Global Constraints

- Routine agent messages never contain arbitrary commands, shell text, environment maps, credentials, or client-selected filesystem paths.
- Every operation is bound to one canonical `spk_[0-9a-f]{32}` node ID, parent job, attempt, fence, payload digest, repository commit, and deadline.
- Unknown fields, stale fences, cross-node claims, oversized payloads, and secret-bearing keys fail closed.
- Git remains the desired-state authority; PostgreSQL stores operational state only.
- Migrations are reversible and expand-compatible with the current control API and worker.

---

### Task 1: Versioned protocol value objects and schemas

**Files:**
- Create: `agent_protocol/pyproject.toml`
- Create: `agent_protocol/src/dgx_agent_protocol/__init__.py`
- Create: `agent_protocol/src/dgx_agent_protocol/contracts.py`
- Create: `agent_protocol/src/dgx_agent_protocol/schemas/agent-job.schema.json`
- Create: `agent_protocol/src/dgx_agent_protocol/schemas/agent-result.schema.json`
- Test: `agent_protocol/tests/test_contracts.py`

**Interfaces:**
- Produces: `AgentOperation`, `AgentClaim`, `AgentProgress`, `AgentResult`, `canonical_message(value) -> bytes`.
- Consumes later: control API serializers and the Spark agent client.

- [ ] **Step 1: Write failing contract tests**

```python
def test_claim_is_node_scoped_and_canonical() -> None:
    claim = AgentClaim.parse({
        "schema_version": 1, "job_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000002", "attempt": 1,
        "fence": "00000000-0000-4000-8000-000000000003",
        "node_id": "spk_00000000000000000000000000000001",
        "operation": "node.probe", "base_commit": "a" * 40,
        "payload_digest": hashlib.sha256(b"{}").hexdigest(), "payload": {},
        "deadline": "2026-08-03T12:00:00+00:00",
    })
    assert json.loads(canonical_message(claim))["operation"] == "node.probe"

@pytest.mark.parametrize("field", ["command", "shell", "environment", "password"])
def test_protocol_rejects_execution_and_secret_fields(field: str) -> None:
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(valid_claim() | {"payload": {field: "unsafe"}})
```

- [ ] **Step 2: Run the tests and verify the module is absent**

Run: `uv run --project agent_protocol pytest agent_protocol/tests/test_contracts.py -v`
Expected: FAIL because the standalone protocol package is absent.

- [ ] **Step 3: Implement frozen value objects and exact schemas**

Create the independently versioned `dgx-agent-protocol` wheel. Use an enum containing only `node.probe`, `release.install`,
`workload.prepare`, `workload.start`, `workload.stop`, `workload.health`,
`workload.verify`, `agent.update`, and `agent.rollback`. Recursively reject keys
matching `password|secret|token|authorization|private.?key|command|shell|environment`,
limit canonical payloads/results to 64 KiB, require aware UTC deadlines, and
copy through canonical JSON before constructing frozen objects.

- [ ] **Step 4: Verify packaged and repository schemas match**

Include the two schemas as wheel artifacts and run:

`uv run --project agent_protocol pytest agent_protocol/tests/test_contracts.py -v`

Expected: PASS with byte-identical schema assertions.

- [ ] **Step 5: Commit contracts**

```bash
git add agent_protocol
git commit -m "feat: define fenced Spark agent protocol"
```

### Task 2: Durable agent and operation models

**Files:**
- Modify: `control/src/dgx_control/models.py`
- Create: `control/migrations/versions/0002_agent_operations.py`
- Test: `control/tests/test_agent_migrations.py`

**Interfaces:**
- Produces tables `agent_nodes`, `agent_certificates`, `agent_operations`, and `agent_operation_attempts`.
- `agent_operations.parent_job_id` references `jobs.id`; each operation has exactly one target node.

- [ ] **Step 1: Write failing upgrade/downgrade tests**

```python
def test_agent_migration_is_reversible(database) -> None:
    upgrade_to("0002_agent_operations", database)
    assert {"agent_nodes", "agent_certificates", "agent_operations", "agent_operation_attempts"} <= tables(database)
    downgrade_to("0001_operational_state", database)
    assert "agent_nodes" not in tables(database)
    assert "jobs" in tables(database)
```

- [ ] **Step 2: Run and observe missing revision**

Run: `uv run --project control pytest control/tests/test_agent_migrations.py -v`
Expected: FAIL because revision `0002_agent_operations` does not exist.

- [ ] **Step 3: Implement expand-compatible models and migration**

Add:

```python
class AgentNode(Base):
    __tablename__ = "agent_nodes"
    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

`AgentCertificate` stores serial, node ID, not-before/not-after, fingerprint,
and revocation time—never a private key. `AgentOperation` stores parent job,
node, kind, payload/digest, base commit, state, and current attempt.
`AgentOperationAttempt` stores unique `(operation_id, attempt)`, fence,
lease deadline, agent certificate serial, state, progress, and result.

- [ ] **Step 4: Run migration and model tests**

Run: `uv run --project control pytest control/tests/test_migrations.py control/tests/test_agent_migrations.py -v`
Expected: PASS.

- [ ] **Step 5: Commit persistence**

```bash
git add control/src/dgx_control/models.py control/migrations/versions/0002_agent_operations.py control/tests/test_agent_migrations.py
git commit -m "feat: persist Spark agent operations"
```

### Task 3: Fenced node-operation service

**Files:**
- Create: `control/src/dgx_control/agent_jobs.py`
- Test: `control/tests/test_agent_jobs.py`

**Interfaces:**
- Produces: `AgentJobService.enqueue(parent_job_id, node_id, operation, base_commit, payload)`, `claim(node_id, certificate_serial, lease_seconds)`, `heartbeat(fence, progress, lease_seconds)`, `succeed(fence, result)`, `fail(fence, reason)`.
- Consumes: protocol objects from Task 1 and models from Task 2.

- [ ] **Step 1: Write failing claim/fence tests**

```python
def test_agent_can_claim_only_its_node_operation(service) -> None:
    operation = service.enqueue(parent, NODE_A, "node.probe", COMMIT, {})
    assert service.claim(NODE_B, "serial-b", 30) is None
    claim = service.claim(NODE_A, "serial-a", 30)
    assert claim.operation_id == operation.id and claim.node_id == NODE_A

def test_expired_attempt_cannot_publish_success(service, clock) -> None:
    first = claim_one(service)
    clock.advance(seconds=31)
    second = service.claim(NODE_A, "serial-a", 30)
    with pytest.raises(StaleAgentAttempt):
        service.succeed(first.fence, {"healthy": True})
    service.succeed(second.fence, {"healthy": True})
```

- [ ] **Step 2: Run and verify service is absent**

Run: `uv run --project control pytest control/tests/test_agent_jobs.py -v`
Expected: FAIL importing `dgx_control.agent_jobs`.

- [ ] **Step 3: Implement transactional claims and terminal aggregation**

Use `SELECT ... FOR UPDATE SKIP LOCKED`, canonical payload validation, exact
node filtering, certificate-state lookup, lease expiry, and a new UUID fence
per attempt. Update the parent job only after all its operations are terminal:
all succeeded -> succeeded; any failed -> failed; any waiting ->
waiting-for-operator. Redact and bound reasons to 1024 characters.

- [ ] **Step 4: Run concurrency and existing queue tests**

Run: `uv run --project control pytest control/tests/test_agent_jobs.py control/tests/test_jobs.py -v`
Expected: PASS with no regression to control-worker jobs.

- [ ] **Step 5: Commit operation service**

```bash
git add control/src/dgx_control/agent_jobs.py control/tests/test_agent_jobs.py
git commit -m "feat: queue fenced node agent operations"
```

### Task 4: Phase verification

**Files:**
- Modify: `control/pyproject.toml`
- Modify: `control/uv.lock`
- Modify: `control/Dockerfile`
- Modify: `agent/pyproject.toml`
- Modify: `scripts/verify-supply-chain`
- Modify: `inventory/sbom/manifest.json`
- Modify: `docs/security/threat-model.md`
- Test: `control/tests/security/test_agent_protocol.py`
- Test: `tests/scripts/test_verify_supply_chain.py`

**Interfaces:**
- Produces a protocol wheel installed in both control and agent environments, plus security assertions used by later plans.

- [ ] **Step 1: Add packaging and boundary tests**

Test cross-node claim denial, revoked certificate denial, secret-key rejection,
payload/result size limits, stale success denial, and lack of an arbitrary
operation enum member. Extend the supply-chain test to require the protocol
lock/wheel hash and SPDX document. Build the control image from repository-root
context and assert it imports the exact protocol version; assert the agent lock
resolves the same version.

- [ ] **Step 2: Run the security tests before documentation edits**

Run: `uv run --project control pytest control/tests/security/test_agent_protocol.py -v && uv run pytest tests/scripts/test_verify_supply_chain.py -v`
Expected: FAIL until both environments and supply evidence include the protocol wheel.

- [ ] **Step 3: Wire the shared package and document the threat boundary**

Add exact local workspace sources for development and install the built protocol
wheel into both release artifacts; do not copy its source independently into
either package. Update Docker build context/copies and generate an SPDX entry.
Add agent impersonation, enrollment replay, stale fence, malicious payload,
result exfiltration, and certificate theft to the threat model with concrete
mitigations and residual recovery requirements.

- [ ] **Step 4: Run Phase 1 suites**

Run: `uv run --project agent_protocol pytest agent_protocol/tests -q && uv run --project control pytest control/tests/test_agent_jobs.py control/tests/test_agent_migrations.py control/tests/security/test_agent_protocol.py -q && uv run pytest tests/scripts/test_verify_supply_chain.py -q && scripts/verify-supply-chain --json && git diff --check`
Expected: all tests pass and no whitespace errors.

- [ ] **Step 5: Commit phase evidence**

```bash
git add control/pyproject.toml control/uv.lock control/Dockerfile agent/pyproject.toml scripts/verify-supply-chain inventory/sbom docs/security/threat-model.md control/tests/security/test_agent_protocol.py tests/scripts/test_verify_supply_chain.py
git commit -m "docs: harden outbound agent protocol boundary"
```
