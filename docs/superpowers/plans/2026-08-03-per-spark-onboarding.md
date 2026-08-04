# Per-Spark Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resumable, idempotent installation mode that safely onboards one newly added DGX Spark without source edits or predetermined names and addresses.

**Architecture:** `sparkctl install node` drives a local journal and a sequence of typed steps. Bootstrap SSH transfers and invokes a pinned Ansible Runner project locally on the one target; versioned roles apply idempotent host policy and emit JSON evidence without cluster identity constants. Git proposal output is canonical and separate from live mutation.

**Post-plan integration note (2026-08-04):** For a fresh or reimaged Spark,
the later outbound-agent migration phase adds a second bootstrap mode that
generates a DGX-Forge seed overlay for NVIDIA's documented
BaseOS/FastOS/cloud-init/OEMDATA workflow. This completed SSH-based mode remains
the path for an already-running node. Both converge on the same physical
identity gate, journal, agent enrollment, and canonical Git proposal; neither
introduces fixed names, addresses, or a fleet-size limit.

**Tech Stack:** Python 3.12, argparse, dataclasses, OpenSSH transport interface, Ansible Runner and roles, Bash wrappers, JSON, pytest, ansible-lint/ShellCheck where available.

## Global Constraints

- Consume Phase 0's generic fleet, installation, and job contracts.
- Install exactly one explicit target per invocation; never continue to another node after failure.
- Never accept changed host keys interactively, disable strict checking, forward an SSH agent, or transfer a private key.
- Identity repair that lacks a trusted remote anchor pauses for physical/out-of-band console action.
- Access-affecting changes require a recovery channel and a fresh positive and negative session proof.
- Every remote mutation is idempotent, checksum-verified, journaled, and resumable.
- Fabric joining is a separate reviewed operation; onboarding only records capabilities and proposes topology.
- Do not modify the active roadmap agent's transport implementation; depend on its public selection interface after it lands or inject a test transport.

---

### Task 1: Add install workspace and journal persistence

**Files:**
- Create: `src/spark_profiles/install/__init__.py`
- Create: `src/spark_profiles/install/store.py`
- Test: `tests/spark_profiles/install/test_store.py`

**Interfaces:**
- Produces: `InstallStore(root: Path)`, `create(request) -> InstallationJournal`, `load(node_id)`, and `save(journal, expected_revision) -> int`.

- [ ] **Step 1: Write failing atomicity and optimistic-lock tests**

```python
def test_store_round_trips_without_secret_material(tmp_path, request):
    store = InstallStore(tmp_path)
    journal = store.create(request)
    raw = next(tmp_path.glob("*.json")).read_text()
    assert store.load(request.node_id) == journal
    assert "PRIVATE KEY" not in raw


def test_store_rejects_stale_revision(tmp_path, request):
    store = InstallStore(tmp_path)
    journal = store.create(request)
    store.save(journal.mark_waiting("console identity repair"), expected_revision=0)
    with pytest.raises(InstallConflict):
        store.save(journal, expected_revision=0)
```

- [ ] **Step 2: Run and verify missing store**

Run: `uv run pytest tests/spark_profiles/install/test_store.py -v`
Expected: FAIL with missing package.

- [ ] **Step 3: Implement atomic restrictive persistence**

Write canonical JSON to a same-directory temporary file with mode `0600`, `fsync` file and directory, then `os.replace`. Store revision, public request fields, state, steps, evidence digests, and redacted errors. Reject symlink roots and files, mismatched node IDs, and stale revisions.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/spark_profiles/install/test_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit store**

```bash
git add src/spark_profiles/install tests/spark_profiles/install/test_store.py
git commit -m "feat: persist Spark installation journals"
```

### Task 2: Define injectable remote installation boundary

**Files:**
- Create: `src/spark_profiles/install/remote.py`
- Test: `tests/spark_profiles/install/test_remote.py`

**Interfaces:**
- Produces: `InstallTransport` protocol with `run(endpoint, argv, stdin, timeout) -> RemoteResult` and `copy(endpoint, source, destination, mode) -> RemoteResult`.
- Produces: `RemoteResult(returncode: int, stdout: bytes, stderr: bytes)` with bounded output.

- [ ] **Step 1: Write failing safety tests**

```python
def test_remote_command_pins_noninteractive_safe_options(fake_exec, endpoint):
    transport = OpenSshInstallTransport(exec=fake_exec, ssh_bin="ssh", scp_bin="scp")
    transport.run(endpoint, ("hostname",), b"", timeout=10)
    argv = fake_exec.calls[0]
    assert "BatchMode=yes" in argv
    assert "ForwardAgent=no" in argv
    assert "StrictHostKeyChecking=yes" in argv


def test_remote_boundary_rejects_password_and_private_key_arguments(endpoint):
    with pytest.raises(UnsafeInstallArgument):
        OpenSshInstallTransport().run(endpoint, ("--password=secret",), b"", 10)
```

- [ ] **Step 2: Run and observe missing boundary**

Run: `uv run pytest tests/spark_profiles/install/test_remote.py -v`
Expected: FAIL because `InstallTransport` is absent.

- [ ] **Step 3: Implement protocol and OpenSSH adapter**

Build argv arrays without a local shell, pin batch mode, no forwarding, strict known-host checking, explicit port/user/host, connection timeout, and output bounds. Accept credential references only through environment/provider integration, never command arguments. Keep binary selection injectable so Phase 2 can wire the landed shared transport selector.

- [ ] **Step 4: Run focused safety tests**

Run: `uv run pytest tests/spark_profiles/install/test_remote.py -v`
Expected: PASS.

- [ ] **Step 5: Commit boundary**

```bash
git add src/spark_profiles/install/remote.py tests/spark_profiles/install/test_remote.py
git commit -m "feat: add safe onboarding transport boundary"
```

### Task 3: Implement identity-gate inspection

**Files:**
- Create: `nodes/bin/inspect-node-identity`
- Create: `src/spark_profiles/install/identity.py`
- Test: `tests/nodes/test_inspect_node_identity.py`
- Test: `tests/spark_profiles/install/test_identity.py`

**Interfaces:**
- Remote script emits JSON fields `product_serial_sha256`, `machine_id_sha256`, `host_key_fingerprints`, and `requires_console_repair`.
- Produces: `evaluate_identity(observation, trusted_assertion) -> IdentityDecision`.

- [ ] **Step 1: Write failing script and policy tests**

```python
def test_identity_probe_emits_hashes_not_raw_machine_identity(run_probe):
    result = run_probe(serial="SERIAL-SECRET", machine_id="a" * 32)
    assert result["product_serial_sha256"] == sha256(b"SERIAL-SECRET").hexdigest()
    assert "SERIAL-SECRET" not in json.dumps(result)


def test_unanchored_first_contact_requires_console_repair(observation):
    decision = evaluate_identity(observation, trusted_assertion=None)
    assert decision.action == "wait-for-console"
```

- [ ] **Step 2: Run and verify failures**

Run: `uv run pytest tests/nodes/test_inspect_node_identity.py tests/spark_profiles/install/test_identity.py -v`
Expected: FAIL because script/module is absent.

- [ ] **Step 3: Implement read-only probe and decision policy**

The Bash probe reads DMI serial, machine ID, configured public host keys, and `sshd -T`; it hashes sensitive identifiers locally and prints one JSON object. Python requires an administrator assertion matching the physically observed serial digest and all expected host-key fingerprints, or pauses with console repair instructions. Duplicate known machine/host identities quarantine both records.

- [ ] **Step 4: Run tests and ShellCheck**

Run: `uv run pytest tests/nodes/test_inspect_node_identity.py tests/spark_profiles/install/test_identity.py -v && shellcheck nodes/bin/inspect-node-identity`
Expected: PASS. If ShellCheck is unavailable, record that fact and run `bash -n nodes/bin/inspect-node-identity`.

- [ ] **Step 5: Commit identity gate**

```bash
git add nodes/bin/inspect-node-identity src/spark_profiles/install/identity.py tests
git commit -m "security: gate Spark onboarding on trusted identity"
```

### Task 4: Parameterize and version node policy installers

**Files:**
- Create: `deploy/ansible/project/`
- Create: `deploy/ansible/roles/dgx_spark/`
- Create: `nodes/bin/apply-node-policy`
- Create: `nodes/policy/default.json`
- Modify: `nodes/bin/install-ssh-hardening`
- Test: `tests/nodes/test_apply_node_policy.py`
- Modify: `tests/runbooks/test_ssh_hardening.sh`

**Interfaces:**
- `apply-node-policy --policy FILE --check|--apply|--verify` invokes a pinned local Ansible Runner project and emits JSON plus the role/policy SHA-256.
- `install-ssh-hardening` accepts `--admin-user USER`, `--drop-in FILE`, and `--check|--apply|--verify|--rollback`; no compiled user name.

- [ ] **Step 1: Write failing parameterization and idempotency tests**

```python
def test_hardening_has_no_compiled_user(repository_root):
    script = (repository_root / "nodes/bin/install-ssh-hardening").read_text()
    assert "readonly admin_user='carst'" not in script
    assert "--admin-user" in script


def test_policy_apply_twice_reports_unchanged(policy_host):
    first = policy_host.run("--apply")
    second = policy_host.run("--apply")
    assert first["status"] == "changed"
    assert second["status"] == "unchanged"
    assert first["policy_sha256"] == second["policy_sha256"]
```

- [ ] **Step 2: Run and verify the fixed user and absent policy script fail tests**

Run: `uv run pytest tests/nodes/test_apply_node_policy.py -v && bash tests/runbooks/test_ssh_hardening.sh`
Expected: FAIL on the current fixed `carst` constant and missing policy script.

- [ ] **Step 3: Implement explicit policy inputs and transactions**

Implement the policy as focused idempotent Ansible roles with check-mode and
handlers. Validate user names with the platform account database and a
conservative syntax; stage SSH drop-ins; run `sshd -t`; require a recovery
marker supplied by the orchestrator before reload; retain the previous managed
file for rollback; parameterize early-OOM policy; and emit changed paths and
role/policy digests without secrets. Reject unknown policy keys. The wrapper
uses fixed Runner inputs and cannot select a repository playbook or arbitrary
module. Do not enable periodic `ansible-pull`.

- [ ] **Step 4: Run node tests**

Run: `uv run pytest tests/nodes/test_apply_node_policy.py -v && ansible-lint deploy/ansible && bash tests/runbooks/test_ssh_hardening.sh && bash -n nodes/bin/apply-node-policy nodes/bin/install-ssh-hardening`
Expected: PASS.

- [ ] **Step 5: Commit installers**

```bash
git add deploy/ansible nodes/bin nodes/policy tests/nodes/test_apply_node_policy.py tests/runbooks/test_ssh_hardening.sh
git commit -m "feat: parameterize idempotent Spark node policy"
```

### Task 5: Build the resumable onboarding orchestrator

**Files:**
- Create: `src/spark_profiles/install/orchestrator.py`
- Test: `tests/spark_profiles/install/test_orchestrator.py`

**Interfaces:**
- Produces: `NodeInstaller(store, transport, evidence_store, clock).run(node_id, *, until=None) -> InstallationJournal`.
- Step handlers: identity, pre-inventory, public-key, SSH hardening, node policy, post-inventory, acceptance.

- [ ] **Step 1: Write failing order, resume, and hard-stop tests**

```python
def test_installer_executes_one_node_in_declared_order(installer, request):
    journal = installer.start(request)
    completed = installer.run(journal.request.node_id)
    assert [step.name for step in completed.steps] == [
        "identity", "pre-inventory", "public-key", "ssh-hardening",
        "node-policy", "post-inventory", "acceptance",
    ]


def test_installer_resumes_after_completed_steps(installer_with_failure, request):
    journal = installer_with_failure.start(request)
    failed = installer_with_failure.run(journal.request.node_id)
    resumed = installer_with_failure.with_failure_removed().run(journal.request.node_id)
    assert resumed.steps[0].attempts == 1
    assert resumed.state == "accepted"


def test_failure_never_touches_another_node(two_node_installer):
    two_node_installer.fail_target("alpha")
    two_node_installer.run(two_node_installer.alpha.id)
    assert two_node_installer.calls_for(two_node_installer.beta.id) == []
```

- [ ] **Step 2: Run and verify orchestrator is absent**

Run: `uv run pytest tests/spark_profiles/install/test_orchestrator.py -v`
Expected: FAIL with missing module.

- [ ] **Step 3: Implement explicit step registry and evidence checks**

Each handler verifies the previous state, runs only its target, writes bounded stdout/stderr to an evidence file, hashes it, advances the journal atomically, and returns. Waiting-for-console is not a failure and requires an explicit resume assertion. Failed steps store a redacted reason and can retry only when declared retry-safe or after a verification handler establishes remote state.

- [ ] **Step 4: Run install package tests**

Run: `uv run pytest tests/spark_profiles/install -v`
Expected: PASS.

- [ ] **Step 5: Commit orchestrator**

```bash
git add src/spark_profiles/install/orchestrator.py tests/spark_profiles/install/test_orchestrator.py
git commit -m "feat: orchestrate resumable Spark onboarding"
```

### Task 6: Expose onboarding through the CLI

**Files:**
- Create: `src/spark_profiles/install/cli.py`
- Create: `bin/spark-install`
- Test: `tests/spark_profiles/install/test_cli.py`
- Modify: `README.md`

**Interfaces:**
- Commands: `spark-install node start`, `status`, `resume`, `verify`, and `emit-record`.
- `start` requires `--host`, `--user`, `--credential-ref`, `--display-name`; optional `--port` and repeated `--label KEY=VALUE`.
- All commands support `--json`; mutating commands require `--apply`, otherwise they print a plan.

- [ ] **Step 1: Write failing CLI contract tests**

```python
def test_start_defaults_to_non_mutating_plan(run_cli):
    result = run_cli("node", "start", "--host", "spark.local", "--user", "admin",
                     "--credential-ref", "secret://ssh/admin", "--display-name", "alpha", "--json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["mode"] == "plan"
    assert result.remote_calls == []


def test_cli_has_no_name_or_address_defaults(run_cli):
    result = run_cli("node", "start", "--apply")
    assert result.returncode == 2
    assert "--host" in result.stderr and "--display-name" in result.stderr
```

- [ ] **Step 2: Run and confirm launcher is absent**

Run: `uv run pytest tests/spark_profiles/install/test_cli.py -v`
Expected: FAIL because `bin/spark-install` is absent.

- [ ] **Step 3: Implement parser and dependency injection**

Keep parsing and production dependency construction separate. Generate node IDs with `secrets.token_hex(16)`, validate labels, render redacted JSON, default every start/resume to a plan, and call `NodeInstaller` only with `--apply`. `emit-record` writes canonical TOML to stdout and never modifies Git.

- [ ] **Step 4: Run CLI tests and help smoke test**

Run: `uv run pytest tests/spark_profiles/install/test_cli.py -v && bin/spark-install --help`
Expected: PASS and help lists node commands.

- [ ] **Step 5: Commit CLI**

```bash
git add bin/spark-install src/spark_profiles/install/cli.py tests/spark_profiles/install/test_cli.py README.md
git commit -m "feat: add per-Spark installation mode"
```

### Task 7: Emit canonical Git proposal and document operation

**Files:**
- Create: `src/spark_profiles/install/proposal.py`
- Create: `docs/runbooks/node-onboarding.md`
- Test: `tests/spark_profiles/install/test_proposal.py`
- Test: `tests/runbooks/test_node_onboarding.py`

**Interfaces:**
- Produces: `build_node_proposal(fleet, accepted_journal, observations) -> RepositoryProposal`.
- Proposal contains base commit, target path, canonical bytes, SHA-256, and no Git mutation.

- [ ] **Step 1: Write failing deterministic proposal tests**

```python
def test_proposal_is_deterministic_and_sanitized(accepted_install):
    first = build_node_proposal(*accepted_install)
    second = build_node_proposal(*accepted_install)
    assert first.content == second.content
    assert first.sha256 == second.sha256
    assert b"PRIVATE KEY" not in first.content
    assert b"credential_ref" not in first.content


def test_unaccepted_install_cannot_emit_proposal(failed_install):
    with pytest.raises(ProposalError, match="accepted"):
        build_node_proposal(*failed_install)
```

- [ ] **Step 2: Run and observe missing proposal module**

Run: `uv run pytest tests/spark_profiles/install/test_proposal.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement canonical serializer and runbook**

Sort nodes by ID and labels by key, normalize line endings, include schema and policy versions, exclude credentials/raw serials/job logs, and target `inventory/fleet.toml`. Document trusted first contact, console pause, dry-run, apply, resume, record review, Git commit, topology separation, and recovery.

- [ ] **Step 4: Run Phase 1 and full tests**

Run: `uv run pytest tests/spark_profiles/install tests/nodes tests/runbooks/test_node_onboarding.py -v && uv run pytest -q && git diff --check`
Expected: all tests PASS with the existing skip unchanged.

- [ ] **Step 5: Commit proposal and runbook**

```bash
git add src/spark_profiles/install/proposal.py tests/spark_profiles/install/test_proposal.py docs/runbooks/node-onboarding.md tests/runbooks/test_node_onboarding.py
git commit -m "feat: propose repository-backed Spark records"
```
