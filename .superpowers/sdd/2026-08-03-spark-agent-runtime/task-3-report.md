# Task 3 report — trusted releases and fixed workload operations

## Outcome and scope

Task 3 is implemented on accepted base
`c66b544a338651602cfa61441b10f263fcc9a2c7`. The agent now compiles
`release.install` and the five workload operations present in the accepted
protocol enum: `workload.prepare`, `workload.start`, `workload.stop`,
`workload.health`, and `workload.verify`. The brief's reference to “six”
`workload.*` operations was reconciled against the accepted enum; no new
protocol operation was invented.

Later-task long polling, systemd/supervisor installation, simulator,
control-plane reconciliation, agent self-update/rollback, and platform-wide
orchestration were not implemented.

## TDD evidence

The accepted agent baseline was 227 passing tests. Development used focused
RED/GREEN slices:

- typed release/workload imports first failed because the new modules and
  compiled adapter types did not exist;
- three installer attacks failed before the fix: sparse content was accepted,
  a dangling destination reached transport, and concurrent same-release pulls
  raced;
- a deterministic same-UID destination swap initially combined a receipt from
  one inode with a tree from another; installed verification now holds one
  releases-parent descriptor and one destination descriptor for the receipt and
  full tree, then rechecks the destination name through that same parent before
  reporting idempotent success; the initial-existing, publish-race, and inspect
  branches each have deterministic substitution coverage;
- two ORAS attacks failed before the fix: credential arguments exposed mutable
  paths, and path/same-inode mutations changed the bytes seen by ORAS;
- external review round 1 reproduced an unusable production credential policy:
  private service-owned `0600` auth/key metadata was rejected unless the test
  seam was enabled; production-mode metadata and full snapshot regressions now
  exercise direct service-UID reads below simulated root-controlled ancestry;
- ten deterministic review regressions initially failed for non-canonical or
  duplicate receipts, missing release/TUF traversal deadlines, missing workload
  deadline propagation, and unbounded error persistence; two further RED cases
  proved that naive elapsed checks skipped required parent fsync after release
  and marker publication;
- external review round 2 produced ten RED regressions for work continuing
  inside python-tuf and the ORAS executable snapshot after expiry, recursive
  cleanup after expiry, orphaned staging state, and path-reopened publication
  parents. The final suite additionally covers updater-constructor root
  validation, per-thread trace restoration and two-thread isolation, durable
  restart recovery/crash windows, corrupt and substituted recovery state,
  recovery-aware inspection, exact-inode sidecar deletion, and empty-directory
  substitution at the final cleanup step;
- external review round 3 produced 41 RED regressions for remaining local
  ORAS/TUF setup work, every staging-ownership syscall boundary, public error
  typing, missing/replaced recovery intents, regular/symlink/hardlink leaf
  substitution, and durable complete/unsafe recovery temporary records. Repeated
  ancestry expiry additionally proves descriptor counts remain stable;
- external review round 4 produced 13 RED regressions for raw public installer
  setup filesystem errors, destination and reservation rescans, aggregate
  persisted-plus-active reservation races, and recovery-budget expiry during
  quarantine token generation. The public boundary now preserves existing
  typed trust/validation failures while converting applicable setup OSErrors,
  closing every acquired descriptor/lock, and stopping reservation/recovery
  mutation on the claim or recovery deadline;
- ProcessRequest initially accepted duplicate and reserved auxiliary FDs;
- compiled adapter inspection initially failed during the monotonic deadline
  change, exposing and fixing the recovery deadline/binding contract;
- added signed-fixture RED cases drove threshold, rollback/freeze,
  mix-and-match, rotation, delegation, cache, interrupted-refresh, fsync, and
  oversized-target handling;
- Compose/Caddy tests failed until the fourth distinct registry SNI, private
  networks, anchored digest routes, and publisher validation were complete.

The release-only command now contains 149 passing tests. The brief's exact
release/workload command contains 167 passing tests; the
release/workload/operation command contains 182 tests, and the complete agent
suite contains 400 tests.

## Exact typed contracts

### Release claim

`ReleaseRequest` is frozen and accepts only:

```text
schema_version: 1
target_name: canonical lower-case token
oci_manifest_digest: sha256:<64 lower-case hex>
target_digest: <64 lower-case hex>
provenance_digest: <64 lower-case hex>
adapter_id: canonical lower-case token
```

The claim cannot select origins, repositories, paths, credentials, commands,
arguments, imports, environments, timeouts, or working directories.

### Signed TUF target

The target body and `custom.release` must be identical strict descriptors with:

```text
schema_version, target_name, target_digest, target_length,
registry_origin, repository, oci_manifest_digest, provenance_digest,
adapter_id, adapter_version, architecture,
agent_min_version, agent_max_version,
protocol_min_version, protocol_max_version, members
```

Each member has exactly `path`, `sha256`, `size`, `mode`, `uid`, and `gid`.
Members are sorted and canonical, modes are only `0400` or `0500`, aggregate
length equals `target_length`, and the canonical install receipt must fit the
64 KiB re-verification bound. Installed receipts reject duplicate keys and
must byte-for-byte equal the compact, sorted canonical receipt with exactly one
trailing newline.

### Workload claims and adapter ABI

Every workload claim has `schema_version`, `workload_id`, `release_digest`,
and `adapter_id`, plus exactly one operation field where required:

- prepare: `profile_digest`
- start: `preparation_digest`
- stop: no additional field
- health: no additional field
- verify: `expected_digest`

Production adapter resolution is source-closed to
`spark-runtime-v1 -> bin/runtime-adapter`. Execution argv is compiled per
operation. Trusted claim `job_id`, `operation_id`, `attempt`, and `fence` are
added outside payload authority. The adapter must echo those exact values in
its typed execution result. Mutating inspection receives the same binding and
must return the binding recorded with observed state; omissions and stale or
cross-operation values fail closed. Result JSON rejects duplicate/extra keys,
and statuses/dispositions use closed operation-specific vocabularies.

## Trust, transport, and filesystem design

- `tuf==7.0.0` and `securesystemslib[crypto]==1.4.0` are exact locked pins.
- A fresh `tuf.ngclient.Updater` runs under one nonblocking cache lock.
  Bootstrap trust is marked established only after cached root content and
  directories are hardened and descriptor-fsynced. The marker binds the exact
  latest cached-root digest. A missing pointer is repaired only from matching
  root-history bytes; loss of an established root fails operator-closed and
  never falls back to the older installed bootstrap.
- The HTTPS fetcher accepts one canonical control origin and exact metadata
  and target routes, has no proxy/netrc/environment path, no redirect/retry,
  bounded connect/read intervals, and one claim-derived monotonic deadline.
- Real signed fixtures exercise valid authorization, role thresholds,
  expiration/freeze, rollback, snapshot mix-and-match, target hashes,
  valid/invalid root rotation, delegated lookup/thresholds, cache corruption,
  concurrency, interrupted refresh/recovery, and fsync failures.
- Accepted metadata is hardened and fsynced after refresh and target work while
  budget remains. Error-path persistence receives the same deadline and stops
  before starting additional traversal once elapsed; any partial cache remains
  fail-closed under the next cache validation. Authority-bearing marker and
  root-pointer replacements always receive their mandatory parent fsync before
  an elapsed error is propagated.
- python-tuf downloads the target into a pre-opened memfd through
  `/proc/self/fd`; the descriptor is immediately write-sealed before parsing.
  The persistent target cache must remain empty, eliminating symlink/FIFO/
  hardlink overwrite targets.
- ORAS policy pins canonical HTTPS origin/repository and version 1.3.3. In
  production, private auth/key inputs must be owned by the running dedicated
  service UID with exact mode `0600`; public CA/certificate inputs remain
  root-owned and non-writable by group/other. Every ancestor remains root-owned
  and non-writable, and the reviewed executable remains root-owned and
  digest-pinned. All four inputs become sealed snapshots. The child receives
  only digest references and child-only `/proc/self/fd` paths through the
  bounded process supervisor.
- One monotonic deadline is bound before new state begins and is threaded
  through trust, cache/error persistence, chunked reads, member/deep-tree walks,
  receipt verification, fsync, workload re-verification, transport, rename,
  and adapter launch. Checks occur before and immediately after each potentially
  blocking step. A syscall cannot be preempted in-process, so deadline overrun
  is bounded to the one syscall already in progress; no next chunk/member starts.
  The fixed deadline callback also protects the ORAS executable snapshot loop.
  Executable ancestry traversal, leaf open/stat, hash construction, and memfd
  creation each check before and after their boundary. Updater construction,
  refresh, target lookup/download, signed custom parsing, target JSON parsing,
  and descriptor parsing execute under a per-thread Python trace guard, while
  target memfd creation is checked on both sides. Thus python-tuf
  validation/hash/copy/parse work is interrupted at Python line boundaries
  without replacing another thread's trace state; each call restores the prior
  trace in `finally`.
  After an irreversible release/marker/root-pointer rename, the one mandatory
  parent fsync completes before elapsed is reported. Recovery inspection uses a
  separate compiled local budget; an expired mutation can be inspected/completed
  but cannot be retried.
- Install uses a same-filesystem private staging directory, exact member/tree
  verification, receipt re-verification, file/directory fsync, a per-release
  bounded lock, Linux `renameat2(RENAME_NOREPLACE)`, and inode-bound cleanup.
  Verification, receipt, and fsync use one held staging dirfd; publication
  rechecks the source identity and then requires the destination to be that
  same inode and pass full verification before success.
  Releases, staging, and metadata publication hold their exact parent dirfds;
  rename/replace and durability fsync are relative to those descriptors. Staging
  ownership begins with a canonical `O_EXCL|O_NOFOLLOW` intent sidecar and is
  completed with the captured staging device/inode. Every intent/temp
  open/write/fsync/stat, parent fsync, directory mkdir/open/stat, completion
  rename, and chmod checks the claim deadline immediately before and after, so
  only the one syscall already crossing the deadline may finish. Completion
  first atomically quarantines and verifies the captured intent inode and uses
  no-replace promotion, so a missing or substituted intent is never overwritten.
  Expired requests never recurse: a fresh installer or recovery inspection
  reaps authenticated state under an independent 100 ms budget. Complete
  canonical `.new` records left across the intent-removal/promotion crash window
  are matched to the exact staging inode and promoted without replacement;
  corrupt or substituted temporaries remain fail-closed without deletion.
  Incomplete intents quarantine but do not delete an unproven same-UID inode.
  Sidecar deletion, leaf deletion (regular, symlink, or hardlink), and final
  empty-directory removal all use atomic quarantine plus exact-inode/type
  verification. Final-tree quarantine is token-bound and resumed on restart;
  canonical recovery-record quarantines are exact-inode-removed on fresh
  preflight, with corrupt/symlink substitutions preserved fail-closed. Leaf,
  final-tree, legacy `.remove-*`, and any unsafe quarantine artifacts are all
  counted with active reservations against one aggregate recovery bound rather
  than becoming hidden after canonical sidecar removal.
  Reservation performs a second descriptor-relative aggregate scan while
  holding the installer recovery lock and charging the prospective active
  reservation. A concurrent reservation is rejected while staging is active,
  so persisted artifacts plus active work never exceed 16 and an active staging
  tree is never reaped by another install. The rescan uses the original claim
  deadline. Every recovery-generated token is followed by a budget check before
  its first rename or other mutation.
- Workload launch reauthorizes the receipt-derived release through TUF on every
  operation, requires exact signed-descriptor equality, verifies receipt/tree
  through one pinned release dirfd, then executes a write-sealed adapter memfd.

## Compose registry boundary

Distribution uses the reviewed multi-platform index:

```text
registry:3.1.1@sha256:1be55279f18a2fe1a74edf2664cac61c1bea305b7b4642dab412e7affdcb3e33
```

It has no published port, persistent storage, bounded logging, a healthcheck,
deletion disabled, no debug endpoint, no credential environment, and two
internal networks. Only Caddy shares the agent-facing `registry-edge` network.
The distinct registry SNI requires agent mTLS and proxies only `GET`/`HEAD`
for `/v2/` and anchored canonical-repository manifest/blob paths ending in an
exact lower-case SHA-256 digest. Other Registry v2 requests receive 405.

Operator publication is a non-standing Docker-admin path on the separate
internal `registry-publisher` network. The wrapper validates the explicit
Compose project name, release tag, absolute source directory, and an exact
digest-pinned publisher image before running ORAS from `/release` with the
relative input `.`. Agents receive no publisher credentials or route.

## File inventory

Production additions/changes include:

- `agent/src/dgx_agent/{deadlines,releases,update_trust,oci,workloads}.py`
- `agent/src/dgx_agent/{operations,probe,_probe_supervisor}.py`
- `agent/pyproject.toml`, `agent/uv.lock`
- `deploy/compose/compose.yaml`, `Caddyfile`, `.env.example`,
  `images.lock.json`, and `caddy/entrypoint.sh`
- `deploy/compose/registry/config.yml` and `registry/README.md`
- `deploy/compose/bin/publish-release`

Tests include the new release, workload, and registry suites plus operation,
process-supervisor, networking, and ingress regressions.

## Final verification

Executed on the final working tree:

```text
uv run --project agent pytest agent/tests -q
400 passed in 10.01s

uv run --project agent pytest agent/tests/test_releases.py -q
149 passed in 1.84s

uv run --project agent pytest \
  agent/tests/test_releases.py agent/tests/test_workloads.py -v
167 passed in 2.65s

uv run --project agent pytest agent/tests/test_releases.py \
  agent/tests/test_workloads.py agent/tests/test_operations.py -v
182 passed in 3.08s

uv run pytest deploy/compose/tests -q
21 passed in 8.00s

uv run --project agent python -m compileall -q agent/src
exit 0

uv build --project agent
Successfully built agent/dist/dgx_agent-0.1.0.tar.gz
Successfully built agent/dist/dgx_agent-0.1.0-py3-none-any.whl

docker compose --env-file deploy/compose/tests/test.env \
  -f deploy/compose/compose.yaml \
  -f deploy/compose/compose.step-ca.yaml config --quiet
exit 0

uvx --from ruff==0.16.1 ruff check \
  agent/src/dgx_agent/nvidia_tools.py \
  agent/src/dgx_agent/update_trust.py \
  agent/src/dgx_agent/releases.py agent/tests/test_releases.py
All checks passed!

git diff --check
exit 0
```

The repository-root aggregate invocation is not recorded as a passing Task 3
gate: on this accepted worktree it reproducibly segfaults during pytest
collection in the unrelated Spark-profile catalog's recursive
`jsonschema.validate` path (`tests/spark_profiles/test_cli.py`), before Task 3
tests execute. The isolated agent, focused Task 3, and Compose suites above run
in fresh processes and are authoritative for this change. Repository-wide Ruff
likewise reports pre-existing findings outside the changed files; Ruff passes
for every Python file changed by this round.

A fresh temporary virtual environment installed the built agent wheel together
with the committed protocol wheel, resolved `tuf==7.0.0`, and imported all
Task 1–3 production modules. It also parsed the exact release request and each
of the five typed workload request shapes from the installed wheel. Output:
`fresh-wheel-imports-and-typed-parsing-ok`.

The earlier internal targeted re-review found the inode-publication fix Ready.
Exact-range external review round 1 subsequently returned Not Ready with two
Important findings (production private-credential ownership and end-to-end
deadline threading) plus one canonical-receipt Minor. This follow-up addresses
those submitted findings. External review round 2 returned Not Ready with three
Important findings covering internal library/snapshot deadlines, expired
staging recovery, and descriptor-bound publication. This follow-up addresses
all three. External review round 3 returned Not Ready with six Important
findings and one Compose-command Minor. This follow-up addresses the submitted
deadline, typed recovery, exact-inode completion/leaf cleanup, durable temporary
recovery, and reproducible Compose-environment findings and awaits exact-range
external re-review.
External review round 4 returned three Important findings covering public
installer setup error typing, race-free aggregate persisted/active recovery
bounds, and a missing post-token recovery-budget check. This follow-up adds
deterministic RED/GREEN coverage and addresses all three findings; exact-range
external re-review remains pending.

## Remaining physical and later-task gates

- Task 5 must create root-owned, non-group/world-writable ancestry; install the
  reviewed ORAS 1.3.3 executable as root-owned executable content (recommended
  `0755`) and CA/client certificate as root-owned `0644`; install registry auth
  and client key owned by the dedicated agent service UID with exact `0600`.
  It must also install the bootstrap root and digest, cache/release roots,
  service account, and systemd confinement with those production identities.
- Deployment operators must select and approve a digest-pinned ORAS publisher
  image. The wrapper command shape is tested; a live production credentialed
  publish is an environment/physical acceptance gate.
- Long polling, supervisor lifecycle, agent update/rollback, simulator, and
  control-plane orchestration remain assigned to later tasks.
