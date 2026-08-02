# Mia DeepSeek Flash 0731 Dual-Spark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` task by task. Work directly on
> `main` by explicit user instruction; do not create branches or worktrees.
> Only one implementation agent edits shared files at a time.

**Goal:** Make `sparkctl prepare agent-full-dual` and
`sparkctl switch default` safely prepare and run the pinned MiaAI-Lab
DeepSeek-V4-Flash-0731 TP=2 runtime across both DGX Sparks.

**Architecture:** `sparkctl` remains the sole cross-node orchestrator. A small,
content-addressed node-local adapter controls one pinned Compose service per
node. Runtime dependencies live in Docker; checkpoints, caches, outputs, and
logs live under `/srv/models` bind mounts.

**Approved design:**
[Mia DeepSeek dual runtime](../specs/2026-08-02-mia-deepseek-dual-runtime-design.md)

## Immutable inputs

- Mia source: `b131b2a22164675890dd1465fd8862b5cfb6ff13`
- Image: `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`
- Model revision: `9e165c30e2704aec5d9d593cce3eebd58bbef1cb`
- API: `127.0.0.1:8888`
- Start: Spark 2 worker, then Spark 1 head
- Stop: Spark 1 head, then Spark 2 worker
- Container restart: `no`

## Task 1: Reconcile the planned definition and old DeepSeek plan

**Files:**

- Modify: `docs/superpowers/plans/2026-08-01-deepseek-0731-runtime.md`
- Modify: `config/workloads/deepseek-agent-dual.toml`
- Modify: `locks/model-definitions.toml`
- Modify: `inventory/reports/model-definitions.json`
- Modify: `docs/model-profile-overview.md`
- Modify: `tests/spark_profiles/test_contracts.py`

- [ ] Mark the old staged lane plan superseded while retaining its rationale.
- [ ] Change the Mia source pin from `914c35bd...` to `b131b2a...`.
- [ ] Regenerate the definition lock and planned maturity-index fingerprint in
  the same change; never leave the catalog internally inconsistent.
- [ ] Assert the audited source, image, checkpoint, topology, order, and
  loopback endpoint in tests.
- [ ] Keep maturity `planned`; configuration is not installation evidence.
- [ ] Run the focused contract tests and `git diff --check`.

## Task 2: Fingerprint releases, deadlines, and maturity evidence

**Files:**

- Modify: `src/spark_profiles/contracts.py`
- Modify: `src/spark_profiles/catalog.py`
- Modify: `src/spark_profiles/switcher.py`
- Modify: both `workload.schema.json` copies
- Create: `schemas/model-definition-evidence.schema.json`
- Create: `src/spark_profiles/schemas/model-definition-evidence.schema.json`
- Modify: `pyproject.toml`
- Modify: `tests/spark_profiles/test_contracts.py`
- Modify: `tests/spark_profiles/test_catalog.py`
- Modify: `tests/spark_profiles/test_switcher.py`

- [ ] First write failing tests for missing/changed release artifacts, malformed
  deadlines, stage-specific evidence gates, mismatched fingerprints/pins, an
  invalid predecessor, and per-operation timeout selection.
- [ ] Add a runtime-manifest path/digest and deadlines for `prepare`, `verify`,
  `start`, `health`, `infer`, `stop`, and `verify-release`. Keep both contract
  blocks optional for existing planned definitions during this migration task;
  definitions that declare either block must validate it completely.
- [ ] Make catalog loading verify the manifest and every named repository file.
- [ ] Package and enforce the evidence schema before any maturity transition.
- [ ] Run focused tests, then the complete framework suite.

## Task 3: Build the expected checkpoint-manifest tool

**Files:**

- Create: `tools/model_manifest.py`
- Create: `tests/tools/test_model_manifest.py`
- Create: `manifests/deepseek-v4-flash-0731.json`
- Create: `docs/runbooks/model-cache.md`
- Modify: `config/workloads/deepseek-agent-dual.toml`

- [ ] Write fixture tests for revision mismatch, missing/changed shards,
  missing encoder, unsafe symlinks, and a valid complete snapshot.
- [ ] Fetch the pinned revision API with `?blobs=true`, require its top-level
  `sha`, and use `siblings[].lfs.sha256` plus `lfs.size` for LFS artifacts;
  `blobId` is provenance rather than a raw-file SHA-256.
- [ ] Parse the pinned weight index and require all 48 referenced shards. Build
  a complete 74-file snapshot manifest, including all small pinned files and
  `encoding/encoding_dsv4.py`; fetch and hash only non-LFS files during build.
- [ ] Make node verification offline, streaming, bounded-memory, and symlink-safe,
  using no-follow opens where supported and requiring regular files throughout
  the materialized snapshot rather than HF cache symlinks.
- [ ] Generate and review the real expected manifest and pin its digest.

## Task 4: Implement the role-scoped Mia Compose adapter

**Files:**

- Create: `adapters/deepseek/mia-vllm/compose.yaml`
- Create: `adapters/deepseek/mia-vllm/bin/mia-deepseek-dual`
- Create: `adapters/deepseek/mia-vllm/config/common.env`
- Create: `adapters/deepseek/mia-vllm/config/spark1.env`
- Create: `adapters/deepseek/mia-vllm/config/spark2.env`
- Create: `adapters/deepseek/mia-vllm/runtime-manifest.json`
- Create: `tests/adapters/test_mia_deepseek_dual.py`
- Modify: `config/workloads/deepseek-agent-dual.toml`

- [ ] Test both rendered roles before implementation: pinned image, host
  networking/IPC, shared memory, GPU/InfiniBand, memlock, mounts, offline mode,
  rank, restart policy, and loopback-only head bind.
- [ ] Assert exact Mia vLLM arguments: TP=2, PP=1, `mp`, 1M context, C6, 0.80
  utilization, `nvfp4_ds_mla`, probabilistic MTP=5, DeepSeek parsers,
  FlashInfer B12X, and default thinking `low`.
- [ ] Explicitly render and test the read-only snapshot mount at
  `/models/deepseek-ai/DeepSeek-V4-Flash-0731`, `DSPARK_MODEL` pointing to that
  local path, `DSPARK_ENCODING_FILE` pointing to its pinned encoder, and
  writable `VLLM_CACHE_ROOT` plus `FLASHINFER_WORKSPACE_BASE` below the separate
  runtime-cache mount.
- [ ] Generate hashed node env files from `inventory/cluster.toml`, including
  both fabric rails, HCAs, GID indices, MTU, roles, and rendezvous values.
- [ ] Implement `prepare|verify|start|health|infer|stop|verify-release` with an
  explicit role and no cross-node SSH.
- [ ] Reject wrong role/node, mutable tags, wildcard bind, online serving,
  autostart, wrong checkpoint/release, or conflicting runtimes.
- [ ] Generate the release manifest last and update the definition fingerprint.
- [ ] In the same change, make release/deadline blocks required for this Mia
  definition and regenerate its lock plus planned maturity-index fingerprint.

## Task 5: Add atomic runtime-release deployment

**Files:**

- Create: `scripts/deploy-runtime-release`
- Create: `tests/scripts/test_deploy_runtime_release.py`
- Create: `docs/runbooks/runtime-release.md`

- [ ] Test dry-run default, manifest verification, safe temporary paths,
  remote hash verification, atomic rename, identical idempotence, and refusal
  to replace different content.
- [ ] Transfer only manifest-listed files and install them at the absolute
  digest-qualified `/opt/spark/model-adapters/.../releases/<digest>/` path.
- [ ] Require `--apply`; never pull artifacts or start containers here.

## Task 6: Add durable `sparkctl prepare`

**Files:**

- Modify: `src/spark_profiles/switcher.py`
- Modify: `src/spark_profiles/cli.py`
- Modify: `tests/spark_profiles/test_switcher.py`
- Modify: `tests/spark_profiles/test_cli.py`
- Modify: `docs/runbooks/sparkctl.md`

- [ ] Test selector resolution, shared controller locking, refusal in non-stopped
  state, concurrent node preparation, deterministic reporting, resumability,
  absence/timeout failures, and no active-profile mutation.
- [ ] Implement `sparkctl prepare <selector>` and the 86,400-second deadline.
- [ ] Have the adapter start or reattach to a deterministic named preparation
  container with durable progress; SSH interruption must not kill the download.
- [ ] Re-running resumes the same fingerprint and refuses a different one.

## Task 7: Implement runtime quality and release gates

**Files:**

- Create: `adapters/deepseek/mia-vllm/validation/quality.py`
- Create: `adapters/deepseek/mia-vllm/validation/quality-fixtures.json`
- Modify: `adapters/deepseek/mia-vllm/bin/mia-deepseek-dual`
- Modify: `tests/adapters/test_mia_deepseek_dual.py`

- [ ] Add deterministic English, script, repetition, XML, streaming, reasoning,
  tool-call, and longer-than-411-token gates.
- [ ] `verify` proves exact release/image/checkpoint, offline readiness, GB10,
  fabric, memory/disk, role config, Compose, and no autostart/conflict.
- [ ] `health` proves the exact local rank; head also proves API/model identity
  and context/runtime pins from rendered config and logs.
- [ ] `infer head` writes structured bounded evidence; worker inference rejects.
- [ ] Record a boot-ID/release-qualified memory baseline before start and make
  `verify-release` enforce container/port absence plus 1 GiB recovery tolerance.

## Task 8: Complete local verification and review

- [ ] Run `uv run --with pytest --with jsonschema pytest -q`.
- [ ] Run `git diff --check` and Python compilation/static shell checks.
- [ ] Render and inspect both Compose roles.
- [ ] Confirm the definition remains planned/admission-blocked and local tests
  did not mutate either Spark.
- [ ] Run an independent whole-tree review and resolve every blocker.

## Task 9: Deploy and prepare both Sparks

**Evidence:**
`inventory/reports/model-definitions/deepseek-agent-dual-prepared.json`

- [ ] Recheck live node health, boot IDs, Docker/GPU, disk/RAM, `earlyoom`, both
  fabric rails, and absence of model containers.
- [ ] Deploy the immutable adapter release to both nodes with `--apply`.
- [ ] Run `sparkctl prepare agent-full-dual`; pull the pinned image and download
  approximately 155.4 GiB concurrently on both nodes with periodic progress
  updates.
- [ ] Offline-verify both snapshots and image digests.
- [ ] Record schema-valid evidence and advance only `planned -> prepared`.

## Task 10: Verify offline readiness

**Evidence:**
`inventory/reports/model-definitions/deepseek-agent-dual-verified.json`

- [ ] Invoke role-specific verification with serving stopped.
- [ ] Prove offline-only serving, loopback API, restart `no`, installed fabric
  config, and an exact rendered command.
- [ ] Record schema-valid evidence and advance only `prepared -> verified`.

## Task 11: Bring up and accept the runtime

**Evidence:**

- `inventory/reports/model-definitions/deepseek-agent-dual-accepted.json`
- `inventory/reports/accepted-cluster-profiles.json`

- [ ] Start worker first and head second; keep bounded logs.
- [ ] Run identity, quality, streaming, reasoning/tool, 411-token regression,
  context/concurrency, and three lifecycle-cycle gates.
- [ ] Require at least 35 tok/s single decode, 90 aggregate tok/s at C3, and
  1,500 input tok/s at 2K x1.
- [ ] Pass a 15-minute no-throttle run with at most 15% warmed decline.
- [ ] Stop head then worker; prove release and memory recovery.
- [ ] Reboot both nodes for the explicit no-autostart gate.
- [ ] Record exact accepted fingerprints; only then enable profile admission.

## Task 12: Document, verify, commit, and push

**Files:**

- Modify: `docs/model-profile-overview.md`
- Modify: `docs/installation-record.md`
- Modify: `docs/architecture-overview.md`
- Modify: `docs/runbooks/model-switching.md`
- Create: `docs/runbooks/mia-deepseek-agent-dual.md`

- [ ] Document exact pins, paths, preparation/upgrades, capacity/performance,
  stop/reboot/recovery, and SSH-tunnel local use.
- [ ] Re-run the complete test, static, link, and live-health suites.
- [ ] Review the final diff, commit directly to `main`, push, and prove
  `main == origin/main`.
