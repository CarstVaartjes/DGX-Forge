# Task 5 report — stable Spark agent supervision and installation

Date: 2026-08-05
Branch: `main` (direct-main override)
Original Task 5 commit: `af6a2eb`
Independent-review fix parent: `57c9463` (with unrelated documentation-only
interleavings `01d6366` and `57c9463` preserved)
Design: `docs/superpowers/specs/2026-08-04-stable-agent-supervisor-design.md`
Plan: `docs/superpowers/plans/2026-08-04-stable-agent-supervisor.md`

## Outcome

Task 5 supplies a stable, non-replaceable A/B supervisor; a native self-contained
agent slot artifact; restrictive split-privilege systemd units; a generic,
networkless, crash-recovering per-node installer; an exact NVIDIA Enterprise
Manageability lock and installed policy; strict installed runtime policy; and the
remaining enrollment/readiness wiring required for safe activation and rollback.

The replaceable agent executes only a descriptor-verified, immutable ELF from fixed
slot A or B as `dgx-agent`. The root supervisor coordinates fixed state and service
units only. It never accepts a path or shell argument. Activation is generation,
slot, digest, deadline, boot-attempt, and authenticated-readiness bound. Invalid or
unready pending slots roll back once to the verified prior slot; corrupt stable state
and invalid stable slots fail closed.

Independent review closed all three critical and seven important findings. The
supervisor now starts Python in isolated mode, re-verifies the pending ELF immediately
before commit, and atomically moves readiness into a root-only quarantine before
inspection. The installer parses each submitted certificate as X.509 and requires
`CA:TRUE`, while publication rejects pre-existing destination ancestry that does not
already satisfy its exact owner/mode policy. The agent reports authenticated readiness
before executing a non-empty claim. Effective systemd policy includes the clean-boot
`CAP_CHOWN` requirement and the complete reviewed read-only DGX Spark device set.

## Delivered boundaries

- `agent/supervisor/dgx-agent-supervisor`
  - isolated `/usr/bin/python3 -I` entry, so writable user/site hooks cannot run before
    slot verification;
  - canonical duplicate-free bounded state and readiness JSON;
  - immutable ELF/digest/architecture/single-link checks through held descriptors;
  - atomic staged state publication with file and directory fsync;
  - separate coordination and state locks, with the state writer lock released while
    the restarted agent emits readiness;
  - exact-state recheck before commit or rollback;
  - atomic readiness quarantine in root-owned `/run/dgx-forge-agent-supervisor`, so a
    replacement published at either pathname-race boundary survives untouched;
  - fresh descriptor/digest/ELF verification under the writer lock immediately before
    pending-state commit, plus bounded A/B rollback.
- `agent/systemd/*.service`
  - non-root agent and minimal root coordinator split;
  - explicit supplementary-group reset, empty agent capabilities, no-new-privileges,
    strict filesystem/home/kernel/device protections, namespace restrictions, fixed
    writable roots, and bounded service restart/start behavior;
  - coordinator bounding set is exactly `CAP_CHOWN CAP_DAC_READ_SEARCH
    CAP_DAC_OVERRIDE`, sufficient for first-boot runtime ownership without adding
    network or executable-mutation privilege;
  - `DevicePolicy=closed` and read-only grants/binds for `/dev/nvidiactl`,
    `/dev/nvidia0`, modeset, UVM, UVM tools, and monitor-only
    `/dev/nvidia-caps/nvidia-cap2`; no MIG configuration or NVSwitch grant;
  - both units are installed and enabled; only the supervisor is explicitly started.
- `nodes/bin/install-dgx-agent`
  - no network operation and no mutable bundle lookup;
  - snapshots all authorized inputs before mutation;
  - acquires the root-owned install flock before account resolution/creation;
  - validates a dedicated nonzero system account, exact home/group/nologin shell, and
    absence of supplementary/admin/docker groups;
  - retains trusted destination dirfds throughout writes, chown, staged-inode checks,
    no-replace rename, cleanup, and fsync;
  - bounded restart cleanup after every file/tree publication crash boundary;
  - every publication parent is descriptor-opened and checked against exact final
    ownership/mode; unsafe pre-existing root publication ancestry is never repaired or
    traversed;
  - fixed `/usr/bin/openssl x509 -inform DER` parsing with a fixed C locale requires
    an actual CA certificate and rejects private-key bytes, relabeled DER, and non-CA
    X.509 before target mutation;
  - node-bound durable proof before treating the one-time enrollment token as consumed;
  - mandatory stable-supervisor and two-unit source snapshots before target mutation.
- `agent/tools/build-slot-artifact`
  - descriptor snapshots wheel and entry bytes into a private build root;
  - rejects unsafe wheel members and extracts only the snapshots;
  - verifies the exact frozen lock digest and exact packaging distribution versions;
  - invokes the fixed project PyInstaller interpreter with a fixed environment and no
    install, cache, PATH lookup, or network step;
  - atomically publishes one native self-contained ELF.
- `agent/src/dgx_agent/runtime_policy.py` and runtime integration
  - exact digest-derived ORAS path and exact auth/TUF/release/staging locations in
    production; `_load_for_test` alone accepts an explicit relocation prefix;
  - one credential provider is shared by control HTTPS, TUF, and ORAS;
  - production `build_agent` constructs real release and workload handlers.
- Enrollment/readiness integration
  - durable pending metadata distinguishes enrollment from rotation;
  - generation-one publication recovers idempotently at every pending cleanup boundary;
  - renewal activation reports readiness after durable local activation;
  - one reporter instance publishes readiness only once;
  - supervisor and reporter share the `1..999999999` generation bound.
- `nodes/vendor/nvidia-manageability.lock.json`
  - exact official archive/version/SHA-256, all 132 archive member metadata records,
    all 12 installed role/path/digest/size/mode/version mappings, MIT license, source,
    provenance document, and provenance digest.

## RED/GREEN evidence

Strict task-start REDs were observed for missing supervisor (7 failures), missing
installer, missing runtime-policy module, missing readiness module, new configuration
fields, and missing slot builder (2 failures). Those boundaries were added only after
their focused failures.

The two audit passes added the following permanent behavioral regressions. The command
for each agent test is `uv run --project agent pytest <test> -q`; installer tests use
`uv run pytest <test> -q`.

| Finding / RED observation | Permanent covering test and GREEN behavior |
|---|---|
| Supervisor held its exclusive state flock through restart/wait; the real fake-systemctl child timed out at 3 seconds. | `test_supervise_releases_writer_lock_so_restarted_agent_can_emit_readiness`: child `run-agent` emits readiness and supervision completes. |
| Activation used a synchronous restart. | `test_activation_accepts_only_exact_generation_bound_readiness`: captures `--no-block restart dgx-forge-agent-supervisor.service`. |
| Clean boot lacked a prepared readiness directory and the first regression raised `FileNotFoundError: run`. | `test_stable_supervisor_prepares_clean_boot_runtime_without_dependency_cycle`: supervisor creates exact mode 0700 runtime and starts without an ordering cycle. |
| Supervisor could not reliably consume the service-owned marker with its original capability set. | stale-marker rollback test plus systemd test: the exact marker is consumed through the coordinator's bounded DAC permissions; independent review subsequently added only the clean-boot `CAP_CHOWN` requirement. |
| A pathname replacement between marker read and unlink could delete the replacement; initial race test failed because no replacement survived. | `test_readiness_replacement_during_consumption_is_not_unlinked`: opened inode is accepted, atomically swapped replacement remains for the next poll. |
| Agent writable release locations differed from installed runtime roots. | installer idempotency test and systemd unit test require `/var/lib/dgx-forge/releases` and `/var/lib/dgx-forge/release-staging` exactly. |
| Source-line-only systemd assertions did not prove parsed policy. | `test_systemd_units_verify_and_enforce_split_privilege_hardening`: alternate-root `verify` plus parsed offline JSON assertions for effective user/no-new-privileges/protect-system/private-network. |
| File publication used pathname reopen/rename inside service-owned directories. The first substitution run returned success instead of rejecting the injected replacement. | `test_file_publication_resists_parent_and_temporary_inode_substitution`: parent replacement cannot redirect a root write; temporary inode substitution never reaches the final name; next-run recovery converges. |
| Existing `dgx-agent` validation accepted root/wrong-home/wrong-group/admin-group identities. | `test_account_contract_rejects_root_wrong_home_group_and_admin_membership`: every unsafe identity fails; exact dedicated UID/GID/home/nologin succeeds. |
| Account resolution/creation preceded the install flock. | `test_installer_locks_before_account_resolution_and_concurrent_first_install`: asserts lock-before-account order and concurrent first-install convergence. |
| SIGKILL left `.new` file/tree publications with no restart recovery. | `test_abandoned_publication_crash_boundaries_recover_bounded_exact_staging`, parameterized over create, write, file-fsync, tree-fsync, rename, and parent-fsync: crash exits 99 and next run converges without `.new`. |
| Config existence plus token absence was treated as completed enrollment. The corrected expectation initially failed the old `unchanged` assertion (`changed != unchanged`). | `test_reinstall_restores_token_without_durable_node_bound_active_identity` and `...suppresses_token_only_for_durable_node_bound_active_identity`: exact active generation and credential node binding control restoration. |
| Generic CA bytes, including key material, were accepted. | `test_private_key_or_mixed_ca_input_is_rejected_before_target_mutation`: private-key/mixed material fails before the target root exists. |
| Initial credential publication returned early when active existed and could leave enrollment CSR/key state to be renewed. Cleanup-boundary parameters 3/4 initially failed recovery. | `test_initial_install_recovers_each_pending_cleanup_boundary_without_renewal`, four parameters: enrollment metadata remains last and constructor/startup recovery removes partial state without renewal. |
| Readiness rewrote/fsynced on every exchange and renewal activation did not report before a later claim. Both focused tests were RED. | lifecycle readiness and rotation recovery tests: second report is false with unchanged mtime; successful activation reports even when the following claim fails; activation failure never reports. |
| Runtime policy accepted arbitrary absolute “safe” paths. | `test_runtime_policy_rejects_alternate_absolute_installed_paths`, seven parameters: every alternate ORAS/auth/TUF/release/staging path fails. |
| Missing unit sources were silently skipped and only the coordinator was enabled. | `test_missing_unit_fails_before_target_mutation_and_both_units_are_enabled`: missing unit fails before target mutation; captured commands reload, enable both, and start only supervisor. |
| Slot build followed PATH and normal uv cache/network; hostile `uv` was invoked and exited 91. | `test_builder_snapshots_wheels_and_ignores_hostile_path_network_and_empty_cache`: hostile PATH marker remains absent, post-snapshot wheel substitution is ignored, empty cache/proxy traps do not matter, native ELF succeeds. |
| NVIDIA assertions covered counts and one sample only. | `test_nvidia_lock_binds_exact_archive_license_provenance_and_installed_subset`: exact installed mapping, license, source, provenance, archive/version/digest. |

Additional supervisor REDs fixed during self-review included ancestor symlink acceptance,
umask-dependent state mode, invalid pending-slot persistence, consumed-token restoration,
immutable-tree internal symlinks, noncanonical config paths, missing publication crash
hooks, nonfinite deadlines, and rollback/service coordination races.

### Independent review fix round — exact RED/GREEN evidence

No finding was closed from source inspection alone. Each production change followed an
observed failing regression (or the literal failing system command), then the closest
focused GREEN:

| Review finding | Observed RED | Permanent GREEN evidence |
|---|---|---|
| Critical: the supervisor's normal Python startup could execute writable site hooks before verifying the slot. | `test_supervisor_entrypoint_ignores_writable_python_site_hooks` created the hostile marker under the original shebang. | `/usr/bin/python3 -I` prevents both `PYTHONPATH` and user-site hook execution; the focused test passes (`1 passed`). |
| Critical: clean-boot coordinator ownership repair required `CAP_CHOWN`, absent from the effective bounding set. | The effective-property assertion reported `CapabilityBoundingSet_CAP_CHOWN_FSETID_SETFCAP=true` (capability absent). | Clean-boot runtime creation plus effective unit assertions pass (`2 passed`); the exact bounding set contains CHOWN and the two DAC capabilities only. |
| Critical: certificate-shaped bytes were accepted without X.509/CA semantics. | With the parser fix deliberately removed, both the relabeled private-key DER case and the non-CA certificate case returned installer status 0. | The two rejection cases plus a valid fixture CA pass (`3 passed`), and rejection occurs before target-root mutation. |
| Important: authenticated readiness was emitted only after executing a non-empty operation. | `test_nonempty_authenticated_claim_reports_readiness_before_operation_execution` recorded `execute,ready`. | The lifecycle transport boundary now records `ready,execute`; it and the empty/authenticated exchange coverage pass (`2 passed`). |
| Important: the pending ELF was not reverified after readiness and before stable commit. | `test_pending_slot_replacement_before_commit_rolls_back` replaced the ready slot, but state committed stable. | A fresh `_open_slot` under the reacquired writer lock detects replacement and rolls back/restarts (`2 passed`). |
| Important: readiness `stat`/`unlink` remained a pathname race. | The post-identity replacement regression lost the newly published canonical marker. | Atomic no-replace quarantine preserves replacements at both injected race boundaries; four focused readiness/race cases pass. |
| Important: existing destination parents bypassed full trust validation. | `test_root_publication_rejects_untrusted_existing_parent` accepted a pre-created mode-0777 `/usr/libexec`. | Unsafe ancestry is rejected, safe install remains idempotent, and publication-race coverage passes (`3 passed`). |
| Important: the closed NVIDIA allowlist omitted the physical GPU and monitor-capability nodes. | Effective-policy parsing found `/dev/nvidia0` and `/dev/nvidia-caps/nvidia-cap2` absent. | `test_agent_effective_device_policy_is_closed_and_read_only` passes with exact read-only allow/bind sets and no cap1/NVSwitch grant (`1 passed`). |
| Important: literal source-tree systemd verification was not executable evidence. | `systemd-analyze verify agent/systemd/dgx-forge-agent.service agent/systemd/dgx-forge-agent-supervisor.service` exited 1 because fixed `/usr/libexec/dgx-agent-supervisor` was not installed; the harness regression initially found no script. | `scripts/verify-agent-systemd --json` installs the real units/executable into a disposable root, verifies by installed name, and runs offline security for both units. |
| Important: the mandatory greenfield Ruff gate had 174 findings. | `uvx --from ruff==0.16.1 ruff check . --statistics` reported 174 errors. | Safe mechanical fixes plus semantic regressions produced exact repo-wide `All checks passed!`; no ignore, per-file suppression, or lint configuration change was added. |

Semantic Ruff repairs were also test-driven. Eight type/error-boundary regressions were
observed failing across dashboard, jobs, metrics, reconciliation, runtime, identity,
and install-step parsing before their corrections (`6` control and `2` root tests then
passed). Four broad-catch regressions proved unexpected `AssertionError` was swallowed
by workload inspection, reconciliation, worker execution, and install orchestration;
all four now propagate programming defects while expected operational failures remain
normalized. The protocol/runtime byte changes triggered content-address REDs in catalog,
wheel-lock, SBOM, and slot-builder verification; manifests, definition locks, the
reproducible protocol wheel, both consumer locks, SPDX documents, and the reviewed
builder lock digest were regenerated/re-pinned before the related suites passed.

## Final verification evidence

- Focused security/runtime suites:
  - supervisor: `22 passed in 3.49s`;
  - installer: `23 passed in 7.10s`;
  - lifecycle plus workloads: `30 passed in 1.33s`;
  - the two real native-slot build/smoke regressions: `2 passed in 16.13s`.
- Complete suites:
  - `uv run --project agent pytest agent/tests -q` → `515 passed in 54.94s`;
  - `uv run --project agent_protocol pytest agent_protocol/tests -q` → `321 passed in 0.32s`;
  - `uv run --project control pytest control/tests -q` → `288 passed in 36.27s`;
  - `uv run pytest -q` → `679 passed, 1 skipped in 72.56s`;
  - `uv run pytest tests/nodes -q` → `70 passed, 1 skipped in 11.38s`;
  - `uv run pytest deploy/compose/tests -q` → `22 passed in 8.27s`.
- Compose rendering:
  - step-ca and built-in-CA commands using
    `--env-file deploy/compose/tests/test.env` both exited 0 under
    `docker compose ... config --quiet`.
- systemd installed-root verification:
  - `scripts/verify-agent-systemd --json` → `verify: passed`;
  - effective offline security: agent `2.8 OK`, supervisor `2.8 OK`, each with
    `81` assessments;
  - the effective-property regression additionally proves exact capabilities,
    closed/read-only devices, users, writable paths, and sandbox controls.
- Packaging and ELF:
  - the protocol wheel was rebuilt twice with identical SHA-256
    `0eb0b930b4ac606bc6ddcd7aec1015e55f6e2e1972fb69b39d9c0258292d32fc`;
  - a fresh offline agent wheel environment resolved and installed 14 exact packages,
    imported client/main/OCI/operations/readiness/releases/runtime-policy/workloads
    under `python -I`, and passed installed `dgx-forge-agent --help`;
  - native PyInstaller output passed ELF class/endianness/machine checks, isolated
    `--help`, packaged-module smoke, hostile PATH/cache/network tests, and cross-arch
    rejection/ARM64 header validation without foreign execution.
- Supply chain:
  - all 15 verifier regressions pass;
  - `scripts/verify-supply-chain --json` → `ok: true`, 6 images, 4 SBOMs,
    manifest SHA-256 `ef86650a7a23489866111f0e90efc50f80a3c72e73da880ca9c10ba9e4fe0200`.
- Static/build gates:
  - exact `uvx --from ruff==0.16.1 ruff check .` → `All checks passed!`;
  - project/repository `compileall` plus direct `py_compile` of supervisor, installer,
    systemd harness, and slot builder exited 0;
  - `git diff --check` exited 0.

There are no lint, systemd, packaging, or supply-chain exemptions. Direct source-tree
systemd verification is retained as a documented RED because fixed production
`ExecStart` must point at the installed `/usr/libexec` path; the deterministic
disposable installed-root harness is the final executable gate and does not alter that
production path.

## Compatibility and security decisions

- `PrivateDevices=yes` and `DevicePolicy=closed` are retained with the six exact
  read-only NVIDIA paths listed above. NVIDIA's DGX Spark hardware and MIG capability
  documentation support the single GPU and monitor-only cap2 selection; cap1 (MIG
  configuration) and NVSwitch devices are intentionally absent.
- No live DGX device inventory was available in this environment. The named later
  physical release gate is `approved-physical-spark-lifecycle`; it must exercise the
  real installed unit's inventory/health adapters on every supported DGX Spark device
  inventory before publication.
- The root coordinator retains CHOWN plus DAC read/search/override so first-boot
  runtime ownership and root-only readiness quarantine work under its effective
  bounding set. It has no network namespace access and accepts no external path or
  command.
- The agent receives no capabilities or supplementary groups. `RestrictNamespaces=yes`,
  `ProtectSystem=strict`, and exact `ReadWritePaths` prevent mutation of executable,
  policy, and neighboring state roots.
- Both units may be enabled safely: the agent can create its runtime independently;
  the supervisor also prepares it before coordinator-initiated start. `PartOf` provides
  restart propagation without an `After`/`Requires` ordering cycle.

No push or PR was created. The controller owns publication after independent review.
