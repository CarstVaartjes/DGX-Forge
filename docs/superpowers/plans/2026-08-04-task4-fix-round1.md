# Task 4 Fix Round 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make certificate renewal issuance durable and non-reissuable across ambiguity, preserve staged-certificate denial across downgrade, and durably fail/replay expired active claims.

**Architecture:** A node-unique rotation-intent row is committed before the external CA call and becomes the authority for in-progress/manual recovery; only the creator may call the provider, using the persisted request ID. Downgrade maps non-active state into the previous schema's `revoked_at` denial predicate. Operation recovery persists one exact-fence `claim_deadline_expired` result before submission.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, FastAPI, SQLite, PostgreSQL 16, pytest, Smallstep step-ca, Docker Compose.

## Global Constraints

- Work directly on `main`; do not push or open a pull request.
- Preserve and do not stage `.superpowers/sdd/2026-08-03-node-agent-runtime/progress.md`.
- Write each behavioral regression before production code and observe its expected failure.
- The persisted control-plane issuance state is authoritative; agent retry behavior cannot be the only issuance bound.
- Ambiguous provider outcomes never authorize an automatic second CA call.
- Preserve fixed `/agent/v1/...` routes, canonical JSON, strict active/staged authorization, and exact result fencing.

---

### Task 1: Durable certificate-rotation issuance intent

**Files:**
- Modify: `control/tests/test_enrollment.py`
- Modify: `control/tests/test_agent_api.py`
- Modify: `control/tests/test_step_ca.py`
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/src/vonk_control/enrollment.py`
- Modify: `control/src/vonk_control/agent_api.py`
- Modify: `control/src/vonk_control/pki.py`
- Modify: `control/src/vonk_control/step_ca.py`
- Modify: `control/migrations/versions/0005_certificate_rotation.py`

**Interfaces:**
- Consumes: active certificate identity, normalized CSR, `CertificateAuthority.renew_node`, and the existing staged-certificate replay.
- Produces: `AgentCertificateRotation`, `RenewalInProgress`, `RenewalIssuanceUncertain`, and `renew_node(node_id, csr_pem, now, *, request_id)`.

- [x] **Step 1: Write failing service tests**

```python
def test_renewal_provider_exception_is_durable_manual_recovery(service):
    authority.raise_after_issue = True
    with pytest.raises(RenewalIssuanceUncertain):
        enrollment.renew(NODE_ID, issued.serial, request)
    with pytest.raises(RenewalIssuanceUncertain):
        enrollment.renew(NODE_ID, issued.serial, request)
    assert len(authority.calls) == 2  # enrollment plus exactly one renewal
    assert stored_rotation.state == "manual-recovery"

def test_process_death_keeps_intent_and_never_reissues(service):
    authority.abort_after_issue = True
    with pytest.raises(SystemExit):
        enrollment.renew(NODE_ID, issued.serial, request)
    with pytest.raises(RenewalInProgress):
        restarted.renew(NODE_ID, issued.serial, request)
    clock.advance(seconds=301)
    with pytest.raises(RenewalIssuanceUncertain):
        restarted.renew(NODE_ID, issued.serial, request)
```

- [x] **Step 2: Run the named service tests and verify RED**

Run: `uv run --project control pytest control/tests/test_enrollment.py -k 'renewal and (manual or process or persistence or simultaneous)' -v`

Expected: failures show no rotation model/state, rollback removes all evidence, and retries call the authority again.

- [x] **Step 3: Implement the durable intent state machine**

```python
class AgentCertificateRotation(Base):
    __tablename__ = "agent_certificate_rotations"
    node_id = mapped_column(ForeignKey("agent_nodes.node_id"), primary_key=True)
    source_serial = mapped_column(String(128), nullable=False)
    generation = mapped_column(Integer, nullable=False)
    csr_pem = mapped_column(Text, nullable=False)
    csr_public_key_fingerprint = mapped_column(String(64), nullable=False)
    provider_request_id = mapped_column(String(64), unique=True, nullable=False)
    state = mapped_column(String(32), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False)
```

Split renewal into a committed `_claim_rotation` transaction, one owner-only
provider call carrying `provider_request_id`, and a `_persist_rotation` transaction.
Catch ordinary provider/persistence errors, mark the exact intent
`manual-recovery` in a new transaction, and raise a bounded terminal exception.
Let `BaseException` simulate process death so the committed `issuing` row survives.
Existing exact issuing rows return in-progress until the five-minute boundary,
then atomically become manual; they never invoke the authority.

- [x] **Step 4: Add API and provider-request-ID RED tests**

```python
assert client.post("/agent/v1/renew", ...).status_code == 503
assert client.post("/agent/v1/renew", ...).content == canonical_message(response.json())
assert jwt.decode(seen_token, options={"verify_signature": False})["jti"] == request_id
```

Run: `uv run --project control pytest control/tests/test_agent_api.py control/tests/test_step_ca.py -k 'renew or rotation or request_id' -v`

Expected: the API maps all enrollment denials to 403 and Step CA generates a random `jti` internally.

- [x] **Step 5: Implement typed API mapping and persisted Step CA jti**

Map `RenewalInProgress` to canonical 503, leave manual recovery terminal at 403,
add a required keyword-only provider request ID to renewal, and use that exact
value as the Step sign JWT `jti`. Built-in issuance validates/accepts the same
boundary while relying on the control-plane intent for call uniqueness.

- [x] **Step 6: Verify service/API/provider GREEN and concurrency**

Run the focused service/API/Step tests, then the SQLite and PostgreSQL rotation
tests. Assert concurrent followers never increment the renewal provider-call
count, and a later exact replay returns the one staged certificate.

### Task 2: Downgrade-safe staged certificate denial

**Files:**
- Modify: `control/tests/test_agent_migrations.py`
- Modify: `control/migrations/versions/0005_certificate_rotation.py`

**Interfaces:**
- Consumes: 0005 certificate `state` and 0004's `revoked_at IS NULL` authentication semantics.
- Produces: downgrade data transformation that makes every non-active certificate denied under 0004.

- [x] **Step 1: Write the failing staged downgrade test**

```python
connection.execute(text("INSERT INTO agent_certificates (..., state, generation, ...) VALUES (..., 'staged', 2, ...)"))
downgrade_to("0004_agent_enrollment", database)
accepted = connection.execute(text("SELECT serial FROM agent_certificates WHERE serial=:serial AND revoked_at IS NULL"), {"serial": "staged-serial"}).scalar_one_or_none()
assert accepted is None
```

- [x] **Step 2: Run migration RED**

Run: `uv run --project control pytest control/tests/test_agent_migrations.py -k certificate_rotation -v`

Expected: the staged row survives with `revoked_at NULL` and is accepted by the literal prior predicate.

- [x] **Step 3: Implement safe downgrade and rotation-table migration**

Before dropping `state`, execute
`UPDATE agent_certificates SET revoked_at = CURRENT_TIMESTAMP WHERE state <> 'active' AND revoked_at IS NULL`.
Create the intent table during upgrade and drop it during downgrade in an order
that preserves foreign keys and model/head parity.

- [x] **Step 4: Run reversible migration and parity GREEN**

Run: `uv run --project control pytest control/tests/test_agent_migrations.py -v`

Expected: all migration tests pass, staged denial survives downgrade, re-upgrade succeeds, and Alembic metadata comparison is empty.

### Task 3: Expired active exact-fence failure recovery

**Files:**
- Modify: `agent/tests/test_lifecycle.py`
- Modify: `agent/src/vonk_agent/operations.py`

**Interfaces:**
- Consumes: `AgentStateStore.begin/finish/recover_pending/acknowledge` and `Agent.run_once` pending-first ordering.
- Produces: canonical failed `AgentResult` with `error_code="claim_deadline_expired"` and the original fence.

- [x] **Step 1: Write the failing restart lifecycle test**

```python
state.begin(expired_claim)
control.result_failures = 1
with pytest.raises(AgentTransportError):
    Agent(control, OperationRegistry(), context).run_once()
pending = state.recover_pending()
assert pending.result.result == {"status": "failed", "error_code": "claim_deadline_expired"}
assert pending.result.fence == expired_claim.fence
```

Continue the test with a restarted control containing a new claim: first run
replays and acknowledges the expired result without claiming; the following run
claims and completes the new fence.

- [x] **Step 2: Run lifecycle RED**

Run: `uv run --project agent pytest agent/tests/test_lifecycle.py -k expired_active -v`

Expected: `MonotonicDeadline.bind` raises `AgentProtocolError`, no terminal result is stored, and the lifecycle cannot progress.

- [x] **Step 3: Persist the bounded deadline result**

After exact completion inspection but before retry execution, catch deadline
binding failure, call `state.begin(claim)` idempotently, build the literal failed
result with every original protocol identity field, and call `state.finish`.
Return its canonical bytes as a replay when the claim was already active.

- [x] **Step 4: Run lifecycle and operations GREEN**

Run: `uv run --project agent pytest agent/tests/test_lifecycle.py agent/tests/test_operations.py -v`

Expected: the expired exact fence is durably replayed through acknowledgment and
only then may the queued fresh claim execute.

### Task 4: Report, full verification, and commit

**Files:**
- Modify: `.superpowers/sdd/2026-08-03-node-agent-runtime/task-4-report.md`
- Modify: `docs/superpowers/plans/2026-08-04-task4-fix-round1.md`

**Interfaces:**
- Consumes: final command output and RED/GREEN observations.
- Produces: a fix-round report section, completed plan, and local commit(s), without staging the controller ledger.

- [x] Run focused Task 4, full agent, protocol, full control, migration, Compose ingress/config, Ruff, compileall, build, fresh-wheel smoke, supply-chain verification, and `git diff --check`.
- [x] Append exact RED/GREEN and final gate results to the Task 4 report; do not edit the progress ledger.
- [x] Mark plan tasks complete, stage only owned files, commit locally, and verify `git status --short` contains only the controller-owned progress modification.
