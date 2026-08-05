# Task 4 brief: remove SSH from production worker wiring

Task 4 starts only after Tasks 2 and 3 are accepted on `main`. Task 3 must first
own durable reconciliation ticking, dependency waves, compensation, route
publication, restart recovery, and reconcile-parent terminalization.

## Boundary

Routine controller-to-Spark work uses only outbound, fenced, mTLS agent
operations. The production worker must not import or invoke `sparkctl`, SSH,
SCP, `SshBackend`, `ProfileSwitcher`, or `scripts/deploy-runtime-release`.

This does **not** disable SSH on a Spark. Preserve explicit operator
break-glass/recovery access, one-time onboarding and hardening, fabric recovery,
and host identity evidence. Preserve API-side Git SSH signing; it is not Spark
transport. The NAS devbox's inbound SSH service is also outside this boundary.

## Cutover contract

1. Move the current bounded direct runtime into `legacy_runtime.py`. It is
   selectable only in development/test with the exact value
   `DGX_LEGACY_DIRECT_TRANSPORT=explicit-test-only`; production rejects every
   non-empty selector and never falls back to it.
2. Production `worker.py` ticks Task 3's persisted reconciliation service and
   bounded housekeeping only. Probe work is a closed `node.probe` agent
   operation. Pre-agent queued jobs without durable reconciliation linkage enter
   `waiting-for-operator` with a bounded upgrade reason.
3. Split worker settings from API settings. The worker receives no repository,
   Git signing, CA private-key, SSH alias, or legacy transport configuration.
4. Build a separate pinned worker image without Git, OpenSSH, the repository,
   `sparkctl`, or deployment scripts. Keep Git/OpenSSH in the API image for
   signed repository administration.
5. Remove the worker repository mount, Git-key secret, and `cluster-egress`
   network. The worker uses internal database/application networks and Task 3's
   publication volume only. LiteLLM retains inference egress.
6. Presence supplies address/withdrawal evidence only. It never becomes desired
   state or publication authority.

## Files

- Modify `control/src/dgx_control/worker.py`, `settings.py`, `control/Dockerfile`,
  Compose, supply-chain checks, and worker/settings/network tests.
- Create `legacy_runtime.py`, `test_production_worker.py`,
  `security/test_no_routine_ssh.py`, and explicit legacy-runtime tests.
- Thin or remove production behavior from `runtime.py`.
- Do not change the ordinary CLI in this task; Unified Admin Clients Task 3
  moves routine commands to the API and leaves direct behavior under
  `sparkctl-legacy`.

## Acceptance

- Reconciliation/probe tests patch all subprocess, SSH, legacy, and transport
  selectors to raise; routine work still proceeds through agent operations.
- Offline, revoked, incompatible, stale-fence, bad-digest, and bad-evidence cases
  stay withdrawn/nonterminal or wait for an operator with zero legacy calls.
- The first completed dependency wave cannot complete the parent; a complete
  graph can, and restart creates no duplicate mutation.
- Static import/AST checks find no direct transport in production worker code.
- Rendered Compose proves the worker lacks repository, signing secret, and
  cluster egress while the API retains signing and lacks cluster egress.
- The built worker image has no `ssh`, `scp`, `git`, or `sparkctl` executable.
- Task 2/3 lifecycle, PostgreSQL race, Compose, full control, and hosted
  macOS/Ubuntu gates remain green; Task 4 creates no migration after `0009`.

The NAS branch overlaps heavily in worker/runtime/settings/Docker/Compose. Port
only Task 3-approved address-policy, lease, supervisor, and atomic-publication
primitives; never merge its synchronous SSH/presence-driven worker wholesale.
