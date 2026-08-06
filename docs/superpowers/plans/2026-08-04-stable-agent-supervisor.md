# Stable Spark Agent Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install and run the outbound Spark agent through a crash-consistent stable A/B supervisor, strict installed runtime policy, and networkless per-node installer.

**Architecture:** A stable Python supervisor outside both slots validates and descriptor-executes the selected agent under a dedicated systemd service account, while a separate root unit owns only activation and rollback. A networkless Python installer publishes immutable digest roots and root-owned policies before mutable configuration, and production `build_agent` constructs the accepted TUF, ORAS, release, and workload handlers from those policies.

**Tech Stack:** Python 3.12 standard library, systemd, POSIX file descriptors/flock/fsync/rename, python-tuf 7.0.0, ORAS 1.3.3, pytest 8.4.2

## Global Constraints

- Work directly on `main`; do not push or open a pull request.
- The exact NVIDIA archive is `enterprise-lifecycle-integration-scripts-20260520-1602.zip`, version `0.1.0`, SHA-256 `0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3`.
- The slot ELF binds the complete Python/application/native-module closure and resolves no Python package outside itself; the supported DGX OS kernel/dynamic-loader/glibc ABI is a separate validated platform contract.
- The installer performs no network fetch and accepts one explicit canonical `spk_[0-9a-f]{32}` node ID plus absolute local inputs.
- Routine operation remains outbound mutual TLS as non-root `dgx-agent`; SSH is neither used nor disabled.
- No claim selects an origin, repository, path, executable, arguments, adapter, environment, trust root, or shell command.
- Supervisor/slot/policy/artifact roots are fixed and non-writable by `dgx-agent`; readiness is the only agent-writable supervisor input.
- State and policies are bounded canonical duplicate-free JSON and fail closed on missing, extra, corrupt, unsafe, or noncanonical content.
- Every production behavior begins with a test that is observed failing for the intended missing or wrong behavior.
- Preserve the later SSH/Ansible/cloud-init migration, recovery CLI, signed rollout, and network `agent.update` boundaries.

---

### Task 1: Stable supervisor state, launch, readiness, and rollback

**Files:**
- Create: `agent/supervisor/dgx-agent-supervisor`
- Create: `agent/tests/test_supervisor.py`

**Interfaces:**
- Consumes: fixed slot root `/opt/dgx-forge/agent-slots`, state root `/var/lib/dgx-forge-agent-supervisor`, runtime root `/run/dgx-forge-agent`, and fixed units `dgx-forge-agent.service` / `dgx-forge-agent-supervisor.service`.
- Produces: `initialize --slot A|B --sha256 HEX`, `activate --slot A|B --sha256 HEX`, `supervise`, and `run-agent`; all interfaces reject unknown options and arbitrary paths.
- State fields: `schema_version`, `generation`, `active_slot`, `previous_slot`, `slot_sha256`, `expected_sha256`, `activation_deadline`, `boot_attempts`, `status`, `rollback_performed`.

- [ ] **Step 1: Write the initial failing supervisor tests**

Add subprocess tests that use the unprivileged-only `DGX_SUPERVISOR_TEST_ROOT`
and `DGX_SUPERVISOR_SYSTEMCTL` hooks. The initial slice asserts:

```python
def test_initialize_and_run_agent_executes_only_verified_slot(tmp_path: Path) -> None:
    host = SupervisorHost(tmp_path)
    agent = host.compile_elf_agent("A", stdout="slot-a")
    initialized = host.run("initialize", "--slot", "A", "--sha256", sha256(agent))
    launched = host.run("run-agent")
    assert initialized.returncode == launched.returncode == 0
    assert launched.stdout == "slot-a\n"

def test_pending_b_requires_exact_generation_slot_digest_readiness(tmp_path: Path) -> None:
    host = initialized_host(tmp_path)
    digest_b = host.write_agent("B", PYTHON_AGENT_B)
    assert host.run("activate", "--slot", "B", "--sha256", digest_b).returncode == 0
    host.write_readiness(generation=1, slot="B", sha256=digest_b)
    assert host.run("supervise").returncode != 0
    assert host.read_state()["active_slot"] == "A"
```

Extend the same file with literal expected states for successful A-to-B
activation, missing executable, digest mismatch, process/unit failure, missed
deadline, stale/cross-generation marker, boot-attempt exhaustion, exactly-once
rollback, both slots invalid, corrupt/duplicate/extra/missing state, symlink,
hardlink, directory/FIFO/script/console-stub slot, architecture mismatch,
substitution after open,
concurrent activations, publication crash hooks, restart from each partial
state, descriptor counts, and fixed command rejection.

- [ ] **Step 2: Run the required RED and preserve its output**

Run:

```bash
uv run --project agent pytest agent/tests/test_supervisor.py -v
```

Expected: collection or subprocess assertions fail because
`agent/supervisor/dgx-agent-supervisor` does not exist.

- [ ] **Step 3: Implement strict state parsing and atomic publication**

Implement the script with constants for all production roots and units.
Unprivileged tests may relocate the fixed root; UID 0 must reject every test
override. Use `json.loads(..., object_pairs_hook=unique)`, compare serialized
canonical bytes (`sort_keys=True`, compact separators, trailing newline), cap
state at 16 KiB, and reject every field/type/value outside the interface.

Use directory-relative `O_NOFOLLOW|O_CLOEXEC` opens, exact root/current UID
ownership rules, exact `0644` state/lock modes, regular files, `st_nlink == 1`,
and `flock` shared/exclusive locking. Publish through a random
`.state.<hex>.new` opened `O_CREAT|O_EXCL`, full writes, `fsync(file)`,
`rename`, and `fsync(directory)`. A test-only crash hook may terminate after
create, write, file-fsync, rename, and directory-fsync only when non-root.

- [ ] **Step 4: Run state tests GREEN**

Run:

```bash
uv run --project agent pytest agent/tests/test_supervisor.py -k 'state or initialize or concurrent or publication' -v
```

Expected: all selected tests pass with no leaked staging file except the exact
pre-rename crash artifact, which the next locked invocation validates by name
and inode and removes.

- [ ] **Step 5: Implement verified FD launch and activation coordination**

Open the selected `dgx-forge-agent` relative to its fixed slot descriptor with
`O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`; require root/current-test owner, mode `0555`,
regular type, one link, maximum 256 MiB, stable metadata before/after hash,
exact digest, ELF magic/class/endianness/type/machine, and the detected supported
architecture. Reject every script/shebang, including a console stub importing
`dgx_agent` from mutable global or site packages. Execute through
`/proc/self/fd/<fd>` with a deliberately built environment containing
only fixed locale/Python values and verified readiness generation/slot/digest.

Activation requires root in production, verifies inactive and previous slots,
publishes generation+1 pending state with the compiled 120-second deadline,
and invokes only `/usr/bin/systemctl restart dgx-forge-agent-supervisor.service`.
`supervise` increments attempts, restarts only the fixed agent unit, accepts
only the exact readiness marker, commits stable state on success, or durably
commits one rollback before restarting the verified previous slot. It exits
for recovery when the previous slot is invalid or rollback was already used.

- [ ] **Step 6: Run all supervisor tests GREEN**

Run:

```bash
uv run --project agent pytest agent/tests/test_supervisor.py -v
```

Expected: every state, process, race, readiness, and rollback case passes.

### Task 2: NVIDIA lock and networkless installer

**Files:**
- Create: `nodes/vendor/nvidia-manageability.lock.json`
- Create: `nodes/bin/install-dgx-agent`
- Create: `tests/nodes/test_install_dgx_agent.py`

**Interfaces:**
- Consumes: `--node-id`, `--agent-artifact`, `--agent-sha256`, `--oras`, `--oras-sha256`, `--nvidia-bundle`, `--health-collector`, `--health-collector-sha256`, `--site-config`, `--ca`, `--tuf-root`, `--tuf-root-sha256`, `--workload-tuf-root`, `--workload-tuf-root-sha256`, `--registry-auth`, `--update-authority`, `--package-grant-public`, `--package-receipt-public`, and `--enrollment-token`; every path is absolute. Platform and workload TUF roots, metadata caches, and target caches are separate installed paths. The installer publishes the grant key under both the agent and root-helper trust paths (`package-grant-public.pem` and `package-fence-public.pem`) and publishes the independent object-receipt key as `package-receipt-public.pem` before enabling the helper socket.
- Produces: immutable agent A, ORAS, NVIDIA, and collector digest roots; restrictive bootstrap/config/policy/state; installed stable supervisor/units; canonical JSON result with `schema_version`, `status`, `node_id`, `agent_sha256`, and `nvidia_sha256`.

- [ ] **Step 1: Write failing installer input and NVIDIA lifecycle tests**

Create real temporary inputs and invoke the script as a subprocess with the
unprivileged-only `DGX_INSTALL_TEST_ROOT`. Use a purpose-built locked NVIDIA ZIP
fixture whose member bytes have the literal reviewed sizes/digests and whose
archive bytes are supplied through a test-only lock path; production refuses
that override. Tests assert two distinct node IDs, second-install no-op,
concurrent installer serialization, exact destination ownership/modes, no
admin/CA/SSH/old-key copy, and retained `LICENSE`/canonical `SOURCE.json`.

Add parameterized negative cases for relative inputs, symlink/hardlink/FIFO/
device/oversized/wrong-owner/wrong-mode inputs, each digest mismatch, site and
TUF duplicate/extra/malformed JSON, wrong policy architecture, non-ELF ORAS,
non-ELF/thin-console-script agent, wrong ELF machine, substituted inode during
snapshot, ZIP traversal/absolute/
backslash escape/duplicate/extra/missing/encrypted/link/device/FIFO members,
wrong member size/mode/CRC/digest, cross-filesystem staging, publication crash
restart, and cleanup limited to the transaction's inode.

The production-archive contract uses literals:

```python
assert lock["filename"] == "enterprise-lifecycle-integration-scripts-20260520-1602.zip"
assert lock["version"] == "0.1.0"
assert lock["sha256"] == "0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3"
assert installed_policy["bundle_root"].endswith(lock["sha256"])
assert {tool["name"] for tool in installed_policy["tools"]} == set(NVIDIA_TOOL_NAMES)
```

- [ ] **Step 2: Run installer RED**

Run:

```bash
uv run pytest tests/nodes/test_install_dgx_agent.py -v
```

Expected: tests fail because the installer and lock do not exist.

- [ ] **Step 3: Commit the exact NVIDIA lock data**

Build the lock from the already-reviewed archive obtained during development,
not at installation time. Normalize its backslash member names to POSIX paths;
record every member's normalized name, size, external mode/type, CRC, and
compressed flag. Record the immutable source URL, all eleven installed file
digests/sizes/modes, LICENSE digest/mode, and the SHA-256 of the exact canonical
`SOURCE.json` bytes. Check the archive digest independently with `sha256sum`.

- [ ] **Step 4: Implement descriptor-safe snapshot and archive validation**

Implement the installer as Python 3 standard library. Snapshot every input to
a sealed memfd (or verified private temporary regular file when memfd sealing
is unavailable), compare metadata before/after copy, and close all descriptors
on every exception. Enforce production UID 0 and reject test hooks as root.

Parse the committed lock and site configuration canonically. Validate the
entire ZIP central directory against the lock before extraction; bound member
count, aggregate compressed/uncompressed size, compression ratio, filename
length, per-member size, and output. Extract only reviewed bytes with new-file
descriptor-relative writes and generate the NVIDIA installed policy from the
compiled adapter literals.

- [ ] **Step 5: Implement crash-consistent idempotent publication**

Take one nonblocking install flock. Create exact account/root ownership in
production and validate the current test UID in tests. Stage immutable trees
below their destination parents, fsync every file/directory bottom-up, rename
only to absent digest names, and verify an existing digest tree byte-for-byte.
Publish private auth/token then root-owned CA/TUF/config/policies, install the
stable supervisor and units, initialize slot A, daemon-reload, enable both
fixed units, and start the supervisor. Configuration is last. Reinstall emits
`unchanged`; a recovered partial install emits `changed`.

- [ ] **Step 6: Run installer GREEN**

Run:

```bash
uv run pytest tests/nodes/test_install_dgx_agent.py -v
```

Expected: all input, archive, crash, concurrency, idempotency, node identity,
secret-boundary, architecture, installed-path, license, and provenance tests
pass.

### Task 3: Strict installed runtime policy and production handler wiring

**Files:**
- Create: `agent/src/dgx_agent/runtime_policy.py`
- Modify: `agent/src/dgx_agent/config.py`
- Modify: `agent/src/dgx_agent/update_trust.py`
- Modify: `agent/src/dgx_agent/main.py`
- Create: `agent/tests/test_runtime_policy.py`
- Modify: `agent/tests/test_lifecycle.py`

**Interfaces:**
- Produces: `RuntimePolicy.load(path) -> RuntimePolicy`, `RuntimePolicy._load_for_test(path)`, and `build_agent(config)` with non-null `ReleaseInstaller` and `WorkloadOperations` boundaries.
- Runtime policy fields: schema/architecture, registry origin/repository, ORAS version/path/digest/auth path, independent platform and workload TUF root paths/digests/metadata/target roots, release/staging roots, and exact `spark-runtime-v1` adapter fields.
- `BoundedHTTPSFetcher` gains an optional `credential_provider: CredentialProvider`; when present, each fetch snapshots the current CA/certificate/key and closes the per-fetch pool.

- [ ] **Step 1: Write runtime-policy and production-wiring RED tests**

Create canonical root-owned-test fixtures and assert literal values. Negative
tests cover duplicate/extra/missing fields, wrong version/digest/architecture,
noncanonical origins/repository/paths, unsafe ownership/modes/ancestry,
symlink/hardlink/FIFO/oversized policy/root/auth/ORAS files, release/staging on
different filesystems, adapter drift, and absent artifact.

Patch only the network/process edge and inspect the real constructed graph:

```python
agent = build_agent(config)
assert isinstance(agent._context.releases, ReleaseInstaller)
assert isinstance(agent._context.workloads, WorkloadOperations)
oras = agent._context.releases._transport
assert oras._policy.credential_provider is agent._credentials
assert agent._context.releases._trust._fetcher._credential_provider is agent._credentials
```

Assert that config/runtime policy controls distinct control and registry
origins, the configured repository, exact roots, and architecture, while a
claim has no path/origin/repository influence.

- [ ] **Step 2: Run runtime RED**

Run:

```bash
uv run --project agent pytest agent/tests/test_runtime_policy.py agent/tests/test_lifecycle.py -v
```

Expected: collection fails for missing `dgx_agent.runtime_policy`, then wiring
tests fail because release/workload handlers are `None`.

- [ ] **Step 3: Implement and verify `RuntimePolicy`**

Follow the existing `InstalledPolicy` descriptor-read conventions, but require
canonical bytes, exact known fields, single-link files, maximum 64 KiB, and
root ownership in production. Validate canonical HTTPS registry origin,
repository token segments, ORAS 1.3.3, lowercase SHA-256, exact fixed absolute
roots, TUF bootstrap digest, current/specified supported architecture, and the
literal compiled adapter policy. `verify_installed()` descriptor-verifies ORAS,
auth, bootstrap root, and root ancestry before returning.

- [ ] **Step 4: Wire rotating TUF/ORAS/release/workload construction**

Extend agent configuration with fixed `runtime_policy_path`,
`enrollment_origin`, and `enrollment_token_path` fields. Build one
`CredentialStore`; pass it to `AgentClient`, dynamic `BoundedHTTPSFetcher`, and
`ORASPolicy`. Read exact bootstrap root bytes after runtime-policy verification.
Construct `TUFReleaseTrust(metadata_root, target_root,
control_origin + '/agent/v1/tuf/metadata/', control_origin +
'/agent/v1/tuf/targets/', bootstrap, fetcher, registry_origin, repository,
architecture)`, then `ReleaseInstaller`, then `WorkloadOperations` with that
same trust. Attach both to the real `OperationContext`.

- [ ] **Step 5: Run runtime-policy GREEN**

Run:

```bash
uv run --project agent pytest agent/tests/test_runtime_policy.py agent/tests/test_releases.py agent/tests/test_workloads.py agent/tests/test_lifecycle.py -q
```

Expected: all policy, accepted Task 3, and lifecycle tests pass.

### Task 4: Self-contained slot artifact build boundary

**Files:**
- Create: `agent/packaging/slot_entry.py`
- Create: `agent/tools/build-slot-artifact`
- Create: `agent/tests/test_slot_artifact.py`
- Modify: `agent/pyproject.toml`

**Interfaces:**
- Consumes: the freshly built `dgx_agent` wheel, committed protocol wheel, an explicit output path, and explicit target architecture equal to the native builder architecture.
- Produces: one self-contained `ET_DYN` or `ET_EXEC` ELF named `dgx-forge-agent` whose one SHA-256 binds the Python runtime, agent/protocol packages, native modules, and every runtime dependency.

- [ ] **Step 1: Write slot-closure RED tests**

The build test runs in a clean temporary output/cache directory and first
builds the ordinary wheel. It invokes `agent/tools/build-slot-artifact` with an
explicit native architecture and asserts ELF class/machine plus absence of any
adjacent package/runtime tree. It then moves the ELF outside the checkout,
renames `agent/.venv` out of discovery for the subprocess, sets
`PYTHONPATH=/nonexistent`, `PYTHONNOUSERSITE=1`, `PYTHONHOME=/nonexistent`, and
an empty temporary HOME, changes cwd outside the repository, and runs:

```python
help_result = subprocess.run([artifact, "--help"], env=isolated, cwd=outside)
module_result = subprocess.run([artifact, "--packaged-module-smoke"], env=isolated, cwd=outside)
assert help_result.returncode == module_result.returncode == 0
assert module_result.stdout == "packaged-agent-modules-ok\n"
```

Negative tests prove the builder rejects cross-architecture requests, missing
wheel/protocol input, output symlinks, and a packaging result that is a script
or console stub. A supervisor integration test installs and executes the
resulting ELF, then proves replacing the repository package or deleting the
current virtual environment does not change its behavior.

- [ ] **Step 2: Run artifact-build RED**

Run:

```bash
uv run --project agent pytest agent/tests/test_slot_artifact.py -v
```

Expected: tests fail because the builder and packaged entry module are absent.

- [ ] **Step 3: Implement pinned one-file packaging**

Add the development-only `pyinstaller==6.15.0` pin. The builder validates every input
descriptor, creates a private temporary packaging environment, installs only
the explicit freshly built agent wheel and committed protocol wheel plus their
locked dependencies, and invokes the pinned one-file packager on
`agent/packaging/slot_entry.py`. The entry delegates ordinary arguments to
`dgx_agent.main.main`; `--packaged-module-smoke` imports `client`, `config`,
`deadlines`, `main`, `nvidia_tools`, `oci`, `operations`, `probe`, `readiness`,
`releases`, `runtime_policy`, `state`, `update_trust`, and `workloads`, then
prints the literal success line. The builder verifies the final ELF machine,
fsyncs it, and publishes it atomically to the explicit output.

Run the builder only on the target architecture; the later signed release
pipeline supplies one release-produced ELF per supported architecture. Task 5
does not add network update or rollout logic. The installer consumes that
already built local ELF and never invokes the builder or resolves packages.

- [ ] **Step 4: Run closure and supervisor GREEN**

Run:

```bash
uv run --project agent pytest agent/tests/test_slot_artifact.py agent/tests/test_supervisor.py -v
```

Expected: the isolated no-repository/no-venv/no-user-or-global-site smoke and
verified FD launch pass.

### Task 5: Enrollment bootstrap and generation-bound readiness emission

**Files:**
- Create: `agent/src/dgx_agent/readiness.py`
- Modify: `agent/src/dgx_agent/client.py`
- Modify: `agent/src/dgx_agent/main.py`
- Modify: `agent/tests/test_client.py`
- Modify: `agent/tests/test_lifecycle.py`

**Interfaces:**
- Produces: `CredentialStore.install_initial(issued)`, `ReadinessReporter.from_environment()`, and an `Agent(..., on_authenticated_exchange=callable)` callback.
- Enrollment consumes only the fixed enrollment origin, installed token, locally durable CSR/key, public CA, and bounded node evidence.

- [ ] **Step 1: Write initial-enrollment and readiness RED tests**

Use a real local CA/client server and real credential store. Prove first startup
creates one Ed25519 key/CSR, pending approval reuses exact CSR and leaves the
token, restart after issued-response loss does not create a second key, issued
generation 1 is installed and active, mismatched certificate/key is rejected,
and token unlink happens only after active-pointer file+directory fsync.

Readiness tests use a real runtime directory and literal canonical marker.
Transport failure and pending enrollment write no marker. A successful
authenticated empty claim, pending-result replay, or ordinary result writes
exact generation/slot/digest once with mode `0600`; a missing/malformed
supervisor environment makes the reporter a no-op or startup error as defined
by whether all three variables are absent or partially present.

- [ ] **Step 2: Run enrollment/readiness RED**

Run:

```bash
uv run --project agent pytest agent/tests/test_client.py agent/tests/test_lifecycle.py -k 'initial or readiness or enrollment' -v
```

Expected: tests fail for missing initial credential installation, reporter,
and authenticated-exchange callback.

- [ ] **Step 3: Implement durable initial credential publication**

Add a dedicated `install_initial` path that requires no seed/active pointer,
requires issued generation 1, verifies node/key/certificate/CA/validity, writes
the generation through the existing descriptor-safe generation publication,
publishes `active.json`, removes pending CSR/key only afterward, and fsyncs the
credential directory. Do not weaken rotation semantics.

At startup, if no active credential exists and the restrictive token exists,
prepare/reuse the CSR, collect fixed probe evidence, call the fixed enrollment
origin without client identity, and return a retryable pending state. After an
issued response, install the active generation, unlink the exact token relative
to its validated bootstrap descriptor, and fsync that directory.

- [ ] **Step 4: Implement authenticated readiness publication**

Parse the three supervisor variables exactly and capture them before polling.
After any successful authenticated runtime request handled by `Agent.run_once`,
invoke the reporter. Publish the marker descriptor-relatively using random
`O_EXCL|O_NOFOLLOW` staging, exact `0600`, full canonical write, file fsync,
rename, and directory fsync. Never report after server-authenticated enrollment.

- [ ] **Step 5: Run enrollment/readiness GREEN**

Run:

```bash
uv run --project agent pytest agent/tests/test_client.py agent/tests/test_lifecycle.py agent/tests/test_supervisor.py -q
```

Expected: credential, token, marker, stale-marker, and accepted outbound-client
tests all pass.

### Task 6: Hardened systemd packaging and effective-property tests

**Files:**
- Create: `agent/systemd/dgx-forge-agent.service`
- Create: `agent/systemd/dgx-forge-agent-supervisor.service`
- Modify: `agent/tests/test_supervisor.py`
- Modify: `tests/nodes/test_install_dgx_agent.py`

**Interfaces:**
- Agent unit fixed command: `/usr/libexec/dgx-agent-supervisor run-agent`.
- Root unit fixed command: `/usr/libexec/dgx-agent-supervisor supervise`.
- Writable agent paths are only `/var/lib/dgx-forge-agent`, `/var/lib/dgx-forge/releases`, `/var/lib/dgx-forge/release-staging`, and `/run/dgx-forge-agent`; root supervisor writes only its state and runtime coordination paths.

- [ ] **Step 1: Write effective unit-property RED tests**

Copy units plus a test relocation drop-in into a temporary systemd unit search
path and run `systemd-analyze verify`. Parse `systemd-analyze security --json`
when supported, and use `systemd-analyze cat-config`/`systemd-analyze verify`
diagnostics for effective properties rather than source-line grep. Assert
non-root user/group, fixed commands, `UMask=0077`, `NoNewPrivileges=yes`, empty
ambient capabilities, bounded restart/start limits, private temp/devices,
protect system/home/kernel/control groups/clock/personality, memory W^X,
explicit read-only/read-write roots, Unix/IPv4/IPv6-only agent families,
no-network supervisor, and no Docker path/group/capability.

- [ ] **Step 2: Run unit RED**

Run:

```bash
uv run --project agent pytest agent/tests/test_supervisor.py -k systemd -v
systemd-analyze verify agent/systemd/dgx-forge-agent.service agent/systemd/dgx-forge-agent-supervisor.service
```

Expected: missing unit failures.

- [ ] **Step 3: Add compatible hardened units**

Set explicit dependencies so the root supervisor initializes/coordinates before
the agent starts, without a dependency cycle. Use systemd 249-compatible
directives for the supported DGX OS baseline; retain stronger directives
accepted by that version. Give the root unit only `CAP_CHOWN` for clean-boot
runtime ownership plus `CAP_DAC_READ_SEARCH` and `CAP_DAC_OVERRIDE` to consume
the exact `0600` agent-owned marker, and no ambient capability.
Preserve read-only `/dev/nvidia*` visibility under `PrivateDevices` using the
supported device policy/directives and do not expose unrelated devices.

- [ ] **Step 4: Run unit verification/security GREEN**

Run:

```bash
uv run --project agent pytest agent/tests/test_supervisor.py -k systemd -v
scripts/verify-agent-systemd --json
```

Expected: verify exits zero; record both exposure summaries and any target-DGX
compatibility ruling in the task report.

### Task 7: Final integration gates, report, and implementation commit

**Files:**
- Modify: `inventory/sbom/agent-python.spdx.json` only if the supply-chain generator proves it stale
- Modify: `inventory/sbom/manifest.json` only if the supply-chain generator proves it stale
- Create: `.superpowers/sdd/2026-08-03-spark-agent-runtime/task-5-report.md`

**Interfaces:**
- Produces: one final implementation commit with subject `feat: supervise Spark agents with A/B rollback`.

- [ ] **Step 1: Run focused tests and static checks**

Run and record exact counts/output:

```bash
uv run --project agent pytest agent/tests/test_supervisor.py agent/tests/test_runtime_policy.py agent/tests/test_client.py agent/tests/test_lifecycle.py -v
uv run pytest tests/nodes/test_install_dgx_agent.py -v
uvx --from ruff==0.16.1 ruff check .
shellcheck nodes/bin/* agent/supervisor/* 2>/dev/null || test $? -eq 127
git diff --check
```

- [ ] **Step 2: Run complete repository gates from the brief**

Run and record:

```bash
uv run --project agent pytest agent/tests -q
uv run --project agent_protocol pytest agent_protocol/tests -q
uv run --project control pytest control/tests -q
uv run pytest tests/nodes -q
uv run pytest deploy/compose/tests -q
docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml config --quiet
docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml -f deploy/compose/compose.builtin-ca.yaml config --quiet
scripts/verify-agent-systemd --json
uv run --project agent python -m compileall -q agent/src agent/supervisor nodes/bin/install-dgx-agent
uv build --project agent
uv run --project agent pytest agent/tests/test_slot_artifact.py -v
scripts/verify-supply-chain --json
git diff --check
```

Also create a fresh Python 3.12 virtual environment, install the committed
protocol wheel and newly built agent wheel, import every production module,
run `dgx-forge-agent --help`, build the native self-contained slot ELF, run its
isolated `--help` and `--packaged-module-smoke` with checkout/venv/global and
user site packages unavailable, run supervisor `--help` and an unprivileged
relocated FD-execution smoke, and validate an ARM64 ELF/site-policy fixture
without executing it.

The physical GPU-device acceptance remains the existing
`approved-physical-spark-lifecycle` release gate. It runs the installed-unit
inventory and health adapters on each supported DGX Spark device inventory;
local effective-property tests do not substitute for that physical evidence.

- [ ] **Step 3: Self-review the final diff**

Review every changed line and explicitly check crash consistency, concurrent
processes, symlink/hardlink/substitution races, descriptors/processes, ownership
and privilege transitions, systemd 249 compatibility, secrets and token
lifecycle, generic site/fleet behavior, source-vs-installed policy agreement,
and exclusion of SSH migration/recovery/rollout/network-update work. Convert
every discovered defect into a new observed RED test before fixing it.

- [ ] **Step 4: Write the complete evidence report**

Write `.superpowers/sdd/2026-08-03-spark-agent-runtime/task-5-report.md` with
approved design decisions, implementation/files, every RED and GREEN command
and output, all verification commands/results, systemd exposure and
compatibility ruling, final self-review, scope boundary, and remaining physical
or environmental concerns. Do not claim a gate that was not run successfully.

- [ ] **Step 5: Commit the final candidate**

Run final fresh verification relevant to any report-only edits, then:

```bash
git add agent/supervisor agent/systemd agent/src agent/tests agent/packaging agent/tools agent/pyproject.toml agent/uv.lock nodes/bin/install-dgx-agent nodes/vendor/nvidia-manageability.lock.json tests/nodes/test_install_dgx_agent.py inventory/sbom .superpowers/sdd/2026-08-03-spark-agent-runtime/task-5-report.md
git commit -m "feat: supervise Spark agents with A/B rollback"
```

Do not stage the controller-owned progress file or unrelated user changes. Do
not push or open a pull request.
