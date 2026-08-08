# DeepSeek V4 Flash 0731 Runtime Implementation Plan

> **Superseded by the Mia dual-GPU node implementation plan:** This initial
> staged-lane plan is retained as historical DeepSeek bring-up research. The
> approved implementation pins Mia commit
> `b131b2a22164675890dd1465fd8862b5cfb6ff13` and treats the 1M-capable
> dual-GPU node runtime as the planned candidate. Execute
> [the superseding plan](2026-08-02-mia-deepseek-dual-runtime.md), not the
> tasks below. The historical rationale remains useful for later acceptance
> gates and the DS4 comparison path.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run and validate the pinned DeepSeek-V4-Flash-0731 checkpoint across both GPU nodes through a loopback-only API before adding the external gateway.

**Architecture:** The MiaAI-Lab dual-GPU node recipe is audited and pinned rather than run blindly. Both nodes keep complete manifest-verified offline caches. Bring-up advances from plain TP=2 through draft-model and padded NVFP4 before enabling concurrent 200K and derived-concurrency 1M lanes; vLLM binds to GPU node 1 loopback and the Mac reaches it through an SSH tunnel.

**Tech Stack:** Docker Compose, vLLM/Anemll GX10 image, Python 3.12, Hugging Face Hub, SHA-256 manifests, DeepSeek V4 encoder, draft-model MTP=5, NVFP4 DS-MLA, pytest, OpenAI-compatible HTTP, upstream benchmark tooling.

## Global Constraints

- Historical research initially used MiaAI-Lab commit
  `914c35bd7d5607560048e4467c3fdd42e892e297`; the superseding plan pins
  `b131b2a22164675890dd1465fd8862b5cfb6ff13`.
- Pin model revision `9e165c30e2704aec5d9d593cce3eebd58bbef1cb`.
- Resolve `ghcr.io/anemll/dspark-vllm-gx10:0.1.1` to and run by immutable digest.
- Require the complete 166,898,660,330-byte snapshot and 166,886,535,336 SafeTensor bytes on both nodes.
- Serving requires `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and `HF_HUB_DISABLE_XET=1`.
- Use tensor parallel 2, pipeline parallel 1, distributed executor `mp`, worker-first start, and head-first stop.
- Use `restart: "no"` for every DeepSeek container.
- Fail if `earlyoom` is active/enabled or Plan 1 fabric evidence is absent.
- Bind the first API to `127.0.0.1:8888`; do not expose it on the LAN before the external gateway plan.
- Do not advertise a shared profile until the later control-plane integration passes, but allow direct local/tunnel testing in this plan.

---

### Task 1: Lock and audit all upstream artifacts

**Files:**
- Create: `locks/sources.toml`
- Create: `locks/images.toml`
- Create: `locks/patches.toml`
- Create: `docs/audits/mia-914c35b.md`
- Create: `scripts/verify-locks`
- Create: `tests/scripts/test_verify_locks.py`

**Interfaces:**
- Produces: source/image/patch lock files consumed by all build and deployment scripts.
- `verify-locks` exits 0 only when commit, image digest, encoder checksum, and patch checksums match.

- [ ] **Step 1: Write failing lock-verification tests**

```python
def test_rejects_mutable_image_tag(run_verify, lock_tree):
    lock_tree.images.write_text('image = "ghcr.io/anemll/dspark-vllm-gx10:0.1.1"')
    result = run_verify(lock_tree)
    assert result.returncode != 0
    assert "digest" in result.stderr

def test_rejects_changed_patch(run_verify, valid_lock_tree):
    valid_lock_tree.patch.write_text("changed")
    assert run_verify(valid_lock_tree).returncode != 0
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/scripts/test_verify_locks.py -v`

Expected: FAIL because `scripts/verify-locks` is absent.

- [ ] **Step 3: Inspect and document the pinned source**

Clone the Mia repository into a temporary directory at the exact commit. Review the launcher, Compose file, encoder-copy logic, draft-model patch, padded NVFP4 caveat, environment matrix, network bindings, image provenance, license, and destructive commands. Record each reviewed path and conclusion in the audit.

- [ ] **Step 4: Resolve immutable digests and checksums**

Use `docker buildx imagetools inspect` for the ARM64 image digest. Hash the pinned encoder and every applied/vendored patch with SHA-256. Store no mutable tag without its digest.

- [ ] **Step 5: Implement and run lock verification**

The script parses TOML, checks 40-hex commits, `sha256:` image digests, and 64-hex file hashes, then hashes each referenced local file.

Run: `pytest tests/scripts/test_verify_locks.py -v && scripts/verify-locks`

Expected: PASS.

- [ ] **Step 6: Commit the audit and locks**

```bash
git add locks docs/audits/mia-914c35b.md scripts/verify-locks tests/scripts/test_verify_locks.py
git commit -m "security: pin and audit DeepSeek runtime"
```

### Task 2: Build model-manifest generation and verification

**Files:**
- Create: `tools/model_manifest.py`
- Create: `tests/tools/test_model_manifest.py`
- Create: `manifests/deepseek-v4-flash-0731.json`
- Create: `docs/runbooks/model-cache.md`

**Interfaces:**
- Produces: `generate(repo_id, revision) -> Manifest` and `verify(manifest, snapshot_dir) -> VerificationReport`.
- Manifest entry fields: `path`, `size`, and `sha256`; manifest also stores repo ID, revision, total bytes, and encoder path.

- [ ] **Step 1: Write failing manifest tests**

```python
def test_verification_detects_missing_and_changed_files(tmp_path, manifest):
    report = verify(manifest, tmp_path)
    assert report.ok is False
    assert report.missing == ("model-00001-of-00048.safetensors",)

def test_manifest_requires_encoder(manifest_without_encoder):
    with pytest.raises(ManifestError, match="encoding/encoding_dsv4.py"):
        validate_manifest(manifest_without_encoder)
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/tools/test_model_manifest.py -v`

Expected: import failure for `tools.model_manifest`.

- [ ] **Step 3: Implement Hugging Face LFS metadata generation**

Fetch only repository metadata for the exact revision. Require 74 files, 48 SafeTensor shards, total bytes `166898660330`, SafeTensor bytes `166886535336`, and `encoding/encoding_dsv4.py`. Treat each LFS OID as the expected SHA-256.

- [ ] **Step 4: Implement streamed local hashing**

Read files in 8 MiB chunks, compare size before hashing, return all mismatches without deleting or downloading anything, and write a machine-readable report.

- [ ] **Step 5: Run tests and generate the checked manifest**

Run: `pytest tests/tools/test_model_manifest.py -v && python -m tools.model_manifest generate --repo deepseek-ai/DeepSeek-V4-Flash-0731 --revision 9e165c30e2704aec5d9d593cce3eebd58bbef1cb --output manifests/deepseek-v4-flash-0731.json`

Expected: PASS and exact aggregate sizes.

- [ ] **Step 6: Commit cache tooling**

```bash
git add tools tests/tools manifests docs/runbooks/model-cache.md
git commit -m "feat: verify DeepSeek model snapshots"
```

### Task 3: Define the five DeepSeek profile configurations

**Files:**
- Create: `profiles/deepseek/compose.yaml`
- Create: `profiles/deepseek/env/common.env`
- Create: `profiles/deepseek/env/baseline.env`
- Create: `profiles/deepseek/env/draft.env`
- Create: `profiles/deepseek/env/nvfp4.env`
- Create: `profiles/deepseek/env/agent.env`
- Create: `profiles/deepseek/env/long.env`
- Create: `profiles/deepseek/bin/profile-start`
- Create: `profiles/deepseek/bin/profile-stop`
- Create: `profiles/deepseek/bin/profile-status`
- Create: `config/profiles/deepseek-baseline.toml`
- Create: `config/profiles/deepseek-draft.toml`
- Create: `config/profiles/deepseek-nvfp4.toml`
- Create: `config/profiles/deepseek-agent.toml`
- Create: `config/profiles/deepseek-long.toml`
- Create: `tests/profiles/test_deepseek_profiles.py`

**Interfaces:**
- Produces: node-local Compose project `vonk-deepseek`.
- Produces: scripts with interface `profile-start NAME RANK`, `profile-stop NAME`, and `profile-status` for manual SSH now and restricted controller use later.
- Profile constants: baseline `16384/1/FP8/off`; draft-model `16384/1/FP8/MTP5`; NVFP4 `16384/1/nvfp4_ds_mla/MTP5`; agent `200000/6/nvfp4_ds_mla/MTP5`; long `1048576/Cfull/nvfp4_ds_mla/MTP5`.

- [ ] **Step 1: Write failing profile-policy tests**

```python
def test_profile_ladder(profile_matrix):
    assert profile_matrix["deepseek-baseline"].speculation is False
    assert profile_matrix["deepseek-agent"].max_num_seqs == 6
    assert profile_matrix["deepseek-long"].max_model_len == 1_048_576

def test_distributed_policy(compose):
    service = compose["services"]["vllm-draft"]
    assert service["restart"] == "no"
    assert "--distributed-executor-backend mp" in rendered_command(service)
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/profiles/test_deepseek_profiles.py -v`

Expected: FAIL because profile files are absent.

- [ ] **Step 3: Implement common pinned runtime settings**

Set TP=2, PP=1, `mp`, `max_num_batched_tokens=8192`, `gpu_memory_utilization=0.80`, worker/head ranks, fabric variables from inventory, offline variables, `--generation-config vllm`, no server generation override, MTP=5 where enabled, and the pinned encoder installation.

- [ ] **Step 4: Implement lane-specific overlays**

Do not include `--speculative-config` in baseline. Add it only from draft-model onward. Use FP8 KV before NVFP4. For long, require `CFULL` equal to `min(2, floor(P / 1048576))` and reject zero.

Set the head API bind to `127.0.0.1:8888`. Implement the three profile scripts with exact profile-name allowlisting, worker/head rank inputs, `restart: "no"`, and no LAN publishing. The later `node-nodectl` command calls these same scripts rather than duplicating Compose logic.

- [ ] **Step 5: Run render and policy tests**

Run: `pytest tests/profiles/test_deepseek_profiles.py -v && docker compose -f profiles/deepseek/compose.yaml config --quiet`

Expected: PASS for every lane/rank rendering.

- [ ] **Step 6: Commit profiles**

```bash
git add profiles/deepseek config/profiles/deepseek-*.toml tests/profiles/test_deepseek_profiles.py
git commit -m "feat: define staged DeepSeek profiles"
```

### Task 4: Build deterministic output-quality gates

**Files:**
- Create: `validation/quality.py`
- Create: `validation/fixtures/deepseek-quality.json`
- Create: `tests/validation/test_quality.py`
- Create: `config/sampling/deterministic.toml`
- Create: `config/sampling/production.toml`

**Interfaces:**
- Produces: `run_quality_gate(base_url, api_key, model, fixture) -> QualityReport`.
- Deterministic preset: temperature 0, top-p 1, fixed seed where supported, fixed output ceiling.
- Production preset: temperature 1.0, top-p 1.0.

- [ ] **Step 1: Write failing detector tests**

```python
def test_detects_cjk_drift():
    assert analyze("这是意外输出", english_fixture()).unexpected_script is True

def test_detects_repetition_loop():
    assert analyze("abc " * 80, english_fixture()).repetition_loop is True

def test_detects_xml_leakage():
    assert analyze("<tool_call><schema>", english_fixture()).xml_leakage is True
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/validation/test_quality.py -v`

Expected: import failure for `validation.quality`.

- [ ] **Step 3: Implement fixed fixtures and detectors**

Include exact sentinel, English fact, executable Python code, reasoning modes `low/high/max`, tool name/JSON arguments, an 8K sentinel beyond token 411, Unicode script ratios that detect unexpected CJK drift, repeated character and 3–8-gram thresholds, entropy floor, and XML/schema markers.

- [ ] **Step 4: Run quality unit tests**

Run: `pytest tests/validation/test_quality.py -v`

Expected: PASS for clean outputs and failure for every corrupt fixture.

- [ ] **Step 5: Commit quality gates**

```bash
git add validation config/sampling tests/validation/test_quality.py
git commit -m "test: add DeepSeek output quality gates"
```

### Task 5: Build capacity and performance acceptance tooling

**Files:**
- Create: `validation/capacity.py`
- Create: `validation/performance.py`
- Create: `tests/validation/test_capacity.py`
- Create: `tests/validation/test_performance.py`
- Create: `config/benchmarks/deepseek-0731.toml`

**Interfaces:**
- Produces: `parse_kv_pool(log_text) -> int`, `derive_cfull(pool) -> int`, and `evaluate_performance(results, thresholds) -> PerformanceReport`.
- Thresholds: prefill 1,794 tok/s; 2K C1 decode 48.1 tok/s; 2048-output C1 57.6 tok/s; C3 aggregate 94.2 tok/s; 2K C6 aggregate 100.5 tok/s.

- [ ] **Step 1: Write failing arithmetic and threshold tests**

```python
def test_derives_full_context_slots():
    assert derive_cfull(2_493_464) == 2
    assert derive_cfull(1_990_142) == 1

def test_agent_pool_floor():
    assert evaluate_agent_pool(1_199_999).passed is False
    assert evaluate_agent_pool(1_200_000).passed is True
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/validation/test_capacity.py tests/validation/test_performance.py -v`

Expected: import failures.

- [ ] **Step 3: Implement capacity and performance evaluators**

Parse the exact boot-log fields, reject missing values, cap `Cfull` at 2, evaluate all numeric floors, compare first/final five-minute medians, and fail a greater-than-15% sustained decline or any thermal-throttling flag.

- [ ] **Step 4: Run tests**

Run: `pytest tests/validation/test_capacity.py tests/validation/test_performance.py -v`

Expected: PASS.

- [ ] **Step 5: Commit acceptance tooling**

```bash
git add validation tests/validation config/benchmarks/deepseek-0731.toml
git commit -m "test: add DeepSeek capacity and performance gates"
```

### Task 6: Prepare and verify both local model caches

**Files:**
- Create: `inventory/reports/deepseek-cache-node1.json`
- Create: `inventory/reports/deepseek-cache-node2.json`
- Modify: `docs/runbooks/model-cache.md`

**Interfaces:**
- Consumes: checked manifest and measured disk gates.
- Produces: complete per-node verification reports with identical revision and encoder hash.

- [ ] **Step 1: Confirm free-disk and maintenance gates**

Require at least 350 GiB free per node before download. Confirm no AI profile is active and record current free bytes.

- [ ] **Step 2: Download on GPU node 2 in explicit online mode**

Use `snapshot_download` with the exact revision and local HF cache. Do not use `latest`, branch names, or Xet. Keep model serving stopped.

- [ ] **Step 3: Verify GPU node 2 and enable offline serving config**

Run the manifest verifier over the resolved snapshot. Require all 74 files, exact aggregate size, and encoder hash. Write the GPU node 2 report.

- [ ] **Step 4: Repeat download and verification on GPU node 1**

Write the GPU node 1 report and compare revision, file count, total bytes, and every SHA-256 result.

- [ ] **Step 5: Verify post-cache disk floor**

Require at least 150 GiB free on each node after image, model, encoder, and JIT preparation. Stop and report if either fails.

- [ ] **Step 6: Commit cache evidence**

```bash
git add inventory/reports/deepseek-cache-*.json docs/runbooks/model-cache.md
git commit -m "ops: verify DeepSeek caches on both GPU nodes"
```

### Task 7: Execute the staged live bring-up

**Files:**
- Create: `inventory/reports/deepseek-ladder.json`
- Create: `docs/runbooks/deepseek-bringup.md`

**Interfaces:**
- Consumes: profile scripts, quality gates, verified caches, and fabric report.
- Produces: pass/fail evidence for each profile before the next starts.

- [ ] **Step 1: Start and accept `deepseek-baseline`**

Start the worker with `ssh vonk-node-2 'profile-start deepseek-baseline worker'`, then the head with `ssh vonk-node-1 'profile-start deepseek-baseline head'`. Require TP=2, correct model/revision, deterministic sentinel, encoding, reasoning, tool-call, and streaming tests through an SSH tunnel. On failure, stop head then worker and diagnose without enabling draft-model.

- [ ] **Step 2: Start and accept `deepseek-draft`**

Require MTP=5 active, nonzero speculative acceptance, and every baseline quality fixture still clean.

- [ ] **Step 3: Start and accept `deepseek-nvfp4`**

Require padded `nvfp4_ds_mla`, an 8,192-token prompt that crosses token 411, correct sentinel, no drift/loop/XML leakage, and stable ranks.

- [ ] **Step 4: Start and accept `deepseek-agent`**

Require live pool `P >= 1,200,000`, six concurrent requests with at most 200,000 live tokens each, and one overload probe that queues/rejects without rank death.

- [ ] **Step 5: Start and accept `deepseek-long`**

Derive `Cfull` from the boot log, render one or two scheduler slots, complete a 900K sentinel request at that admitted limit, and verify one excess request does not terminate either rank.

- [ ] **Step 6: Commit ladder evidence**

```bash
git add inventory/reports/deepseek-ladder.json docs/runbooks/deepseek-bringup.md
git commit -m "ops: validate staged DeepSeek bring-up"
```

### Task 8: Pass direct performance and restart acceptance

**Files:**
- Create: `inventory/reports/deepseek-performance.json`
- Create: `inventory/reports/deepseek-direct-resilience.json`
- Modify: `docs/runbooks/deepseek-bringup.md`

**Interfaces:**
- Produces: direct loopback/tunnel acceptance evidence for `deepseek-agent` and `deepseek-long`; gateway advertisement remains blocked until the external control-plane plan.

- [ ] **Step 1: Run the pinned benchmark sweep**

Warm the endpoint, run the exact 0731 benchmark preset, and require every floor from Task 5.

- [ ] **Step 2: Run the 15-minute thermal test**

Record temperatures, clocks, power, throttling flags, and throughput. Require no thermal-throttling flag and no greater than 15% decline from the first to final five-minute median.

- [ ] **Step 3: Test repeated direct profile transitions**

Perform at least three `deepseek-agent → stopped → deepseek-long → stopped` cycles with the node-local profile scripts. Require worker-first/head-second start, head-first/worker-second stop, and memory recovery within 5 GiB of baseline in 120 seconds.

- [ ] **Step 4: Test direct failure and reboot semantics**

Force a head health-check failure and stop head then worker. Reboot both GPU nodes and require no automatic AI container start. Recreate the SSH tunnel and verify port 8888 remains closed until an explicit worker-first/head-second start.

- [ ] **Step 5: Commit final DeepSeek acceptance**

```bash
git add inventory/reports/deepseek-performance.json inventory/reports/deepseek-direct-resilience.json docs/runbooks/deepseek-bringup.md
git commit -m "test: accept direct dual-GPU node DeepSeek 0731"
```
