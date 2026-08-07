# Qwen3-VL Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and qualify an isolated Qwen3-VL-8B-Instruct single-GPU node runtime through the approved vLLM path, or record a reproducible GPU node prerequisite blocker without advertising the model.

**Architecture:** The model owns one adapter release, source snapshot, checkpoint snapshot, scratch/venv, input/output roots, log/PID files, and endpoint. The adapter prepares the pinned upstream repository and checkpoint, starts a model-specific vLLM OpenAI-compatible server, confines requests to the model input/output roots, and fails closed until the catalog evidence chain reaches the required maturity.

**Tech Stack:** Bash lifecycle adapter, Python 3.12 runtime helpers, vLLM, Transformers processor, `qwen-vl-utils==0.0.14`, JSON evidence, pytest, SSH/GPU node 2.

## Global Constraints

- Use the pinned Qwen3-VL source commit `96588727e44c78b25ba03ea03b8e12f7e64fd0da` and checkpoint revision `0c351dd01ed87e9c1b53cbc748cba10e6187ff3b`.
- Keep all runtime state under model-owned paths: `/srv/models/snapshots/qwen3-vl-8b-single`, `/srv/models/runtime-cache/qwen3-vl-8b-single/venv`, `/srv/models/inputs/qwen3-vl-8b-single`, `/srv/models/outputs/qwen3-vl-8b-single`, and `/srv/models/logs/qwen3-vl-8b-single`.
- Do not stop or modify the accepted Mia runtime except during an explicitly bounded qualification window, and restore it before ending that window.
- Require an ARM64/GB10-compatible serving image or venv before prepared/verified evidence; never substitute an unqualified x86 image or shared environment.
- Only record prepared/verified/accepted evidence for gates actually observed; otherwise keep `qwen3-vl-8b-single` planned and fail closed.
- Semantic acceptance uses a pinned local visual fixture and requires defect classification, ranking, and structured response validation as specified by the multi-runtime design.

---

### Task 1: Define and test the isolated request/runtime contract

**Files:**
- Create: `adapters/creative/qwen3-vl-8b-single/__init__.py`
- Create: `adapters/creative/qwen3-vl-8b-single/runtime.py`
- Create: `tests/adapters/test_qwen3_vl_runtime_contract.py`

**Interfaces:**
- `InferRequestError(ValueError)` identifies malformed or escaping requests.
- `InferRequest.from_payload(payload, input_root)` accepts a mapping with `image_path`, `prompt`, optional `max_tokens`, and optional `seed`, and returns an immutable request with a confined input path.
- `build_output_path(output_root, name) -> Path` confines `.json` output to the model output root.
- `health_payload(ready: bool) -> dict[str, object]` returns model identity and readiness.

- [ ] **Step 1: Write failing tests** for valid image confinement, traversal/unsupported-extension rejection, output confinement, health identity, and adapter exit-code surface.
- [ ] **Step 2: Run the focused tests** with `.venv/bin/pytest -q tests/adapters/test_qwen3_vl_runtime_contract.py`; expect import/implementation failures.
- [ ] **Step 3: Implement the minimal contract** with resolved-path `is_relative_to` checks and no network or model loading.
- [ ] **Step 4: Run the focused tests** and require all tests to pass.
- [ ] **Step 5: Commit** with `git add adapters/creative/qwen3-vl-8b-single tests/adapters/test_qwen3_vl_runtime_contract.py && git commit -m "test: define isolated qwen3 vl runtime contract"`.

### Task 2: Implement the model-owned server and lifecycle adapter

**Files:**
- Create: `adapters/creative/qwen3-vl-8b-single/server.py`
- Create: `adapters/creative/qwen3-vl-8b-single/bin/qwen3-vl-8b-single`
- Create: `adapters/creative/qwen3-vl-8b-single/runtime-manifest.json`
- Modify: `tests/adapters/test_qwen3_vl_runtime_contract.py`

**Interfaces:**
- `server.py` exposes `GET /health` and `POST /v1/chat/completions` on loopback, loads only the configured local checkpoint, and serializes requests through the Qwen processor/vLLM client.
- The executable supports exactly `prepare|verify|start|health|infer|stop|verify-release`, uses the model-specific roots and port `9106`, and exits 2 for unavailable preparation/verification and 3 for non-accepted serving operations.
- The release manifest lists every shipped adapter artifact and is content-addressed by the workload definition.

- [ ] **Step 1: Extend tests** for exact operation dispatch, unknown-operation exit 64, model-specific paths, no shared adapter references, and health/infer request confinement.
- [ ] **Step 2: Run tests** and confirm the new server/adapter behavior is absent or failing.
- [ ] **Step 3: Implement `server.py`** using a local checkpoint path, `qwen-vl-utils` vision preprocessing, deterministic seed handling, bounded JSON output, and explicit readiness state.
- [ ] **Step 4: Implement `bin/qwen3-vl-8b-single`** with idempotent source/checkpoint synchronization, venv creation under the model scratch root, pinned dependency installation, PID/log handling, loopback health polling, and bounded stop/release verification.
- [ ] **Step 5: Build and digest the release manifest**, then run the focused adapter tests.
- [ ] **Step 6: Commit** with `git add adapters/creative/qwen3-vl-8b-single tests/adapters/test_qwen3_vl_runtime_contract.py && git commit -m "feat: add isolated qwen3 vl lifecycle adapter"`.

### Task 3: Lock the catalog definition and evidence boundary

**Files:**
- Modify: `config/workloads/qwen3-vl-8b-single.toml`
- Modify: `locks/model-definitions.toml`
- Modify: `inventory/reports/model-definitions.json`
- Create when observed: `inventory/reports/model-definitions/qwen3-vl-8b-single-prepared.json`
- Create when observed: `inventory/reports/model-definitions/qwen3-vl-8b-single-verified.json`
- Create: `docs/audits/2026-08-03-qwen3-vl-runtime-qualification.json`
- Modify: `docs/superpowers/plans/2026-08-03-phase4-model-definition-rollout.md`

**Interfaces:**
- The workload points every operation to the model-owned release path and preserves unique cache/scratch/output roots and port 9106.
- Evidence records exact source/checkpoint/image/runtime-manifest pins, node boot ID, gates, predecessor, and an explicit blocker when preparation cannot proceed.

- [ ] **Step 1: Add the runtime manifest digest and model-specific command paths** without changing maturity until live gates exist.
- [ ] **Step 2: Recompute the definition lock and maturity fingerprint** with `PYTHONPATH=src .venv/bin/python` and validate via `Catalog.load(Path("."))`.
- [ ] **Step 3: Add the audit schema-shaped report** recording prerequisite checks and the exact GPU node result.
- [ ] **Step 4: Run catalog, adapter, and full tests**; require no shared path or stale fingerprint.
- [ ] **Step 5: Commit** with `git add config locks inventory docs && git commit -m "catalog isolated qwen3 vl runtime"`.

### Task 4: Prepare and qualify on GPU node 2

**Files:**
- Remote model-owned state under `/srv/models/{sources,snapshots,runtime-cache,inputs,outputs,logs}/qwen3-vl-8b-single`.
- Update: `inventory/reports/model-definitions/qwen3-vl-8b-single-{prepared,verified}.json` only for gates actually observed.
- Update: `docs/audits/2026-08-03-qwen3-vl-runtime-qualification.json`.

**Interfaces:**
- Preparation verifies the source commit, checkpoint revision, ARM64-compatible vLLM dependency/image, and local manifest before serving.
- Qualification requires health identity, a deterministic visual fixture response, resource/thermal measurements, three start-infer-stop cycles, release recovery, and direct loopback/SSH validation.

- [ ] **Step 1: Confirm Mia is healthy and retain its exact restore command before the qualification window.**
- [ ] **Step 2: Run `prepare` and `verify` for Qwen3-VL on GPU node 2**, recording versions, image digest, disk use, and checkpoint manifest.
- [ ] **Step 3: If preparation succeeds, run `start`, `health`, and one deterministic fixture inference**, validating structured classification/ranking output.
- [ ] **Step 4: Run three lifecycle cycles plus resource, thermal, and release-recovery checks**, never overlapping another active model.
- [ ] **Step 5: Restore Mia and verify its health before leaving GPU node 2.**
- [ ] **Step 6: Record `prepared`/`verified` maturity only when the evidence validator accepts the complete chain; otherwise record the blocker and retain `planned`.**
- [ ] **Step 7: Run `.venv/bin/pytest -q`, `git diff --check`, and `git ls-remote origin refs/heads/main`, then push the qualification commit to `origin/main`.**

## Self-review

- The plan covers the model-owned directory/venv invariant, official loader choice, contract behavior, catalog evidence, and every Phase 4 acceptance gate applicable to Qwen3-VL.
- No task assumes a compatible ARM64 image exists; the remote gate records failure rather than lowering requirements.
- No planned definition is promoted merely because source or checkpoint downloads succeed.
