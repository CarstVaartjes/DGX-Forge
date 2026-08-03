# TokenRig/SkinTokens Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and qualify an isolated Spark2 TokenRig/SkinTokens runtime with its own immutable release, Python environment, checkpoints, loopback API, and rigging-quality evidence.

**Architecture:** A digest-qualified Bash adapter owns preparation, verification, start, health, inference, stop, and release checks. It downloads the pinned SkinTokens source and both official checkpoints into model-specific paths, while a small loopback HTTP server loads the official `demo.py` pipeline once and validates confined mesh/GLB requests. Maturity advances only after live artifact, API, skeleton/skin-weight, lifecycle, and resource evidence.

**Tech Stack:** Bash, Python 3.11, PyTorch CUDA, official VAST-AI-Research/SkinTokens code, Hugging Face snapshots, trimesh/glTF, loopback `http.server`.

## Global Constraints

- Keep source, weights, scratch/venv, inputs, outputs, logs, PID, and endpoint namespaces unique to `tokenrig-single`.
- Preserve source commit `273b691d35989d71cd17ff2895fdc735097b92d1` and checkpoint revision `79736cad0fd84de384d5eede659b4ebd24effe33` until a new fingerprint is intentionally recorded.
- Use Python >=3.11 and CUDA >=12.1 as required by the official runtime.
- Do not start TokenRig while the accepted Mia dual runtime is active; restore Mia worker first/head second after each Spark2 qualification window.
- Keep `tokenrig-single` planned until prepared and verified evidence reports validate through `Catalog.load`.

---

### Task 1: Define the adapter contract

**Files:**
- Create: `tests/adapters/test_tokenrig_runtime_contract.py`
- Create: `adapters/creative/tokenrig/__init__.py`
- Create: `adapters/creative/tokenrig/runtime.py`

**Interfaces:**
- `parse_infer_request(payload, input_root) -> InferRequest` requires a confined `.glb` input and optional output path.
- `build_output_path(output_root, output_value) -> Path` confines output to the model-owned root.
- `health_payload(ready: bool) -> dict[str, object]` returns stable `tokenrig` identity.

- [ ] **Step 1: Write failing path and identity tests** for missing input, path escape, unsupported suffix, output confinement, and health identity.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/adapters/test_tokenrig_runtime_contract.py`; expect import failure because the runtime module is absent.
- [ ] **Step 3: Implement** the dataclass and validation helpers with `Path.resolve()`/`is_relative_to()` confinement and no model imports.
- [ ] **Step 4: Run** the focused tests and confirm all pass.
- [ ] **Step 5: Commit** the contract as `test: define TokenRig adapter boundaries`.

### Task 2: Implement the official runtime wrapper

**Files:**
- Create: `adapters/creative/tokenrig/server.py`
- Create: `adapters/creative/tokenrig/bin/tokenrig`
- Create: `adapters/creative/tokenrig/runtime-manifest.json`
- Modify: `tests/adapters/test_tokenrig_runtime_contract.py`

**Interfaces:**
- `POST /v1/rig` accepts `{"input_path":"mesh.glb","output_path":"rigged.glb"}` and exports the official `demo.py` result.
- `GET /health` returns stable identity and readiness; `GET /v1/models` returns `tokenrig`.
- The adapter operations are exactly `prepare|verify|start|health|infer|stop|verify-release`.

- [ ] **Step 1: Add failing tests** asserting adapter operation validation, release digest checks, required no-shared-path markers, and server request routing.
- [ ] **Step 2: Run** the focused tests and confirm the missing server/adapter behavior fails.
- [ ] **Step 3: Implement** a serialized service that imports the official `demo.py` components, uses model-owned roots, and rejects requests outside those roots.
- [ ] **Step 4: Implement** `prepare` with a Python 3.11 venv, CUDA Torch install, official dependency install including `flash-attn --no-build-isolation`, and pinned HF snapshot downloads; implement `verify` and lifecycle operations.
- [ ] **Step 5: Hash all release files**, write the immutable manifest, update the workload runtime release and definition fingerprint, and run focused tests plus catalog load.
- [ ] **Step 6: Commit** the runtime release and lock updates as `feat: add isolated TokenRig runtime`.

### Task 3: Deploy and qualify on Spark2

**Files:**
- Create: `inventory/reports/model-definitions/tokenrig-single-prepared.json`
- Create: `inventory/reports/model-definitions/tokenrig-single-verified.json`
- Create: `docs/audits/2026-08-03-tokenrig-runtime-qualification.json`
- Modify: `inventory/reports/model-definitions.json`
- Modify: `docs/model-profile-overview.md`
- Modify: `docs/superpowers/plans/2026-08-03-phase4-model-definition-rollout.md`

**Interfaces:**
- Spark2 release path: `/opt/spark/model-adapters/tokenrig-single/releases/<manifest-sha256>/`.
- Model-owned roots: `/srv/models/snapshots/tokenrig-single`, `/srv/models/runtime-cache/tokenrig-single`, `/srv/models/inputs/tokenrig-single`, `/srv/models/outputs/tokenrig-single`.

- [ ] **Step 1: Stop Mia worker/head in the required order**, deploy the digest-qualified release, and record Spark2 boot ID.
- [ ] **Step 2: Run `prepare` and `verify`**, recording source/checkpoint files, dependency versions, disk use, and venv identity.
- [ ] **Step 3: Run API health and one official example mesh**, then validate output GLB has a skeleton hierarchy and per-vertex weights normalized to approximately 1.0.
- [ ] **Step 4: Run** three start/health/stop/verify-release cycles and a no-autostart reboot gate when authorization permits.
- [ ] **Step 5: Add** prepared/verified evidence only for gates actually observed; leave maturity planned if runtime or quality fails.
- [ ] **Step 6: Restore** Mia worker first/head second, run full tests, commit evidence, and push `main`.

### Task 4: Acceptance and exact-profile gates

**Files:**
- Modify: `inventory/reports/model-definitions/tokenrig-single-verified.json`
- Create if accepted: `inventory/reports/model-definitions/tokenrig-single-accepted.json`
- Modify if accepted: `inventory/reports/accepted-cluster-profiles.json`, `docs/audits/2026-08-03-tokenrig-acceptance.json`

- [ ] **Step 1: Run** quality fixtures across representative meshes and preserve skeleton/weight metrics.
- [ ] **Step 2: Run** thermal, capacity, memory-recovery, and concurrent-load gates with TokenRig alone.
- [ ] **Step 3: Run** exact `rigging` co-residency with the accepted DeepSeek single runtime only after both independent runtimes pass their standalone gates.
- [ ] **Step 4: Promote to accepted only when all required gates are true and `Catalog.load` validates the evidence chain.
