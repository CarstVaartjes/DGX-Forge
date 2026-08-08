# GPU node Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the non-root GPU node agent, persistent fenced executor, outbound mTLS client, and stable A/B supervisor.

**Architecture:** A small Python package separates transport, persistent state, and a closed operation registry. A minimal supervisor owns slot selection and rollback; the replaceable agent never overwrites the active executable.

**Tech Stack:** Python 3.12, `python-tuf`/ngclient, pinned ORAS CLI, systemd, SQLite, POSIX filesystem primitives, pytest

## Global Constraints

- The agent initiates all routine connections and opens no listening socket.
- The network protocol cannot invoke shell, arbitrary commands, paths, environment variables, or uninstalled adapters.
- Agent state and private keys use restrictive ownership, no symlink traversal, atomic writes, and bounded files.
- Operations are idempotent or define explicit inspect/compensate behavior before retry.
- Agent installation supports ARM64 Vonk Forge OS and does not depend on Docker membership or root during normal execution.

---

### Task 1: Agent configuration, identity, and persistent state

**Files:**
- Create: `agent/pyproject.toml`
- Create: `agent/src/vonk_agent/config.py`
- Create: `agent/src/vonk_agent/state.py`
- Test: `agent/tests/test_config.py`
- Test: `agent/tests/test_state.py`

**Interfaces:**
- Produces `AgentConfig.load(path)`, `AgentStateStore.begin(claim)`, `heartbeat`, `finish`, `recover_active`.
- State root defaults to `/var/lib/vonk-forge-agent`; configuration defaults to `/etc/vonk-forge-agent/config.json`.

- [ ] **Step 1: Write failing path and restart tests**

```python
def test_state_survives_restart_and_rejects_new_fence(tmp_path) -> None:
    first = AgentStateStore(tmp_path).begin(claim(fence="fence-a"))
    reopened = AgentStateStore(tmp_path)
    assert reopened.recover_active().fence == "fence-a"
    with pytest.raises(AgentStateConflict):
        reopened.begin(claim(fence="fence-b"))
```

- [ ] **Step 2: Run and observe missing package**

Run: `uv run --project agent pytest agent/tests/test_config.py agent/tests/test_state.py -v`
Expected: FAIL because `agent/pyproject.toml` and package are absent.

- [ ] **Step 3: Implement strict configuration and SQLite state**

Configuration accepts only control URL, node ID, certificate/key/CA paths,
poll bounds, state root, and installed-policy path. Require absolute regular
non-symlink credential paths and HTTPS. SQLite uses WAL, restrictive mode,
transactions, one active attempt, canonical claim/result bytes, and monotonic
progress sequence numbers. Never persist enrollment token after success.

- [ ] **Step 4: Run state tests under interruption simulation**

Run: `uv run --project agent pytest agent/tests/test_config.py agent/tests/test_state.py -v`
Expected: PASS including reopen after an interrupted transaction.

- [ ] **Step 5: Commit state foundation**

```bash
git add agent/pyproject.toml agent/src/vonk_agent/config.py agent/src/vonk_agent/state.py agent/tests/test_config.py agent/tests/test_state.py
git commit -m "feat: persist fenced GPU node agent state"
```

### Task 2: Closed operation registry and node probes

**Files:**
- Create: `agent/src/vonk_agent/operations.py`
- Create: `agent/src/vonk_agent/probe.py`
- Create: `agent/src/vonk_agent/nvidia_tools.py`
- Modify: `nodes/bin/collect-health`
- Test: `agent/tests/test_operations.py`
- Test: `agent/tests/test_probe.py`
- Test: `agent/tests/test_nvidia_tools.py`

**Interfaces:**
- Produces `OperationRegistry.execute(claim, context) -> Mapping`, `inspect(claim, context) -> OperationInspection`.
- `OperationContext` exposes fixed installed roots and typed adapter interfaces, not subprocess or shell callbacks.

- [ ] **Step 1: Write failing closed-registry tests**

```python
def test_unknown_or_command_payload_never_reaches_executor(registry) -> None:
    with pytest.raises(UnsupportedOperation):
        registry.execute(claim(operation="system.exec"), context())
    with pytest.raises(AgentProtocolError):
        registry.execute(claim(payload={"command": ["id"]}), context())
```

- [ ] **Step 2: Run and observe missing registry**

Run: `uv run --project agent pytest agent/tests/test_operations.py agent/tests/test_probe.py -v`
Expected: FAIL importing operations.

- [ ] **Step 3: Implement typed registry and pinned-tool adapter**

Map enum members to concrete handler objects. `node.probe` collects existing
health evidence through a fixed installed collector with no payload arguments,
a 15-second deadline, 256-KiB output limit, fixed environment, and no shell.
Add a fixed-path adapter for the pinned NVIDIA DGX Spark Enterprise
Manageability bundle. The installed policy fixes bundle digest/version,
executable paths, exact argument vectors, per-tool deadlines, and output
limits; the claim cannot select a tool or arguments. Validate the NVIDIA JSON
envelope as untrusted input and normalize safe output from device identity,
hardware, firmware, OS, drivers, `vendor_diagctl health`, and reset reason.
Drop process lists, raw serials, addresses, log lines, artifact paths, and
unknown fields. Combine these platform fields with the existing
Vonk Forge-specific fabric/runtime collector. Missing or incompatible pinned
tools is explicit degraded/unsupported evidence, never silent execution of a
PATH-selected alternative. Normalize/redact before returning. Implement
inspection so a completed probe can be replayed without rerunning collection.

- [ ] **Step 4: Run operation tests and existing health collector tests**

Run: `uv run --project agent pytest agent/tests/test_operations.py agent/tests/test_probe.py agent/tests/test_nvidia_tools.py -v && uv run pytest tests/nodes/test_collect_health.py -q`
Expected: PASS.

- [ ] **Step 5: Commit registry**

```bash
git add agent/src/vonk_agent/operations.py agent/src/vonk_agent/probe.py agent/src/vonk_agent/nvidia_tools.py agent/tests/test_operations.py agent/tests/test_probe.py agent/tests/test_nvidia_tools.py
git commit -m "feat: execute typed GPU node agent operations"
```

### Task 3: Content-addressed release and workload handlers

**Files:**
- Create: `agent/src/vonk_agent/releases.py`
- Create: `agent/src/vonk_agent/workloads.py`
- Create: `agent/src/vonk_agent/oci.py`
- Create: `agent/src/vonk_agent/update_trust.py`
- Modify: `deploy/compose/compose.yaml`
- Create: `deploy/compose/registry/config.yml`
- Test: `agent/tests/test_releases.py`
- Test: `agent/tests/test_workloads.py`

**Interfaces:**
- `ReleaseInstaller.install(ReleaseRequest) -> ReleaseEvidence` accepts a TUF target name, exact OCI manifest digest, target/provenance digests, and adapter ID; it verifies the target through locally persisted TUF trust before pulling content.
- `WorkloadOperations` accepts adapter ID, release digest, operation-specific typed fields, and deadline.

- [ ] **Step 1: Write failing digest/path/policy tests**

Test wrong digest, expired/rollback/freeze/mix-and-match TUF metadata, invalid
root rotation, archive traversal, symlink entries,
unexpected file modes, unapproved adapter, release mismatch, and attempts to
supply command/path/environment fields.

- [ ] **Step 2: Run and observe missing handlers**

Run: `uv run --project agent pytest agent/tests/test_releases.py agent/tests/test_workloads.py -v`
Expected: FAIL importing modules.

- [ ] **Step 3: Implement immutable installation and fixed adapter dispatch**

Deploy a pinned CNCF Distribution registry as a separate private service.
Use the maintained TUF client against a fixed HTTPS metadata base on the
configured control origin, seeded by a root installed with the agent. Persist
trusted-root and highest accepted metadata versions atomically in agent state.
The claim selects only an allowlisted target name and must agree with the
TUF-authorized OCI manifest digest.
Resolve no tags: invoke a pinned root-owned ORAS client through fixed argv to
pull only the registry origin and `@sha256` manifest supplied by the control
plane. Credentials come from an absolute restrictive file, never the payload.
Verify the TUF-authorized target digest, complete member allowlist, modes, and ownership;
install atomically beneath `/opt/vonk-forge/releases/<digest>`. Resolve workload
operations only through the installed adapter manifest and compiled adapter
runner. Never accept a repository command array over the network.
Retain `/agent/v1/artifacts/<sha256>` only for explicitly size-bounded
bootstrap/recovery artifacts; normal releases must not traverse that endpoint.

- [ ] **Step 4: Verify idempotency and interrupted installs**

Run: `uv run --project agent pytest agent/tests/test_releases.py agent/tests/test_workloads.py -v`
Expected: PASS; identical release is a no-op and partial temp trees never become active.

- [ ] **Step 5: Commit handlers**

```bash
git add agent/src/vonk_agent/releases.py agent/src/vonk_agent/workloads.py agent/src/vonk_agent/oci.py agent/src/vonk_agent/update_trust.py agent/tests/test_releases.py agent/tests/test_workloads.py deploy/compose/compose.yaml deploy/compose/registry/config.yml
git commit -m "feat: install and operate signed GPU node releases"
```

### Task 4: Outbound enrollment and long-poll client

**Files:**
- Create: `agent/src/vonk_agent/client.py`
- Create: `agent/src/vonk_agent/main.py`
- Test: `agent/tests/test_client.py`
- Test: `agent/tests/test_lifecycle.py`

**Interfaces:**
- Produces `AgentClient.enroll`, `claim`, `heartbeat`, `result`, `renew`; `Agent.run_once()` and `run_forever()`.
- Uses protocol contracts from Phase 1 and persistent executor from Tasks 1-3.

- [ ] **Step 1: Write failing end-to-end fake-server test**

```python
def test_agent_claims_executes_and_reports_with_same_fence(fake_control, agent) -> None:
    fake_control.queue(probe_claim())
    agent.run_once()
    assert fake_control.results[0]["fence"] == probe_claim()["fence"]
    assert fake_control.results[0]["state"] == "succeeded"
```

- [ ] **Step 2: Run and observe missing client**

Run: `uv run --project agent pytest agent/tests/test_client.py agent/tests/test_lifecycle.py -v`
Expected: FAIL importing client.

- [ ] **Step 3: Implement mTLS requests and resilient loop**

Use `http.client.HTTPSConnection` with an `ssl.SSLContext` loading the private
CA and node certificate/key. Bound connect/read timeouts and bodies. Verify
content type and canonical JSON. Back off with bounded jitter on transport
failure, but never retry terminal submission under a different fence. Renew at
one-third remaining lifetime. On restart, inspect persisted active work before
claiming anything new.

- [ ] **Step 4: Run lifecycle, disconnect, and renewal tests**

Run: `uv run --project agent pytest agent/tests/test_client.py agent/tests/test_lifecycle.py -v`
Expected: PASS including server restart, expired grant, revoked cert, and stale result responses.

- [ ] **Step 5: Commit client**

```bash
git add agent/src/vonk_agent/client.py agent/src/vonk_agent/main.py agent/tests/test_client.py agent/tests/test_lifecycle.py
git commit -m "feat: poll control plane from GPU node agent"
```

### Task 5: Stable A/B supervisor and systemd packaging

**Files:**
- Create: `agent/supervisor/vonk-agent-supervisor`
- Create: `agent/systemd/vonk-forge-agent.service`
- Create: `agent/systemd/vonk-forge-agent-supervisor.service`
- Create: `nodes/bin/install-vonk-agent`
- Create: `nodes/vendor/nvidia-manageability.lock.json`
- Test: `agent/tests/test_supervisor.py`
- Test: `tests/nodes/test_install_vonk_agent.py`

**Interfaces:**
- Supervisor state contains `active_slot`, `previous_slot`, expected digest, activation deadline, and boot attempts.
- Installer consumes explicit pinned bundle, config, CA, node ID, and one-time enrollment token paths.

- [ ] **Step 1: Write failing activation/rollback tests**

Test successful A->B activation, missing executable, digest mismatch, process
exit, missed reconnect marker, rollback to A, both slots invalid, and symlink
targets. Test installer idempotency and no private admin key copy.
Test the exact NVIDIA bundle digest, license/provenance retention, and fixed
installed tool paths as part of the same installer boundary.

- [ ] **Step 2: Run and observe missing supervisor/installer**

Run: `uv run --project agent pytest agent/tests/test_supervisor.py -v && uv run pytest tests/nodes/test_install_vonk_agent.py -v`
Expected: FAIL because artifacts are absent.

- [ ] **Step 3: Implement slot state machine and restrictive units**

Use atomic JSON state, SHA-256 verification, `renameat2`/no-replace semantics,
and a fixed slot root. Supervisor runs as root only to select/launch slots; the
agent service runs dedicated user `vonk-agent` with `NoNewPrivileges`, strict
filesystem protections, bounded restart, no Docker socket, and explicit
writable state paths. Roll back when the new slot misses its readiness marker.
Install the reviewed NVIDIA Enterprise Manageability bundle as an immutable
TUF-authorized/OCI-transported dependency beneath a digest directory, retain
its MIT license and source provenance, and generate the fixed installed policy
consumed by `nvidia_tools.py`. Never fetch its mutable web ZIP during node
installation.

- [ ] **Step 4: Run packaging and systemd security checks**

Run: `uv run --project agent pytest agent/tests -q && uv run pytest tests/nodes/test_install_vonk_agent.py -q && systemd-analyze security agent/systemd/vonk-forge-agent.service`
Expected: tests pass; review and record any unavailable sandbox directive on target Vonk Forge OS.

- [ ] **Step 5: Commit supervisor/install**

```bash
git add agent/supervisor agent/systemd nodes/bin/install-vonk-agent nodes/vendor/nvidia-manageability.lock.json agent/tests/test_supervisor.py tests/nodes/test_install_vonk_agent.py
git commit -m "feat: supervise GPU node agents with A/B rollback"
```

### Task 6: Agent simulator and phase acceptance

**Files:**
- Create: `agent/src/vonk_agent/simulator.py`
- Create: `tests/agent/test_failure_matrix.py`
- Create: `scripts/accept-agent-lifecycle`

**Interfaces:**
- Simulator injects disconnect, crash, stale fence, bad artifact, bad certificate, and failed activation without shelling into a GPU node.

- [ ] **Step 1: Write parameterized failure test**

Cover one and sixteen agents; assert no duplicate mutation, no cross-node
claim, stale result rejection, reconnect recovery, and bad update rollback.

- [ ] **Step 2: Run and observe missing simulator**

Run: `uv run pytest tests/agent/test_failure_matrix.py -v`
Expected: FAIL importing simulator.

- [ ] **Step 3: Implement deterministic simulator and acceptance script**

Use seeded clocks and in-memory transport around real state/operation classes.
The script emits canonical JSON and labels all simulated evidence explicitly.

- [ ] **Step 4: Run Phase 3 verification**

Run: `uv run --project agent pytest agent/tests -q && uv run pytest tests/agent/test_failure_matrix.py tests/nodes/test_install_vonk_agent.py -q && scripts/accept-agent-lifecycle --nodes 16 --json && git diff --check`
Expected: all pass.

- [ ] **Step 5: Commit acceptance**

```bash
git add agent/src/vonk_agent/simulator.py tests/agent/test_failure_matrix.py scripts/accept-agent-lifecycle
git commit -m "test: accept outbound GPU node agent lifecycle"
```
