# Stable Spark Agent Supervisor and Local Installer Design

## Purpose and scope

Task 5 installs one Spark node from already authorized local inputs, keeps the
replaceable agent in stable A/B slots, and wires the accepted release runtime
into the production agent. Routine operation remains outbound mutual TLS.
This design does not add SSH orchestration, Ansible, cloud-init, a migration
journal, a recovery CLI, a control-plane rollout planner, or the network
`agent.update` operation. SSH remains available for explicit human bootstrap
and recovery, but this installer neither uses nor disables it.

The local primitive is generic across sites and fleet sizes. It accepts one
canonical `spk_` node ID and absolute local inputs; it has no host name, IP,
SSH user, fleet count, or site default. It performs no network request.

## Installed layout and authority

The stable supervisor is installed outside both replaceable slots:

```text
/usr/libexec/dgx-agent-supervisor
/opt/dgx-forge/agent-slots/A/dgx-forge-agent
/opt/dgx-forge/agent-slots/B/dgx-forge-agent
/var/lib/dgx-forge-agent-supervisor/state.json
/var/lib/dgx-forge-agent-supervisor/supervisor.lock
/run/dgx-forge-agent/readiness.json
```

Slot, supervisor, policy, configuration, CA, TUF bootstrap, ORAS, NVIDIA, and
collector roots are root-owned and not group/world writable. Slot executables
are root-owned regular single-link non-symlinks. Agent state, credential state,
registry authentication, release staging, TUF caches, and the runtime directory
are owned by the dedicated `dgx-agent` account. That account has no login
shell, Docker group, Docker socket, ambient capabilities, or write access to
executables or policy.

The agent artifact may be an architecture-independent Python executable with
the exact `/usr/bin/python3` shebang or an ELF executable for the detected
supported architecture. ORAS must be an ELF executable for the policy
architecture. The installer can validate an ARM64 artifact and policy on an
x86_64 test host without executing it.

## Split supervisor model

`dgx-forge-agent.service` is visibly and effectively `User=dgx-agent`. Its
fixed `ExecStart` calls the stable supervisor in `run-agent` mode with no
caller-selected path. That mode takes a shared process lock, strictly reads
the root-owned state, descriptor-opens the selected slot with `O_NOFOLLOW`,
checks owner, exact mode, link count, type, architecture, size, and SHA-256,
then executes the verified descriptor. It closes the lock before execution
and never writes supervisor state, slots, policy, or installed artifacts.

`dgx-forge-agent-supervisor.service` runs the same stable artifact in
`supervise` mode as root. It performs only selection, activation, readiness,
rollback, and fixed `systemctl` coordination. It validates the current slot
before starting the agent unit. During an activation it increments a bounded
boot-attempt counter, restarts the agent unit, and waits only until the stored
deadline. Agent-unit failure, digest or executable substitution, attempt
exhaustion, or missed readiness causes one rollback to a freshly verified
previous slot. Failure to verify either slot exits for explicit recovery and
does not choose a slot or loop.

The fixed later-update interface is:

```text
dgx-agent-supervisor activate --slot A|B --sha256 <64-lowercase-hex>
```

It accepts no executable path, command, shell, environment, deadline, or
repository argument. It requires root, accepts only the inactive slot, verifies
that slot before state mutation, uses a compiled activation window, persists a
new generation, and restarts the fixed supervisor unit. Task 5 also provides
an installer-only `initialize` command with a slot and digest, and path-free
`run-agent` and `supervise` commands. The later signed update task is
responsible for placing a verified artifact in the inactive fixed slot before
calling `activate`.

## Canonical state and publication

State is duplicate-free canonical UTF-8 JSON capped at 16 KiB. The exact
schema carries:

- `schema_version` and monotonically increasing `generation`;
- `active_slot` and nullable `previous_slot`;
- `slot_sha256` for both fixed slots;
- `expected_sha256` for the selected slot;
- nullable UTC `activation_deadline`;
- `boot_attempts`, activation `status`, and `rollback_performed`.

Every read traverses trusted ancestry and opens a regular single-link file
relative to a directory descriptor. A process-safe `flock` permits one writer
and compatible readers. Publication creates a random new file with
`O_CREAT|O_EXCL|O_NOFOLLOW`, writes canonical bytes fully, fsyncs the file,
atomically renames it over `state.json`, and fsyncs the state directory. A
crash hook at each boundary supports deterministic restart tests. Unknown,
missing, extra, noncanonical, oversized, or corrupt state fails closed.

Initialization publishes slot A only after its executable is verified.
Activation first verifies the inactive slot and the current previous slot,
then publishes pending state without modifying the old slot. Rollback is
represented durably before the old slot is restarted. Once rollback has been
recorded for a generation, restart may ensure that exact old slot is running
but cannot roll back a second time or select another executable.

## Readiness handshake

`run-agent` exports only the verified activation generation, slot, and digest
to the replaceable process. The agent emits readiness only after a successful
authenticated outbound control exchange. It writes canonical JSON containing
exactly `schema_version`, `generation`, `slot`, and `sha256` to the
agent-writable `/run/dgx-forge-agent` directory using a no-clobber temporary
file, file fsync, atomic rename, and directory fsync.

The root coordinator descriptor-opens `readiness.json` and accepts only a
bounded regular single-link file owned by the `dgx-agent` UID with exact
`0600` mode and canonical duplicate-free JSON. All four values must equal the
pending state. A marker from an old generation, other slot, or other digest is
stale and is removed without satisfying readiness. File existence alone is
never success. Stable boots do not require a marker.

## Networkless installer

`nodes/bin/install-dgx-agent` is a Python 3 standard-library installation-mode
primitive. Production mutation requires UID 0. Its CLI takes absolute paths
for the agent executable and digest, ORAS executable and digest, exact NVIDIA
ZIP, health collector and digest, site configuration, CA, TUF bootstrap root
and digest, registry authentication, and one-time enrollment token, plus the
canonical node ID. Digests are explicit except for the compiled exact NVIDIA
archive digest. Test-root and command substitutions are honored only for an
unprivileged test process and are refused by a root production invocation.

Before mutation, the installer opens and snapshots every input. It rejects
symlinks, non-regular files, devices, FIFOs, hardlinks, wrong ownership or
mode, excessive size, digest mismatch, unsupported architecture, mutable file
substitution, malformed or duplicate JSON, and unsafe ancestry. The site
document contains only canonical origins, repository, architecture, polling,
and fabric pairs; paths in installed policy are generated by the installer.

A nonblocking process lock serializes installers. All staging lives on the
destination filesystem and uses unpredictable no-clobber names. Immutable
digest directories are complete and fsynced before rename. Existing matching
content makes reinstall a no-op; conflicting content fails closed. Mutable
configuration is published only after all immutable dependencies and private
bootstrap material are durable. Cleanup is bounded to names and inode
identities created by the current transaction. Re-running after a crash
converges from every publication boundary.

The installer creates or validates the `dgx-agent` account, fixed roots,
systemd units, stable supervisor, initial A slot, and initial state. It copies
the public CA, TUF root, private registry auth, and one-time token with their
exact destination ownership and modes. It does not copy an admin credential,
CA private key, SSH private key, reusable bootstrap secret, old node private
key, or any repository-selected executable argument. The token remains only
at the restrictive bootstrap path until exact certificate pickup is durably
published, after which the accepted enrollment lifecycle unlinks and fsyncs
it. A fresh node key is generated locally.

## NVIDIA and installed policies

The committed NVIDIA lock identifies exactly:

- `enterprise-lifecycle-integration-scripts-20260520-1602.zip`;
- bundle version `0.1.0`;
- SHA-256 `0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3`;
- the immutable NVIDIA source URL;
- every archive member with normalized path, uncompressed size, external
  mode/type, and CRC;
- reviewed installed tools/support files with sizes, modes, and SHA-256;
- the MIT `LICENSE` digest and canonical `SOURCE.json` provenance digest.

The installer never fetches that URL. It rejects an archive with any missing,
extra, duplicated, traversing, absolute, device, FIFO, link, encrypted,
oversized, or lock-disagreeing member. It extracts only the reviewed seven
tools, four support modules, and license beneath
`/opt/dgx-forge/third-party/nvidia/<archive-sha256>`, writes canonical
`SOURCE.json`, and generates the exact installed policy consumed by
`InstalledPolicy.load`. Fixed paths, versions, hashes, arguments, deadlines,
output limits, CPU sampling, and site-supplied validated fabric pairs agree
with the compiled NVIDIA adapter. There is no PATH or mutable-bundle fallback.

A separate strict installed runtime policy contains the detected architecture,
canonical registry SNI origin and repository, ORAS 1.3.3 path and digest,
private registry auth path, TUF bootstrap path and digest, fixed metadata and
target caches, fixed release and same-filesystem staging roots, and exactly
the compiled `spark-runtime-v1` adapter contract. It is root-owned, canonical,
bounded, and descriptor-read. Missing or unsafe policy stops service startup
before polling.

## Production runtime construction and enrollment

`build_agent` loads and verifies both installed policies before constructing
the client. One `CredentialStore` instance is the active credential provider
for runtime HTTPS, dynamic TUF HTTPS snapshots, and every ORAS pull.
`TUFReleaseTrust` uses the installed bootstrap root and fixed control routes;
`ORASClient` uses the installed registry/repository and reviewed executable;
`ReleaseInstaller` receives fixed release/staging roots; and
`WorkloadOperations` receives the same trust boundary and compiled adapter
policy. The resulting `OperationContext` always has probe, release, and
workload handlers in production.

Before an active identity exists, startup uses the installed public CA,
one-time token, a locally generated Ed25519 key/CSR, fixed enrollment origin,
and bounded node evidence. Pending approval retries the identical durable CSR.
An issued certificate is validated against that key, durably installed as the
initial active credential, and only then causes token unlink plus bootstrap
directory fsync. No readiness marker is emitted by enrollment; readiness waits
for a subsequent authenticated runtime exchange.

## systemd confinement

The agent unit uses a fixed executable/configuration, `User=dgx-agent`,
`Group=dgx-agent`, `UMask=0077`, bounded restart/start limits, `NoNewPrivileges`,
an empty capability and ambient-capability set, private temporary storage,
strict home/system/kernel/control-group/clock/personality protections, closed
write/execute memory, private devices with required read-only GPU device
visibility, and explicit read-only/read-write paths. It admits only Unix,
IPv4, and IPv6 address families for local coordination and outbound HTTPS.
It has no Docker socket or broad device access.

The root supervisor unit has the same compatible kernel/filesystem hardening
where it does not prevent fixed systemd coordination, an empty capability set
except the minimum DAC/identity operations actually required, no network
families, `UMask=0077`, bounded starts, and explicit read/write state/runtime
paths. `systemd-analyze verify` must accept both units. Security exposure and
any directive unavailable on the supported DGX OS systemd version are recorded
instead of silently removed.

## Failure and test strategy

Strict RED/GREEN TDD covers the state parser and publication boundaries first,
then real launch/readiness/rollback processes, installer input and archive
boundaries, runtime-policy construction, enrollment cleanup, and effective
systemd properties. Each test names the production defect it catches and uses
literal expected bytes or externally calculated digests.

Supervisor tests exercise A-to-B success, absent/tampered/substituted/hardlinked
executables, stale and malformed markers, process failure, timeout, attempt
exhaustion, rollback restart, corrupt state, both slots invalid, concurrent
processes, descriptor cleanup, restart after each partial state publication,
and exact slot targets. Installer tests exercise two distinct node IDs,
idempotent and concurrent reinstall, every publish crash, wrong ownership/mode/
digest/architecture/policy, archive traversal/type/extra-member rejection,
input substitution, bounded cleanup, retained license/provenance, exact policy,
and absence of copied private administration material.

Runtime tests prove missing/unsafe policy fails before polling, production
handlers are non-null, all origins/paths come from installed input, one live
credential provider is shared, enrollment preserves then removes only the
one-time token, readiness follows authenticated exchange, and ARM64 policy is
validated without foreign execution. Final gates include focused and full
agent/node/protocol/control/Compose tests, Ruff and shell lint where present,
systemd verify/security, bytecode compilation, wheel build and fresh-wheel
smokes, installed console/supervisor smokes, supply-chain verification, and
`git diff --check`.
