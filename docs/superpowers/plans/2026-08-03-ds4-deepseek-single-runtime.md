# DS4 DeepSeek Flash 0731 Single-Spark Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pinned, containerized, single-Spark DS4 serving lane for
DeepSeek-V4-Flash-0731 that preserves the public OpenAI model name `deepseek`
and can later share the cluster with creative workloads on Spark 2.

**Architecture:** Spark 1 runs the Entrpi DS4 v0.5.3 CUDA/GB10 fork against a
manifest-verified asymmetric Q2 0731 GGUF and its generation-matched DSpark
drafter. The model files remain on Spark 1 local NVMe and are mounted read-only
into a loopback-only container; scratch, derived-weight, and disk-KV data use
bounded workload-specific directories. The definition enters the catalog as
`planned`, then advances through the existing evidence chain only after the
digest-qualified runtime is built, deployed, prepared, and verified.

**Tech Stack:** Entrpi DS4 v0.5.3, CUDA 13.0.1, Docker Buildx and Compose,
GB10 `sm_121a`, GGUF/LFS SHA-256 manifests, Bash, Python 3.12, JSON Schema,
pytest, Ruff, and `sparkctl`.

## Global Constraints

- Pin runtime source `https://github.com/Entrpi/ds4.git` at tag `v0.5.3`,
  peeled commit `4ad370b4a338efe9723a386673c0e04f6e214108`.
- Record the audited Spark recipe
  `https://github.com/Entrpi/ds4-on-spark.git` at commit
  `185487ba5749a3c24a71ca81d1bc514c45f10dca`; do not execute its installer.
- Verify the source archive SHA-256
  `7db338d0a441fed36c5e4e7af44ff670e8bfe567e88d482f00ff6a3dc0e5dbe3`.
- Pin build image
  `nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:7d2f6a8c2071d911524f95061a0db363e24d27aa51ec831fcccf9e76eb72bc92`
  and runtime image
  `nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04@sha256:c3fde347d52d578c84fd644bc177bc7ec333feaf11550d990da4084d7612e4c7`.
- Build with `make cuda-spark`; the resulting binary must contain native
  `compute_121a`/`sm_121a` code and `DS4_CUDA_SPARK_HBM_CACHE=1`.
- Pin base repository `antirez/deepseek-v4-gguf` at revision
  `1cd7b564460821938add0475a60b942c409295e0` and file
  `DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf`,
  exactly `86,720,111,488` bytes with SHA-256
  `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0`.
- Pin drafter repository `bleysg/DeepSeek-V4-Flash-DSpark-drafter-GGUF` at
  revision `81c6fdd38f9582da45ba27f0ed7b63bcd3ea3b62` and file
  `DSpark-drafter-Q2K-Q8-0731.gguf`, exactly `6,971,241,504` bytes with
  SHA-256 `8fa269560dc76fd73e4233ad9b1938b5f65dd363381fd9b1a5c6183f7d12d686`.
- The single-Spark lane is Q2-imatrix plus DSpark, not MXFP4. The available
  0731 MXFP4 GGUF is `155,976,458,848` bytes, exceeds one Spark's visible
  memory, and is rejected by DS4 v0.5.3's loader. Keep it as a deferred
  research candidate, not a serving artifact.
- Do not set `DS4_CUDA_COPY_MODEL` or enable `DS4_MODEL_ANON_HUGE`; the
  production default is the mapped/registered no-copy path. Record the actual
  startup mode and fail verification if a full-copy fallback is observed.
- Set `DS4_NO_UPDATE_CHECK=1`. Serving must not perform outbound checks or
  downloads; preparation is the only networked artifact phase.
- Use `restart: "no"`, bind only `127.0.0.1:8888`, and never install a system
  or user autostart unit.
- The OpenAI model name is always `deepseek`. Clients never select `single`,
  `lite`, `DS4`, the quantization, or the physical Spark.
- Initial placement is fixed to Spark 1 as `single-exclusive`; Spark 2 remains
  idle. A Spark-2 copy is a separate content-addressed definition, not live
  migration.
- Start with a 32,768-token context. Larger context and co-residency are
  separate measured promotions; the claimed 1M model context is not an
  initial one-Spark admission limit.
- Performance fine-tuning, the 15-minute thermal gate, three lifecycle cycles,
  and reboot/no-autostart acceptance remain in the final cross-model
  optimization phase. This plan may finish at `verified`, never falsely at
  `accepted`.
- Work directly on `main`, as explicitly authorized by the repository owner,
  and commit and push each completed task.

---

### Task 1: Generalize Evidence and Release Deployment for One-Node Definitions

**Files:**
- Modify: `schemas/model-definition-evidence.schema.json`
- Modify: `src/spark_profiles/schemas/model-definition-evidence.schema.json`
- Modify: `src/spark_profiles/catalog.py`
- Modify: `scripts/deploy-runtime-release`
- Modify: `tests/spark_profiles/test_catalog.py`
- Modify: `tests/scripts/test_deploy_runtime_release.py`

**Interfaces:**
- Consumes: `WorkloadDefinition.nodes` as the authoritative node set.
- Produces: stage evidence whose node IDs exactly equal the definition's
  declared nodes, and a release deployer whose `Release.aliases` contains only
  those nodes' SSH aliases.

- [ ] **Step 1: Add failing one-node evidence tests**

Add catalog tests that construct a valid `single` definition on `spark1` and a
prepared/verified evidence chain containing only Spark 1. Assert that it loads.
Add negative cases for missing Spark 1, an extra Spark 2, and a single-node
report attached to the existing dual definition. Keep the existing Mia report
fixtures byte-for-byte valid.

- [ ] **Step 2: Add failing workload-scoped deployment tests**

Extend the deployer fixture with `nodes = ["spark1"]` and assert dry-run and
apply invoke only `dgx-spark-1`. Retain the distributed test that invokes
`dgx-spark-1` and `dgx-spark-2`.

- [ ] **Step 3: Run the focused tests and observe the failures**

Run:

```bash
uv run pytest tests/spark_profiles/test_catalog.py \
  tests/scripts/test_deploy_runtime_release.py -v
```

Expected: the one-node evidence schema rejects `nodes`, and the deployer still
targets both aliases.

- [ ] **Step 4: Implement topology-aware evidence validation**

Change both schema copies identically: `nodes` accepts one or two unique node
records. Preserve the existing exact distributed verified-gate object. Add a
second exact verified-gate object requiring only these boolean-true fields:
`offline`, `release`, `image`, `architecture`, `manifest`, `mmap`, and
`api_identity`. In `catalog.py`, compare the report node-ID set to
`set(definition.nodes)` and reject missing, duplicate, or extra nodes.

- [ ] **Step 5: Implement workload-scoped release deployment**

In `_load_release`, validate `workload["nodes"]` as a nonempty unique subset of
`{"spark1", "spark2"}`. Resolve aliases in declared-node order from
`inventory/cluster.toml`; do not enumerate both nodes unconditionally. Preserve
all existing alias safety checks and atomic deployment behavior.

- [ ] **Step 6: Verify schema synchronization and focused behavior**

Run:

```bash
cmp schemas/model-definition-evidence.schema.json \
  src/spark_profiles/schemas/model-definition-evidence.schema.json
uv run pytest tests/spark_profiles/test_catalog.py \
  tests/scripts/test_deploy_runtime_release.py -v
uv run ruff check src tests scripts
git diff --check
```

Expected: all commands pass.

- [ ] **Step 7: Commit and push**

```bash
git add schemas src/spark_profiles/schemas src/spark_profiles/catalog.py \
  scripts/deploy-runtime-release tests/spark_profiles/test_catalog.py \
  tests/scripts/test_deploy_runtime_release.py
git commit -m "feat: support single-node runtime evidence"
git push origin main
```

### Task 2: Record the DS4 Audit and Verify the Two-Artifact Checkpoint

**Files:**
- Create: `docs/audits/ds4-v0.5.3.md`
- Create: `adapters/deepseek/ds4/manifests/deepseek-v4-flash-0731-ds4.json`
- Create: `adapters/deepseek/ds4/tools/artifact_manifest.py`
- Create: `tests/adapters/test_ds4_artifact_manifest.py`
- Modify: `docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md`
- Modify: `docs/model-capacity-overview.md`
- Modify: `docs/model-profile-overview.md`
- Modify: `tests/spark_profiles/test_contracts.py`

**Interfaces:**
- Produces: `artifact_manifest.py verify --manifest PATH --root DIRECTORY`,
  which streams both files in 8 MiB chunks and exits nonzero on any filename,
  size, or SHA-256 mismatch.
- Produces: a checked manifest containing `schema_version`, `artifacts`, and
  exact repository/revision/path/size/SHA-256 data for the base and drafter.

- [ ] **Step 1: Write failing manifest-verifier tests**

Cover a valid two-file root, a missing drafter, a wrong base size, a changed
base digest, an absolute artifact path, a `..` path, duplicate paths, unknown
JSON keys, and a symlink in place of either model file.

- [ ] **Step 2: Run the tests and observe the import failure**

```bash
uv run pytest tests/adapters/test_ds4_artifact_manifest.py -v
```

Expected: FAIL because the verifier does not exist.

- [ ] **Step 3: Implement the strict verifier and checked manifest**

Use only the Python standard library. Refuse symlinks and non-regular files,
check size before hashing, hash every artifact even after an earlier mismatch,
and emit a deterministic JSON report. The manifest names exactly the two pins
from Global Constraints and records total bytes `93,691,352,992`.

- [ ] **Step 4: Write the immutable upstream audit**

Document the three source pins, MIT licenses, mutable-download problems in the
upstream installer, the supported CUDA build, writable paths, plaintext disk-KV
risk, absent HTTP authentication, disabled update check, and the model-alias
patch requirement. Include the evidence that GGUF type 39 MXFP4 is rejected by
the v0.5.3 loader and does not fit one Spark.

- [ ] **Step 5: Reconcile current design and overview wording**

Replace `DS4 Flash 0731 MXFP4 candidate` with the audited Q2-imatrix + DSpark
definition. State explicitly that MXFP4 remains deferred until both loader
support and measured one-Spark admission exist. Replace the obsolete
`DS4_CUDA_COPY_MODEL` design assumption with the mapped/registered no-copy
contract while retaining the prohibition on full-copy startup.

- [ ] **Step 6: Verify documentation contracts and manifest behavior**

```bash
uv run pytest tests/adapters/test_ds4_artifact_manifest.py \
  tests/spark_profiles/test_contracts.py -v
uv run ruff check adapters/deepseek/ds4/tools tests
git diff --check
```

Expected: all commands pass and no document calls the selected lane MXFP4.

- [ ] **Step 7: Commit and push**

```bash
git add docs adapters/deepseek/ds4/manifests \
  adapters/deepseek/ds4/tools tests/adapters/test_ds4_artifact_manifest.py \
  tests/spark_profiles/test_contracts.py
git commit -m "docs: pin the DS4 Spark serving lane"
git push origin main
```

### Task 3: Build and Publish the Patched DS4 CUDA Runtime Image

**Files:**
- Create: `adapters/deepseek/ds4/Dockerfile`
- Create: `adapters/deepseek/ds4/patches/served-model-name.patch`
- Create: `adapters/deepseek/ds4/compose.yaml`
- Create: `adapters/deepseek/ds4/config/runtime.env`
- Create: `tests/adapters/test_ds4_runtime.py`

**Interfaces:**
- Produces: ARM64 image `ghcr.io/carstvaartjes/spark-ds4` with
  `/opt/ds4/ds4-server` and OCI labels for source commit, source-archive
  SHA-256, patch SHA-256, CUDA build target, and served model ID.
- Produces: Compose service `ds4-deepseek-single`, using host networking,
  NVIDIA GPU access, `restart: "no"`, read-only root filesystem, read-only
  model mount, bounded writable cache mounts, and no LAN bind.

- [ ] **Step 1: Write failing static render and pin tests**

Assert both `FROM` lines use the exact digests from Global Constraints, the
source URL contains the exact commit, the archive digest is verified before
extraction, `make cuda-spark` is used, the update check is disabled,
`DS4_CUDA_COPY_MODEL` and `DS4_MODEL_ANON_HUGE=1` are absent, the command uses
32,768 context and port 8888, and Compose renders `restart: "no"` with no
published LAN port.

- [ ] **Step 2: Write failing served-name patch tests**

Apply the patch to a fixture containing the v0.5.3 model-ID functions. Assert
that `DS4_SERVED_MODEL_NAME=deepseek` changes `/v1/models`, default response
model IDs, and `GET /v1/models/deepseek`, while an unset variable retains the
upstream ID.

- [ ] **Step 3: Run focused tests and observe failures**

```bash
uv run pytest tests/adapters/test_ds4_runtime.py -v
```

Expected: FAIL because the image, patch, and Compose files are absent.

- [ ] **Step 4: Implement the multi-stage image and minimal alias patch**

Install only the compiler/build dependencies in the build stage. Download the
exact source archive, verify its SHA-256, apply the checked patch, and run
`make cuda-spark`. Copy the DS4 binaries, license, and required runtime files
into the pinned CUDA runtime stage. Run as a numeric non-root user, disable the
update check, and leave tracing off.

The patch must read `DS4_SERVED_MODEL_NAME`, validate it as a nonempty model ID
without control characters, default to the upstream engine ID when unset, and
recognize the configured name in the model-detail route. It must not change
model selection, prompt rendering, sampling, or tool-call logic.

- [ ] **Step 5: Implement the loopback-only Compose service**

Mount `/srv/models/snapshots/deepseek-v4-flash-0731-ds4` read-only at
`/models`. Mount workload-specific derived-weight, KV, and log directories
writable. Set `DS4_SERVED_MODEL_NAME=deepseek`, `DS4_NO_UPDATE_CHECK=1`,
`DS4_CONT_MTP_MODE=2`, `DS4_CONT_DSPARK=1`, and the exact drafter path. Start:

```text
/opt/ds4/ds4-server --cuda
-m /models/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf
-c 32768 --host 127.0.0.1 --port 8888
--kv-disk-dir /var/lib/ds4/kv --kv-disk-space-mb 8192
```

- [ ] **Step 6: Run static verification**

```bash
uv run pytest tests/adapters/test_ds4_runtime.py -v
docker compose -f adapters/deepseek/ds4/compose.yaml \
  --env-file adapters/deepseek/ds4/config/runtime.env config --quiet
uv run ruff check tests
git diff --check
```

Expected: all commands pass. If local Docker is unavailable, the Compose test
must still run in CI and cannot be skipped.

- [ ] **Step 7: Build and publish from Spark 1**

Create an SSH Docker context for `dgx-spark-1`, create a Buildx remote builder,
authenticate the local client to GHCR without printing the token, then run an
ARM64 build with `--push` and `--metadata-file`. Tag the immutable build
`ghcr.io/carstvaartjes/spark-ds4:ds4-v0.5.3-q2-0731-health`; record the returned
manifest digest and verify its ARM64 platform and OCI labels with
`docker buildx imagetools inspect`.

- [ ] **Step 8: Commit and push**

```bash
git add adapters/deepseek/ds4/Dockerfile \
  adapters/deepseek/ds4/patches adapters/deepseek/ds4/compose.yaml \
  adapters/deepseek/ds4/config tests/adapters/test_ds4_runtime.py
git commit -m "feat: build the DS4 Spark runtime"
git push origin main
```

### Task 4: Add the DS4 Adapter, Definition, and Single-Agent Profile Intent

**Files:**
- Create: `adapters/deepseek/ds4/bin/ds4-deepseek-single`
- Create: `adapters/deepseek/ds4/runtime-manifest.json`
- Create: `config/workloads/deepseek-agent-single.toml`
- Create: `config/cluster-profiles/agent-single.toml`
- Modify: `locks/model-definitions.toml`
- Modify: `inventory/reports/model-definitions.json`
- Modify: `tests/adapters/test_ds4_runtime.py`
- Modify: `tests/spark_profiles/test_catalog.py`
- Modify: `tests/spark_profiles/test_admission.py`
- Modify: `tests/spark_profiles/test_switcher.py`

**Interfaces:**
- Produces: adapter operations `prepare`, `verify`, `start`, `health`, `infer`,
  `stop`, and `verify-release`, each running on Spark 1 without a role suffix.
- Produces: planned definition `deepseek-agent-single` and planned Cluster
  Profile `agent-single`, with endpoint `deepseek = "deepseek-agent-single"`.

- [ ] **Step 1: Write failing adapter lifecycle tests**

Cover exact-node enforcement (`spark-3542` only), free-memory and free-disk
gates, two concurrent resumable artifact downloads, streamed SHA verification,
image-digest verification, release verification, no autostart, offline start,
startup-log mmap/no-copy evidence, health, `/v1/models` exactly advertising
`deepseek`, a `model: deepseek` completion, structured tool call, reasoning
off/low/high/max, graceful stop, and memory recovery.

- [ ] **Step 2: Write failing catalog/admission/switch tests**

Assert the definition is locked and planned, `agent-single` places it only on
Spark 1 with Spark 2 empty, admission refuses it until exact accepted evidence
exists, endpoint name remains `deepseek`, single-node lifecycle calls never
touch Spark 2, and no `head`/`worker` argument is appended.

- [ ] **Step 3: Run focused tests and observe failures**

```bash
uv run pytest tests/adapters/test_ds4_runtime.py \
  tests/spark_profiles/test_catalog.py \
  tests/spark_profiles/test_admission.py \
  tests/spark_profiles/test_switcher.py -v
```

Expected: FAIL because the adapter, definition, and profile are absent.

- [ ] **Step 4: Implement the fail-closed adapter**

Use the same immutable-release and lifecycle-record patterns as the Mia
adapter, without distributed fabric/role checks. `prepare` creates only
workload-scoped directories, downloads the two immutable URLs in parallel to
`.partial` files, resumes with HTTP Range, checks size and SHA-256, and renames
only verified files. It never deletes an existing mismatch. `verify` requires
GB10/SM121, the exact image digest, the exact two-artifact manifest, the
digest-qualified release, at least 120 GB free disk before first preparation,
and no autostart. `start` uses offline mode and records the boot ID, image,
container ID, release, context, served name, and mapped/no-copy startup mode.

- [ ] **Step 5: Add the content-addressed definition and profile intent**

Use the pushed image digest from Task 3 and the generated runtime-manifest
digest. Define Spark 1 only, `topology = "single"`,
`placement_class = "single-exclusive"`, `co_location = "exclusive"`, port
8888, workload-specific paths, and all seven deadlines. Compute the definition
fingerprint with the repository catalog, add it to `locks/model-definitions.toml`,
and add only a `planned` history entry referencing the approved multi-runtime
design. Do not modify `config/profile-selectors.toml`; `default` and `agent`
continue to resolve to `agent-full-dual`.

- [ ] **Step 6: Generate the immutable adapter manifest**

Hash every file below `adapters/deepseek/ds4` except Python bytecode and the
runtime manifest itself. Write canonical sorted JSON, hash it, insert the hash
into the workload definition, recompute the definition fingerprint, and update
the lock and planned maturity record once.

- [ ] **Step 7: Verify all local framework behavior**

```bash
uv run pytest tests/adapters/test_ds4_runtime.py \
  tests/spark_profiles/test_catalog.py \
  tests/spark_profiles/test_admission.py \
  tests/spark_profiles/test_switcher.py -v
uv run pytest -q
uv run ruff check .
git diff --check
```

Expected: the full suite passes; `sparkctl inspect agent-single` resolves the
profile but admission reports planned maturity and no exact accepted evidence.

- [ ] **Step 8: Commit and push**

```bash
git add adapters/deepseek/ds4 config/workloads/deepseek-agent-single.toml \
  config/cluster-profiles/agent-single.toml locks/model-definitions.toml \
  inventory/reports/model-definitions.json tests
git commit -m "feat: add the DS4 single-Spark definition"
git push origin main
```

### Task 5: Deploy, Prepare, and Verify the DS4 Lane on Spark 1

**Files:**
- Create: `inventory/reports/model-definitions/deepseek-agent-single-prepared.json`
- Create: `inventory/reports/model-definitions/deepseek-agent-single-verified.json`
- Create: `inventory/reports/deepseek-ds4-operational.json`
- Modify: `inventory/reports/model-definitions.json`
- Modify: `docs/installation-record.md`
- Modify: `docs/model-capacity-overview.md`
- Modify: `docs/model-profile-overview.md`
- Modify: `docs/runbooks/sparkctl.md`

**Interfaces:**
- Produces: a deployed immutable adapter and complete verified cache on Spark 1.
- Produces: maturity `verified` plus an operational report with `accepted: false`.

- [ ] **Step 1: Deploy the immutable adapter only to Spark 1**

Run `scripts/deploy-runtime-release deepseek-agent-single` in its default
dry-run mode, confirm its only alias is `dgx-spark-1`, then apply. Verify the
installed release digest directly from the content-addressed release directory;
the Workload Definition uses that absolute immutable path and no mutable
`current` symlink participates. Do not invoke the lifecycle `verify-release`
operation while Mia still owns port 8888 and no DS4 lifecycle baseline exists.

- [ ] **Step 2: Prepare the artifact cache while Mia remains live**

Run the DS4 adapter's `prepare` operation on Spark 1. Monitor both file sizes
and the adapter log; the base and drafter downloads must overlap in time. Keep
the Mia service running during network/disk preparation, and do not start a
second inference runtime yet.

- [ ] **Step 3: Verify offline readiness and record prepared evidence**

Disable network access inside the offline container check, rehash both GGUFs,
and inspect the image and architecture while Mia remains live. Do not run the
adapter's full exclusive-runtime `verify` yet: it requires Mia to have released
port 8888 and the single-Spark memory budget. Generate the canonical prepared
report for Spark 1's current boot ID, advance maturity from `planned` to
`prepared`, and verify the catalog.

- [ ] **Step 4: Stop Mia safely and start DS4**

Stop the Mia head on Spark 1, then its worker on Spark 2. Require both nodes to
recover within the Mia memory tolerance. Run the DS4 adapter's full `verify`
operation from the immutable release, then start DS4 on Spark 1, leaving Spark
2 idle. If verification or DS4 fails, stop DS4 if necessary and leave the
cluster stopped; do not automatically restart Mia.

- [ ] **Step 5: Run the live DS4 quality and identity gates**

Require HTTP 200 health, `/v1/models` returning only `deepseek`, streaming,
English/script/repetition/XML gates, reasoning off/low/high/max, tool calling,
an 8K prompt crossing token 411, and a second-turn prefix-cache reuse signal.
Require `/v1/stats` to report the exact loaded model, 32,768 context, derived
artifact source, and no full-copy fallback.

- [ ] **Step 6: Record verified-not-accepted evidence**

Generate the canonical verified report with single-node gates `offline`,
`release`, `image`, `architecture`, `manifest`, `mmap`, and `api_identity` all
true. Advance maturity from `prepared` to `verified`. Record actual cold-start,
memory, disk, KV, basic throughput, temperature, and DSpark acceptance metrics
in `deepseek-ds4-operational.json`, with performance/thermal/lifecycle/reboot
acceptance explicitly false.

- [ ] **Step 7: Restore the default Mia home runtime explicitly**

After all DS4 evidence is durably written, stop DS4 and invoke its unchanged
`verify-release` operation to require container and port cleanup plus Spark 1
memory recovery. Start the Mia worker on Spark 2 first, then the Mia head on
Spark 1, and rerun its hardened health and `deepseek` identity checks. This is
an explicit successful-test restoration, not automatic rollback: any failure
leaves the cluster stopped for diagnosis.

- [ ] **Step 8: Reconcile documentation**

Document exact pins, install paths, image digest, measured resource envelope,
quality results, stable `deepseek` endpoint, and the fact that `agent-single`
cannot yet be selected through accepted admission. Mark DS4 Q2 operational and
verified; keep MXFP4 deferred.

- [ ] **Step 9: Run final repository and live verification**

```bash
uv run pytest -q
uvx --from ruff==0.16.1 ruff check .
git diff --check
curl --fail --silent http://127.0.0.1:8888/v1/models
```

Expected: all repository gates pass and the restored Mia endpoint advertises
`deepseek`. Run the complete CI matrix after push.

- [ ] **Step 10: Commit and push**

```bash
git add inventory/reports docs
git commit -m "ops: verify the DS4 single-Spark runtime"
git push origin main
gh run watch --exit-status
```

## Completion Boundary

This plan is complete when DS4 Q2 0731 has run successfully and is `verified`
on Spark 1, its immutable evidence is pushed, and the default Mia dual-Spark
runtime has been explicitly restored with the live endpoint still
client-compatible as `deepseek`. It does not accept `agent-single`, does not
claim MXFP4 support, and does not claim co-residency until the final cross-model
acceptance phase records the missing performance, thermal, lifecycle, reboot,
and exact-profile evidence.
