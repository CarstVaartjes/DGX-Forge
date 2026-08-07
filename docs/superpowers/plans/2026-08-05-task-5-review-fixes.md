# Task 5 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all independent-review findings against Task 5 and leave every repository, runtime, packaging, systemd, and supply-chain gate green.

**Architecture:** Keep the approved stable A/B boundary and generic networkless installer, but close each review-discovered trust gap at its original boundary. The installed supervisor starts through isolated Python, pending activation is committed only after a fresh descriptor verification, readiness is consumed by atomic quarantine, CA input is parsed as X.509 CA certificates, destination ancestry is descriptor-validated, and systemd policy is checked through a disposable installed root.

**Tech Stack:** Python 3.12 standard library, pytest 8.4.2, systemd 249-compatible units, systemd-analyze 255 verification host, Ruff 0.16.1

## Global Constraints

- Work directly on `main`; do not push or open a pull request.
- Preserve fixed production paths, no SSH use, no inbound listener, no arbitrary command/path/environment selection, and the generic one-node-at-a-time installer contract.
- Preserve the exact closed-network, non-root agent boundary and the stable supervisor outside both A/B slots.
- Every production defect receives an observed behavioral RED before its production fix.
- Repository-wide `uvx --from ruff==0.16.1 ruff check .` must exit zero without weakening Ruff configuration or suppressing legitimate findings.
- Systemd verification uses `scripts/verify-agent-systemd`; it installs the production units and executable into a disposable root and runs both verify and offline security by installed unit name.
- The physical device acceptance gate is a later real-Vonk Forge inventory/health smoke; local tests must still prove the complete closed read-only effective policy.
- Do not modify `.superpowers/sdd/2026-08-03-node-agent-runtime/progress.md`.

---

### Task 1: Critical startup and trust-boundary fixes

**Files:**
- Modify: `agent/supervisor/vonk-agent-supervisor`
- Modify: `agent/systemd/vonk-forge-agent-supervisor.service`
- Modify: `agent/tests/test_supervisor.py`
- Modify: `nodes/bin/install-vonk-agent`
- Modify: `tests/nodes/test_install_vonk_agent.py`

**Interfaces:**
- Consumes: the fixed `/usr/bin/python3` interpreter, clean `/run/vonk-forge-agent`, and public PEM CA bundle input.
- Produces: isolated supervisor startup, exact minimal coordinator capabilities, and certificate-only CA output.

- [x] **Step 1: Add isolated-startup RED**

Run the installed supervisor executable with a writable fake home containing both user-site and global-site `sitecustomize.py` payloads. Assert neither payload executes before `--help` or `run-agent` verifies a slot.

- [x] **Step 2: Observe isolated-startup RED**

Run `uv run --project agent pytest agent/tests/test_supervisor.py::test_supervisor_entrypoint_ignores_writable_python_site_hooks -v`.
Expected: FAIL because the existing `#!/usr/bin/python3` entry point imports user-site startup code.

- [x] **Step 3: Isolate the fixed entry point and verify GREEN**

Change the production shebang to `#!/usr/bin/python3 -I`, retain the fixed `ExecStart=/usr/libexec/vonk-agent-supervisor ...` interface, and rerun the focused test.

- [x] **Step 4: Add clean-boot capability RED**

Exercise runtime-directory creation and ownership through the real supervisor path, and parse the installed unit's effective capability bounding set. Assert the exact capability set contains the identity operation used by `fchown` and no unrelated capability.

- [x] **Step 5: Observe capability RED, add only `CAP_CHOWN`, and verify GREEN**

Run `uv run --project agent pytest agent/tests/test_supervisor.py -k 'clean_boot_runtime or capability' -v` before and after changing the root unit to `CAP_CHOWN CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE`.

- [x] **Step 6: Add relabeled-private-key/non-CA certificate REDs**

Relabel real private-key DER as `CERTIFICATE`, supply a valid leaf certificate with `BasicConstraints(ca=False)`, and append trailing/mixed material. Every case must fail before target mutation; the fixture CA must still install.

- [x] **Step 7: Observe CA RED, implement bounded DER X.509 CA parsing, and verify GREEN**

Run `uv run pytest tests/nodes/test_install_vonk_agent.py -k ca -v`; parse each exact PEM block as a structurally valid X.509 certificate, require the critical/basic-constraints CA boolean, reject duplicates/mixed/trailing material, and rerun.

### Task 2: Activation/readiness correctness

**Files:**
- Modify: `agent/src/vonk_agent/main.py`
- Modify: `agent/tests/test_lifecycle.py`
- Modify: `agent/supervisor/vonk-agent-supervisor`
- Modify: `agent/tests/test_supervisor.py`

**Interfaces:**
- Consumes: an authenticated parsed claim, a pending slot digest, and agent-owned readiness marker.
- Produces: readiness before claim execution, stable commit only for a freshly verified slot, and replacement-safe marker consumption.

- [x] **Step 1: Add and observe non-empty-claim readiness-order RED**

Use a probe that records execution order. Run `uv run --project agent pytest agent/tests/test_lifecycle.py::test_nonempty_authenticated_claim_reports_readiness_before_operation_execution -v`; expect execution before readiness.

- [x] **Step 2: Move readiness reporting before operation execution and verify GREEN**

Call `_report_authenticated_exchange()` immediately after every successfully parsed claim response, before registry execution. Keep transport/parse failures unreported.

- [x] **Step 3: Add and observe pending-slot substitution RED**

Replace the pending ELF after accepted readiness but before the writer-lock commit. Run `uv run --project agent pytest agent/tests/test_supervisor.py::test_pending_slot_replacement_before_commit_rolls_back -v`; expect the old implementation to commit the replacement stable.

- [x] **Step 4: Reverify under the writer lock and verify GREEN**

After reacquiring the writer lock and confirming exact state, descriptor-open and verify the active pending slot against the expected digest. Roll back once on mismatch; publish stable only after the fresh check succeeds.

- [x] **Step 5: Add and observe stat-to-unlink replacement RED**

Replace `readiness.json` between final identity check and unlink. Run `uv run --project agent pytest agent/tests/test_supervisor.py::test_readiness_replacement_after_identity_check_survives -v`; expect the replacement to be deleted.

- [x] **Step 6: Implement atomic quarantine consumption and verify GREEN**

Atomically rename `readiness.json` to a random root-only quarantine name, open/verify that quarantined inode, and unlink only that name. A concurrently published new `readiness.json` remains untouched.

### Task 3: Installer ancestry and GPU device policy

**Files:**
- Modify: `nodes/bin/install-vonk-agent`
- Modify: `tests/nodes/test_install_vonk_agent.py`
- Modify: `agent/systemd/vonk-forge-agent.service`
- Modify: `agent/tests/test_supervisor.py`

**Interfaces:**
- Consumes: fixed destination policies and NVIDIA device nodes.
- Produces: descriptor-validated parent chains and a closed read-only GPU device set.

- [x] **Step 1: Add and observe unsafe existing-parent RED**

Precreate a publication parent with wrong owner or group/world-writable non-sticky mode and assert installation fails before root-owned publication. Run `uv run pytest tests/nodes/test_install_vonk_agent.py::test_root_publication_rejects_untrusted_existing_parent -v`.

- [x] **Step 2: Validate every opened ancestor and final parent, then verify GREEN**

Always call `_ensure_directory` before publication and make `_open_destination_directory` verify each descriptor against the required owner/mode policy, including the final parent.

- [x] **Step 3: Add and observe complete device-policy RED**

Parse the installed unit's effective `DevicePolicy`, `DeviceAllow`, and bind paths. Require the reviewed Vonk Forge GPU node set: read-only `/dev/nvidiactl`, `/dev/nvidia0`, modeset, UVM, UVM tools, and monitor capability `/dev/nvidia-caps/nvidia-cap2` under `DevicePolicy=closed`. Do not grant the MIG configuration capability or nonexistent Vonk Forge GPU node NVSwitch devices.

- [x] **Step 4: Add the reviewed read-only allowlist and verify GREEN**

Retain `PrivateDevices=yes`, grant only `r`, and document that the existing `approved-physical-node-lifecycle` release gate runs real inventory and health adapters against the installed unit on every supported Vonk Forge GPU node device inventory.

### Task 4: Executable systemd harness and repository lint cleanup

**Files:**
- Create: `scripts/verify-agent-systemd`
- Modify: `agent/tests/test_supervisor.py`
- Modify: `docs/superpowers/plans/2026-08-04-stable-agent-supervisor.md`
- Modify: every Python file reported by the literal Ruff gate only as required by Ruff findings

**Interfaces:**
- Produces: `scripts/verify-agent-systemd [--json]`, exiting zero only when installed-root verify and both installed-unit security analyses succeed.

- [x] **Step 1: Preserve literal systemd RED**

Run `systemd-analyze verify agent/systemd/vonk-forge-agent.service agent/systemd/vonk-forge-agent-supervisor.service`.
Expected: exit 1 with `/usr/libexec/vonk-agent-supervisor is not executable`.

- [x] **Step 2: Add harness RED, implement the disposable installed root, and verify GREEN**

The regression calls the absent command first. Implement a Python standard-library harness using `TemporaryDirectory`, install both units and the real supervisor at their exact fixed paths, copy the host baseline units required for dependency resolution, then run `systemd-analyze verify --root=...` and `systemd-analyze security --offline=yes --root=... --json=short UNIT` for both installed unit names. Run `scripts/verify-agent-systemd --json`; expected exit 0 and canonical JSON containing both exposure results.

- [x] **Step 3: Update the original plan's literal gates**

Replace non-executable source-tree verify/security commands with `scripts/verify-agent-systemd --json` in focused and final verification sections.

- [x] **Step 4: Preserve repository-wide Ruff RED and clear mechanical findings**

The observed command is `uvx --from ruff==0.16.1 ruff check . --statistics` and reports 174 errors. Apply safe Ruff fixes, then address remaining findings one rule/file at a time without ignores or configuration changes.

- [x] **Step 5: Add behavioral regressions before semantic lint edits**

Before changing exception boundaries, subprocess status handling, loop-variable capture, dataclass defaults, or error types, run the closest focused suite and add a regression wherever the lint correction can change observable behavior.

- [x] **Step 6: Run literal Ruff GREEN**

Run `uvx --from ruff==0.16.1 ruff check .`; expected exit 0 with `All checks passed!`.

### Task 5: Full verification, evidence, and delivery

**Files:**
- Modify: `.superpowers/sdd/2026-08-03-node-agent-runtime/task-5-report.md`

**Interfaces:**
- Produces: one local fix commit; no push or PR.

- [x] **Step 1: Run focused and full suites**

Run focused supervisor/installer/lifecycle tests, all agent/protocol/control/node/Compose suites, both Compose configuration variants, and the exact Ruff gate.

- [x] **Step 2: Run packaging/system/supply-chain gates**

Run `scripts/verify-agent-systemd --json`, compileall/py_compile, fresh wheel install/import/console smoke, native slot build/isolated smoke, ARM64 validation, `scripts/verify-supply-chain --json`, and `git diff --check`.

- [x] **Step 3: Append exact evidence and self-review**

Remove every pre-existing exemption. Record each RED command/result, covering test and GREEN result, final counts, systemd scores, device-policy physical acceptance gate, and scope preservation. Review every changed line and ensure the controller-owned progress file is unstaged.

- [x] **Step 4: Commit locally**

Stage only Task 5 fix files and commit with a concise fix subject. Do not push or open a pull request.
