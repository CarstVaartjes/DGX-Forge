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
  directory descriptor for the receipt and full tree and rechecks the pathname
  identity before reporting idempotent success;
- two ORAS attacks failed before the fix: credential arguments exposed mutable
  paths, and path/same-inode mutations changed the bytes seen by ORAS;
- ProcessRequest initially accepted duplicate and reserved auxiliary FDs;
- compiled adapter inspection initially failed during the monotonic deadline
  change, exposing and fixing the recovery deadline/binding contract;
- added signed-fixture RED cases drove threshold, rollback/freeze,
  mix-and-match, rotation, delegation, cache, interrupted-refresh, fsync, and
  oversized-target handling;
- Compose/Caddy tests failed until the fourth distinct registry SNI, private
  networks, anchored digest routes, and publisher validation were complete.

Final focused release/workload/operation coverage is 83 passing tests; the
brief's exact release/workload command contains 71 passing tests, and the final
complete agent suite contains 304 passing tests.

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
64 KiB re-verification bound.

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
- Accepted metadata is hardened and fsynced even if a later refresh or target
  stage fails. Final metadata and target roots are fsynced before authorization
  returns.
- python-tuf downloads the target into a pre-opened memfd through
  `/proc/self/fd`; the descriptor is immediately write-sealed before parsing.
  The persistent target cache must remain empty, eliminating symlink/FIFO/
  hardlink overwrite targets.
- ORAS policy pins canonical HTTPS origin/repository and version 1.3.3. The
  executable and four auth/CA/client inputs are sealed snapshots. The child
  receives only digest references and child-only `/proc/self/fd` paths through
  the bounded process supervisor.
- One monotonic deadline is bound before new state begins and is threaded
  through trust, lock acquisition, transport, verification, fsync, rename, and
  adapter launch. Recovery inspection uses a separate compiled local budget;
  an expired mutation can be inspected/completed but cannot be retried.
- Install uses a same-filesystem private staging directory, exact member/tree
  verification, receipt re-verification, file/directory fsync, a per-release
  bounded lock, Linux `renameat2(RENAME_NOREPLACE)`, and inode-bound cleanup.
  Verification, receipt, and fsync use one held staging dirfd; publication
  rechecks the source identity and then requires the destination to be that
  same inode and pass full verification before success.
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
304 passed in 8.44s

uv run --project agent pytest \
  agent/tests/test_releases.py agent/tests/test_workloads.py -v
71 passed in 1.41s

uv run pytest deploy/compose/tests -q
21 passed in 7.46s

uv run --project agent python -m compileall -q agent/src
exit 0

uv build --project agent
Successfully built agent/dist/dgx_agent-0.1.0.tar.gz
Successfully built agent/dist/dgx_agent-0.1.0-py3-none-any.whl

docker compose -f deploy/compose/compose.yaml \
  -f deploy/compose/compose.step-ca.yaml config --quiet
exit 0

git diff --check
exit 0
```

A fresh temporary virtual environment installed the built agent wheel together
with the committed protocol wheel, resolved `tuf==7.0.0`, and imported all
Task 1–3 production modules. It also parsed the exact release request and each
of the five typed workload request shapes from the installed wheel. Output:
`fresh-wheel-imports-and-typed-execution-ok`.

The final independent re-review found no remaining Critical or Important
issues. Its targeted verification included all 54 release tests and the
deterministic destination-swap regression; verdict: Ready.

## Remaining physical and later-task gates

- Task 5 must install the bootstrap root, mTLS/auth files, reviewed ORAS 1.3.3
  executable and digest, cache/release roots, service account, and systemd
  confinement with production ownership.
- Deployment operators must select and approve a digest-pinned ORAS publisher
  image. The wrapper command shape is tested; a live production credentialed
  publish is an environment/physical acceptance gate.
- Long polling, supervisor lifecycle, agent update/rollback, simulator, and
  control-plane orchestration remain assigned to later tasks.
