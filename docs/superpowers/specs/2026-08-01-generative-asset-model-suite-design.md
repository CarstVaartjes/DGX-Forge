# Generative Asset Model Suite Design

> **Superseded:** The approved [multi-runtime model profiles design](2026-08-02-multi-runtime-model-profiles-design.md) and [model capacity overview](../../model-capacity-overview.md) replace this earlier model catalog, placement survey, and delivery sequence. This file is retained as historical design context only and must not be used as an implementation source.

**Date:** 2026-08-01

## Objective

Extend the two-Vonk Forge-GPU node platform into a local generative-asset model suite
without turning the GPU nodes into orchestration or storage servers.

The developer machine owns the asset pipeline, selects models, switches cluster
profiles, transfers inputs and outputs, and keeps canonical artifacts and
provenance. The GPU nodes run pinned AI workloads and keep verified model caches
plus temporary working data.

DeepSeek-V4-Flash-0731 remains the platform's default general agent. Image,
vision, 3D, and rigging models are explicit tools used by the developer-machine
pipeline.

## Scope and sequencing

This work starts after:

1. secure host and direct-fabric validation;
2. dual-GPU node DeepSeek 0731 acceptance; and
3. initial TRELLIS.2 acceptance.

It runs before the NAS-dependent Caddy, browser UI, LiteLLM decision, and
Tailscale control-plane work. The initial interface is therefore local
developer-machine commands and SSH tunnels.

The future NAS may host the gateway and control plane, but it does not become
the pipeline orchestrator or enter the model-cache, generated-artifact, or
inter-node hot paths.

## Non-goals

- Requiring every model to use both GPU nodes.
- Automatically bin-packing arbitrary models based only on free memory.
- Keeping all models loaded simultaneously.
- Treating a secondary model as optional or unavailable.
- Silently substituting a different model when the selected model fails.
- Presenting a reduced DeepSeek profile as the full dual-GPU node service.
- Making generated files on a GPU node the canonical copy.

## Model catalog

Every listed model is audited, pinned, installed, and independently accepted
before it becomes selectable. Priority controls default routing, not
availability.

| Priority | Model | Pipeline role | Default behavior |
| --- | --- | --- | --- |
| Essential | Qwen-Image | Text-to-image concepts and clean reference images | Default concept-image generator |
| Essential | Qwen-Image-Edit-2511 | Alternate views, consistency repair, material edits, and texture-projection images | Default image editor |
| Essential | Pixal3D | High-fidelity image-to-3D geometry and PBR assets | First default image-to-3D candidate |
| Essential | TRELLIS.2 4B | Alternative image-to-3D foundation and Pixal3D comparison path | Default comparison candidate when explicitly selected |
| Recommended | Qwen3-VL-8B-Instruct | Turntable evaluation, defect detection, prompt rewriting, and candidate ranking | Specialized evaluator |
| Recommended | SkinTokens | Skeleton and skinning generation candidate | Explicit rigging tool |
| Recommended | TokenRig | Skeleton and skinning generation candidate | Explicit rigging tool |
| Secondary | Step1X-3D | Independent geometry-plus-texture generation | Explicitly selectable A/B candidate |
| Secondary | TripoSG | Fast image-to-geometry drafts and batch generation | Explicitly selectable A/B candidate |
| Secondary | Hunyuan3D-Omni | Additional independent 3D generation approach | Explicitly selectable A/B candidate |

SkinTokens and TokenRig are treated as separate workload candidates unless
their authoritative upstream implementations require a coupled deployment.

The image-to-3D capability may run Pixal3D, TRELLIS.2, Step1X-3D, TripoSG, and
Hunyuan3D-Omni against the same input. The developer machine retains every
candidate and its provenance so evaluation remains reproducible.

## Two-layer profile model

### Model workload definition

A workload definition describes one deployable model service:

- authoritative source repository and license;
- pinned source commit, checkpoint revision, and per-file manifest;
- pinned container digest or reproducible ARM64 build inputs;
- supported node topology: GPU node 1, GPU node 2, or both;
- cache, scratch, input, and output paths;
- ports and loopback-only endpoint contract;
- startup, health, quality, stop, and cleanup commands;
- measured memory, disk, startup, thermal, and performance envelope;
- co-location eligibility and incompatibilities; and
- output/provenance schema.

### Cluster profile

A cluster profile is the complete desired workload state of both GPU nodes. It
declares zero, one, or several allow-listed workloads per node and all
distributed reservations.

Examples include:

- full DeepSeek 0731 reserving GPU node 1 and GPU node 2;
- one large generator on one GPU node with the other idle;
- different 3D generators on separate GPU nodes for A/B generation;
- a heavy generator on one GPU node and Qwen3-VL evaluation on the other; and
- multiple smaller workloads on one GPU node only after explicit interference
  acceptance.

A distributed workload reserves both nodes and records its rank assignment,
worker-first startup, head-first shutdown, and fabric requirements.

## DeepSeek default-agent policy

The validated full DeepSeek 0731 profile is the home state of the platform:

- aliases such as `default` and `agent` resolve to the full DeepSeek profile;
- the platform returns to this profile between asset-generation jobs;
- Qwen3-VL is a specialist evaluator, not the default general agent;
- conflicting generator profiles make the default agent explicitly
  unavailable during their run; and
- no fallback or downgrade is hidden from the caller.

The topology audit also investigates a distinct single-GPU node companion agent:

- reduced context, a smaller or quantized checkpoint, or another validated
  lighter DeepSeek runtime may be considered;
- its model identity is distinct from the full dual-GPU node service;
- it must pass separate reasoning, tool-use, context, quality, latency, memory,
  and thermal gates;
- it is enabled only in explicitly accepted cluster profiles alongside a
  generator on the other GPU node; and
- if no safe, sufficiently capable configuration exists, no companion profile
  is published.

## Placement and admission

The controller uses explicit cluster profiles rather than automatic
bin-packing.

Co-location is allowed only after a profile passes:

- aggregate peak-memory and minimum-free-memory gates;
- startup and shutdown ordering tests;
- latency and throughput interference measurements;
- sustained thermal checks;
- output-quality regression checks;
- failure isolation; and
- memory and scratch-space recovery after stop.

Free memory alone never proves that workloads may coexist. A model that can use
both GPU nodes is deployed across both only when its upstream runtime genuinely
supports that topology and measured correctness or performance improves.

## Switching semantics

The developer machine initially runs the profile controller through a
repository-managed `vonkctl` command. The later NAS control plane consumes
the same workload and cluster-profile contracts.

A switch performs:

1. lock acquisition and current-state inspection;
2. admission validation for the requested cluster profile;
3. drain of changed endpoints;
4. stop of changed distributed heads before workers;
5. verification of memory and scratch recovery;
6. startup of desired workers before distributed heads;
7. per-workload health and output-quality gates;
8. publication of only healthy loopback endpoints; and
9. persisted status and provenance on the developer machine.

Unchanged healthy workloads may remain running. A failed transition does not
silently select another model. It finishes in a reported stopped or degraded
state, preserves logs and canonical outputs, and explicitly reports whether
restoration of the DeepSeek home profile succeeded.

`vonkctl status` reports:

- active cluster profile;
- workload and reservation state on each GPU node;
- exact model/runtime identity;
- health and endpoint availability;
- measured memory and disk headroom; and
- the last transition result.

## Developer-machine pipeline

The developer machine:

1. hashes and records source prompts, images, masks, meshes, and parameters;
2. selects a capability and exact model;
3. switches to an accepted cluster profile;
4. opens authenticated SSH tunnels to healthy loopback endpoints;
5. submits work and retrieves outputs;
6. optionally runs alternative generators sequentially or concurrently;
7. invokes Qwen3-VL or another evaluator to inspect turntables and rank
   candidates;
8. invokes SkinTokens or TokenRig when rigging is requested;
9. writes canonical artifacts and provenance locally; and
10. cleans temporary GPU node working files before restoring the DeepSeek home
    profile.

Until the new NAS arrives, the developer machine is the system of record for
images, meshes, textures, turntables, rigs, rankings, and pipeline metadata.

## Artifact provenance

Every generated candidate records:

- input hashes;
- requested capability and exact selected model;
- source commit, checkpoint revision, and container digest;
- cluster profile and node placement;
- parameters, random seed, and runtime versions;
- start/end timestamps and health-gate results;
- output filenames, media types, dimensions or mesh statistics, and checksums;
- evaluator scores and explanations where applicable; and
- whether the DeepSeek home profile was restored.

Secondary models remain fully available through explicit selection. The
pipeline never substitutes one implicitly, and provenance never labels one
model's output as another's.

## Per-model Vonk Forge GPU node audit

Each workload follows the same audit:

1. Resolve the authoritative source, license, checkpoint, and required
   dependencies.
2. Audit install scripts and every host/container write path before execution.
3. Verify Linux ARM64, CUDA, PyTorch, attention/custom kernels, and container
   compatibility.
4. Establish a safe single-GPU node baseline.
5. Investigate real dual-GPU node mechanisms such as model-native distribution,
   torchrun/NCCL, Accelerate, Ray, tensor parallelism, pipeline parallelism, or
   stage splitting.
6. Measure whether two-node execution improves a defined outcome.
7. Measure peak unified memory, disk/cache use, startup time, generation time,
   thermals, scratch use, and cleanup recovery.
8. Verify complete offline restart from pinned local caches.
9. Run output-specific correctness and quality gates.
10. Exercise repeated switching and approved co-location profiles.

Unsupported x86-only or unavailable dependencies are recorded as blockers.
They are not bypassed with unreviewed binaries or mutable images.

## Output acceptance

- **Qwen-Image:** valid deterministic fixtures, requested dimensions, prompt
  alignment, and absence of corrupt or blank output.
- **Qwen-Image-Edit-2511:** requested edits occur while fixture-defined
  protected properties remain stable.
- **3D generators:** valid geometry, bounded mesh statistics, texture/PBR
  checks where claimed, GLB export, and deterministic turntable generation.
- **Qwen3-VL-8B-Instruct:** fixed defect-detection, prompt-rewrite, and
  candidate-ranking fixtures.
- **SkinTokens and TokenRig:** valid skeleton hierarchy, bounded/normalized skin
  weights, and deformation fixtures.

Health endpoints and file existence alone do not constitute acceptance.

## Delivery phases

1. Build the workload-definition schema, cluster-profile schema, and
   developer-machine `vonkctl`.
2. Audit and accept Qwen-Image and Qwen-Image-Edit-2511.
3. Audit and accept Pixal3D and the already introduced TRELLIS.2 profile.
4. Audit and accept Qwen3-VL-8B-Instruct, SkinTokens, and TokenRig.
5. Audit and accept Step1X-3D, TripoSG, and Hunyuan3D-Omni.
6. Audit a single-GPU node DeepSeek companion without changing the full DeepSeek
   default.
7. Build and accept explicit co-location and dual-GPU node cluster profiles.
8. Validate A/B generation, evaluation, rigging, provenance, cleanup, and
   restoration of the DeepSeek home profile from the developer machine.
9. Continue with the NAS-hosted access/control-plane roadmap.

## Acceptance criteria

- Every listed model is available through an accepted workload definition.
  An evidence-backed platform blocker prevents suite acceptance and remains an
  open implementation issue rather than removing that model from scope.
- Essential, Recommended, and Secondary priorities affect defaults only.
- Full DeepSeek 0731 is the default home profile and is restored after
  generation jobs when recovery succeeds.
- A companion DeepSeek profile exists only if single-GPU node acceptance passes.
- Every published cluster profile specifies the complete state of both GPU nodes.
- No untested co-location or inferred dual-GPU node mode is published.
- The developer machine can explicitly select any accepted model, retrieve its
  outputs, and reproduce the recorded run.
- Canonical artifacts survive model failures and profile switches.
- Failed switches never silently substitute a different model.
- The future NAS control plane can consume the same workload/profile contracts
  without relocating pipeline orchestration or model execution.
