# Vonk Rust Spark Agent and Debian Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the production Python Spark service, privileged helper, and stable supervisor with memory-safe Rust binaries delivered as a signed ARM64 Debian package while retaining protocol and failure-behavior parity.

**Architecture:** A Rust workspace provides an unprivileged outbound agent, a narrowly privileged Unix-socket helper, and a stable A/B supervisor. Canonical JSON fixtures make the existing Python implementation the migration oracle until every controller operation and failure case passes against Rust. The agent initiates mTLS long polling to the controller, persists fences and operation receipts locally, and delegates only an allow-listed set of host mutations to the helper.

**Tech Stack:** Rust 1.97.1, 2024 edition, Tokio, rustls, reqwest, serde, clap, tracing, SQLite, systemd, Debian packaging, GitHub Actions, Syft/CycloneDX, Cosign/Sigstore.

---

## Task 1: Establish the pinned Rust workspace and protocol parity harness

**Files:**
- Create: `rust-toolchain.toml`
- Create: `Cargo.toml`
- Create: `rust/crates/vonk-agent-protocol/Cargo.toml`
- Create: `rust/crates/vonk-agent-protocol/src/lib.rs`
- Create: `rust/crates/vonk-agent-protocol/tests/fixtures.rs`
- Create: `agent_protocol/fixtures/enrollment-request.json`
- Create: `agent_protocol/fixtures/operation-poll.json`
- Create: `agent_protocol/fixtures/operation-result.json`
- Create: `agent_protocol/fixtures/workload-package.json`
- Create: `agent_protocol/tests/test_rust_fixtures.py`
- Modify: `.github/workflows/ci.yml`

- [x] Write `test_rust_fixtures.py` first. It must require each fixture to round-trip through the Python dataclasses and assert canonical JSON bytes and SHA-256 values from `agent_protocol/fixtures/manifest.json`.
- [x] Run `uv run --project agent_protocol pytest agent_protocol/tests/test_rust_fixtures.py -q`; confirm failure because the fixtures and manifest do not exist.
- [x] Add the four canonical fixtures and manifest by serializing existing Python protocol objects with sorted keys and compact separators.
- [x] Add a Rust workspace pinned to `channel = "1.97.1"`, edition 2024, `resolver = "3"`, and first-party `#![forbid(unsafe_code)]`.
- [x] Define matching serde structs in `vonk-agent-protocol`; reject unknown fields on signed or privileged messages.
- [x] Add Rust tests that parse, serialize canonically, and hash the same fixtures.
- [x] Add CI jobs for `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, and `cargo test --workspace` on x86_64; cache only the Cargo registry/build directory, never credentials.
- [x] Run the Python fixture test and `cargo test -p vonk-agent-protocol`; confirm both pass.
- [x] Commit: `feat(agent): establish Rust protocol parity fixtures`

## Task 2: Build configuration, identity storage, and one-time pairing

**Files:**
- Create: `rust/crates/vonk-agent/Cargo.toml`
- Create: `rust/crates/vonk-agent/src/main.rs`
- Create: `rust/crates/vonk-agent/src/config.rs`
- Create: `rust/crates/vonk-agent/src/identity.rs`
- Create: `rust/crates/vonk-agent/src/pair.rs`
- Create: `rust/crates/vonk-agent/tests/pairing.rs`
- Create: `control/tests/test_rust_agent_pairing.py`
- Modify: `control/src/dgx_control/enrollment.py`
- Modify: `control/src/dgx_control/agent_api.py`

- [x] Write `pairing.rs` first with a fake HTTPS controller. Assert an expired or reused pairing token fails closed, the CA is pinned before credentials are written, file modes are `0600`, and a successful response creates an agent key/certificate bound to the reported node identity.
- [x] Run `cargo test -p vonk-agent pairing`; confirm failure because the crate is absent.
- [x] Implement strict config loading from `/etc/vonk-forge/agent.toml` with controller URL, CA fingerprint, data directory, poll timings, and no secret values accepted from command-line flags.
- [x] Implement `vonk-agent pair --controller ... --token-stdin --ca-sha256 ...`; generate the private key on the Spark, submit the CSR and hardware identity, verify the pinned CA, and atomically persist credentials.
- [x] Add controller compatibility tests that exercise both the existing Python client and the Rust pairing request against the same enrollment endpoint.
- [x] Ensure audit entries record token identifier, node identifier, certificate serial, outcome, and reason without recording the token or private material.
- [x] Run pairing tests in Rust and Python; confirm pass.
- [x] Commit: `feat(agent): add pinned mTLS pairing`

## Task 3: Implement outbound long polling, leases, fences, and durable receipts

**Files:**
- Create: `rust/crates/vonk-agent/src/client.rs`
- Create: `rust/crates/vonk-agent/src/state.rs`
- Create: `rust/crates/vonk-agent/src/executor.rs`
- Create: `rust/crates/vonk-agent/tests/polling.rs`
- Create: `rust/crates/vonk-agent/tests/restart_receipts.rs`
- Modify: `agent/tests/test_client.py`
- Modify: `control/tests/test_agent_api.py`

- [x] Write failing Rust tests for poll timeout, controller restart, certificate rotation, operation deadline, monotonic fence rejection, duplicate operation replay, persisted result redelivery, and bounded jittered backoff.
- [x] Run `cargo test -p vonk-agent polling restart_receipts`; confirm failure at missing modules.
- [x] Implement a rustls-only HTTPS client that opens outbound long polls, sends protocol/capability versions, applies request deadlines, and never disables certificate verification.
- [x] Persist node identity, last accepted fence, operation status, and result body in SQLite under `/var/lib/vonk-forge`; use transactions so a restart cannot execute an acknowledged operation twice.
- [x] Require every mutating operation to carry operation ID, group ID, deadline, desired generation, and monotonic fence. Reject stale, expired, or identity-mismatched work before calling an executor.
- [x] Deliver results idempotently until the controller acknowledges their receipt; distinguish retryable transport failures from terminal operation failures.
- [x] Run the Rust tests plus the existing Python client tests. Compare captured wire payloads byte-for-byte after canonicalization.
- [x] Commit: `feat(agent): add durable outbound operation loop`

## Task 4: Implement inventory and the typed unprivileged workload executor

**Files:**
- Create: `rust/crates/vonk-agent/src/inventory.rs`
- Create: `rust/crates/vonk-agent/src/oci.rs`
- Create: `rust/crates/vonk-agent/src/workloads.rs`
- Create: `rust/crates/vonk-agent/src/health.rs`
- Create: `rust/crates/vonk-agent/tests/inventory.rs`
- Create: `rust/crates/vonk-agent/tests/workloads.rs`
- Modify: `agent_protocol/fixtures/operation-poll.json`
- Modify: `agent/tests/test_operations.py`

- [x] Write failing tests for disk/RAM/GPU inventory, image digest verification, weight manifest verification, typed container arguments, read-only root filesystem, dropped capabilities, device assignment, health evidence, stop-before-withdraw semantics, and rejection of shell/privileged/host-path inputs.
- [x] Run `cargo test -p vonk-agent inventory workloads`; confirm failure.
- [x] Implement inventory collection through `/proc`, `/sys`, and explicit NVIDIA CLI JSON/CSV calls with bounded execution and strict parsing. Report physical and available RAM, disk by managed store, GPU memory, driver/runtime versions, container runtime, and active allocations.
- [x] Implement OCI pull/inspect/start/stop through an argument-vector process wrapper; accept only digest-pinned images and controller-compiled typed runtime fields. Do not invoke a shell.
- [x] Materialize model artifacts only into `/var/lib/vonk-forge/models/sha256/<digest>` and containers only receive declared read-only model mounts plus a managed writable state directory.
- [x] Emit health evidence containing recipe revision, image digest, weight digest, model identity, rank/world size, listening endpoint, memory reservation, and observed readiness.
- [x] Run Rust workload tests and the matching Python oracle tests; resolve every behavior difference explicitly in fixtures.
- [x] Commit: `feat(agent): execute typed digest-pinned workloads`

## Task 5: Replace the privileged package helper

**Files:**
- Create: `rust/crates/vonk-agent-helper/Cargo.toml`
- Create: `rust/crates/vonk-agent-helper/src/main.rs`
- Create: `rust/crates/vonk-agent-helper/src/protocol.rs`
- Create: `rust/crates/vonk-agent-helper/src/operations.rs`
- Create: `rust/crates/vonk-agent-helper/tests/authority.rs`
- Create: `packaging/systemd/vonk-agent-helper.socket`
- Create: `packaging/systemd/vonk-agent-helper.service`
- Modify: `agent_protocol/tests/test_package_helper_authority.py`

- [x] Write failing authority tests that enumerate every permitted operation and reject unknown fields, traversal, symlink escape, arbitrary executable paths, environment injection, raw package-manager arguments, and calls from users outside the `vonk-agent` group.
- [x] Run `cargo test -p vonk-agent-helper authority`; confirm failure.
- [x] Implement a length-prefixed JSON protocol on `/run/vonk-forge/helper.sock`, authenticate peer credentials, and allow only exact typed operations: create managed directories, atomically activate an approved agent slot, install a verified Vonk `.deb`, restart named Vonk units, and perform bounded reboot scheduling.
- [x] Validate all artifact digests and signatures before privileged mutation; open managed paths defensively and require canonical descendants of configured roots.
- [x] Configure systemd socket activation, a read-only filesystem, private temporary directory, minimal capabilities, syscall filtering, and explicit writable paths.
- [x] Run Rust authority tests, Python protocol parity tests, and `systemd-analyze security` in an Ubuntu 24.04 VM/container fixture. Record the exposure score in CI artifacts.
- [x] Commit: `feat(agent): replace privileged helper with Rust service`

## Task 6: Replace the stable supervisor and preserve A/B rollback

**Files:**
- Create: `rust/crates/vonk-agent-supervisor/Cargo.toml`
- Create: `rust/crates/vonk-agent-supervisor/src/main.rs`
- Create: `rust/crates/vonk-agent-supervisor/src/slots.rs`
- Create: `rust/crates/vonk-agent-supervisor/src/health.rs`
- Create: `rust/crates/vonk-agent-supervisor/tests/rollback.rs`
- Create: `packaging/systemd/vonk-agent.service`
- Create: `packaging/systemd/vonk-agent-supervisor.service`
- Modify: `agent/tests/test_supervisor.py`
- Modify: `agent/tests/test_slot_artifact.py`

- [x] Write failing tests for valid activation, corrupt artifact, bad signature, crash loop, readiness timeout, power loss during pointer update, rollback to previous slot, and never rolling back across an incompatible state-schema boundary.
- [x] Run `cargo test -p vonk-agent-supervisor rollback`; confirm failure.
- [x] Implement signed slot manifests, atomic `current`/`previous` symlink exchange, child-process readiness handshake, bounded restart policy, and automatic rollback with a durable reason record.
- [x] Keep the stable supervisor outside replaceable agent slots. Require a separately signed Debian upgrade for supervisor/helper changes.
- [x] Port every scenario from the Python supervisor and slot-artifact tests to the shared fixture set, then run Python and Rust suites against it.
- [x] Commit: `feat(agent): add Rust A-B supervisor rollback`

## Task 7: Produce reproducible ARM64 Debian packages and a signed apt repository

**Files:**
- Create: `packaging/debian/control`
- Create: `packaging/debian/rules`
- Create: `packaging/debian/changelog`
- Create: `packaging/debian/conffiles`
- Create: `packaging/debian/postinst`
- Create: `packaging/debian/prerm`
- Create: `packaging/config/agent.toml`
- Create: `scripts/build-agent-deb`
- Create: `scripts/verify-agent-deb`
- Create: `.github/workflows/agent-release.yml`
- Create: `docs/operations/agent-package-release.md`
- Modify: `.github/dependabot.yml`

- [ ] Write `verify-agent-deb` first to fail unless package architecture is `arm64`, binaries are AArch64 ELF, system users/permissions/units are correct, configuration survives upgrade, checksums match, SBOM/provenance exist, and maintainer scripts do not make network calls.
- [ ] Run the verifier against an empty fixture; confirm its expected failure.
- [ ] Cross-build or natively build locked release binaries for `aarch64-unknown-linux-gnu`; strip deterministically and package as `vonk-forge-agent_<semver>_arm64.deb`.
- [ ] Make `postinst` create the system user/directories, daemon-reload, and enable the supervisor/socket without pairing or contacting the network.
- [ ] Generate CycloneDX/SPDX SBOMs, SLSA-compatible provenance, SHA-256 checksums, and keyless Cosign signatures in GitHub Actions. Publish the `.deb` and evidence to a GitHub Release.
- [ ] Add a second release job that publishes the same verified package into a signed `aptly` repository at `packages.vonkforge.ai`; keep signing identity in GitHub environment protection, not Railway or the repository.
- [ ] Test fresh install, offline reinstall from cache, upgrade, downgrade rejection, configuration preservation, removal, and rollback on Ubuntu 24.04 ARM64.
- [ ] Commit: `build(agent): release signed ARM64 Debian package`

## Task 8: Switch production to Rust and retire Python only after parity

**Files:**
- Create: `tests/acceptance/test_rust_agent_parity.py`
- Create: `docs/operations/install-spark-agent.md`
- Create: `docs/operations/migrate-python-agent.md`
- Modify: `compose.yaml`
- Modify: `control/src/dgx_control/agent_jobs.py`
- Modify: `README.md`
- Modify: `agent/pyproject.toml`

- [ ] Write the acceptance test first. Run the same controller sequence against Python and Rust agents and compare enrollment, inventory, duplicate handling, install/start/stop, group abort, update, rollback, and audit outcomes.
- [ ] Run the acceptance test against Rust-disabled configuration; confirm the explicit failure.
- [ ] Add controller capability negotiation so Rust is required for new pairings while enrolled Python agents receive a visible migration-required state and cannot be assigned newly introduced operation versions.
- [ ] Test an in-place migration that installs the `.deb`, transfers only public identity/configuration and durable operation receipts, starts Rust, verifies controller identity, and then disables the Python units.
- [ ] Remove Python from production packaging and service instructions only after all parity fixtures and a 24-hour physical Spark soak pass. Preserve the Python implementation under test-only migration-oracle tooling for one release.
- [ ] Run all Rust, agent protocol, controller, security, and physical acceptance suites. Attach exact versions and logs to the release evidence.
- [ ] Commit: `feat(agent): make Rust the production Spark service`

## Verification

Run from the repository root:

```bash
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
uv run --project agent_protocol pytest agent_protocol/tests -q
uv run --project agent pytest agent/tests -q
uv run --project control pytest control/tests -q
scripts/build-agent-deb
scripts/verify-agent-deb dist/vonk-forge-agent_*_arm64.deb
git diff --check
```

The plan is complete only after one single-node and one multi-node physical Spark deployment prove pairing, restart recovery, install, start, stop, agent upgrade, failed-upgrade rollback, and controller route withdrawal under the Rust service.
