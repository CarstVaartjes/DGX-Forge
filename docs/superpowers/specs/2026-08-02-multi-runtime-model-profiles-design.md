# Multi-Runtime Model Profiles Design

Date: 2026-08-02
Status: approved

## Purpose

Make every requested model runnable on the two-DGX-Spark platform without forcing incompatible model families through one inference engine. DeepSeek Flash 0731 remains the default agent model. The other models are required profiles: `secondary` controls selection priority, not delivery scope.

The developer machine owns the creative pipeline and decides which model to call. The Sparks expose model capabilities and generated artifacts; they do not own the end-to-end asset workflow.

## Required model set

The concise [model capacity overview](../../model-capacity-overview.md) compares official releases, preferred Spark-optimized paths, published memory evidence, placement, and validation status.

| Model | Pipeline function | Priority |
| --- | --- | --- |
| DeepSeek-V4-Flash-0731 | Default agent, reasoning, tool use, and pipeline control | Default |
| Nemotron 3 Super 120B-A12B | Officially optimized alternative agent for reasoning, tools, and long-context work | Required alternative |
| Nemotron 3 Nano Omni 30B-A3B | Lightweight multimodal agent that can remain available on one Spark while the other runs a creative model | Required lightweight alternative |
| Qwen-Image | Text-to-image concepts and clean reference images | Essential |
| Qwen-Image-Edit-2511 | Alternate views, corrections, material edits, and texture-projection images | Essential |
| Pixal3D | Primary high-fidelity image-to-3D geometry and PBR generation | Essential |
| TRELLIS.2 4B | Alternative image-to-3D generation and Pixal3D foundation | Essential |
| Qwen3-VL-8B-Instruct | Turntable evaluation, defect detection, prompt rewriting, and candidate ranking | Recommended |
| SkinTokens / TokenRig | Skeleton and skin-weight generation | Recommended for animated assets |
| Step1X-3D | Independent geometry-plus-texture alternative | Secondary, required |
| TripoSG | Fast draft and high-volume image-to-geometry generation | Secondary, required |
| Hunyuan3D-Omni | Controlled 3D generation from image, point, voxel, bounding-box, or pose inputs | Secondary, required |

## Decision

Use a common profile contract with runtime-specific adapters. A profile describes placement, conflicts, storage, lifecycle, health, and acceptance behavior. Its adapter preserves the best loader for the model family.

Do not make ComfyUI, Diffusers, vLLM, or any other single runtime the platform-wide loader. ComfyUI may later run on the developer or external service host as a client, but it is not the source of truth for Spark process lifecycle or profile state.

## Runtime adapter contract

Every adapter implements these operations:

```text
prepare -> verify -> start -> health -> infer -> stop -> verify-release
```

| Operation | Required behavior |
| --- | --- |
| `prepare` | Download or synchronize pinned artifacts to local NVMe without starting inference. |
| `verify` | Validate source/image pins, model manifests, architecture, dependencies, free disk, and declared placement. |
| `start` | Start only the declared processes and mounts; distributed profiles obey their rank order. |
| `health` | Prove model identity and runtime readiness, not only that a TCP port is open. |
| `infer` | Accept the profile's declared request schema and write outputs to its declared local artifact path. |
| `stop` | Drain where a gateway exists, terminate within the profile timeout, and retain diagnostic logs. |
| `verify-release` | Prove processes exited and available memory returned within the configured tolerance. |

The controller treats adapters uniformly but does not translate one model family's internal launch commands into another's. Each adapter remains directly operable over SSH for diagnosis.

## Loader and placement matrix

| Profile family | Preferred loader | Placement | Residency |
| --- | --- | --- | --- |
| DeepSeek 0731 service | Audited MiaAI-Lab/Anemll vLLM | both Sparks, TP=2 over NCCL | exclusive, persistent |
| DeepSeek 0731 GGUF | audited DS4 GB10/Spark CUDA build | one Spark by default; optional two-Spark TCP layer pipeline | memory-mapped |
| Nemotron 3 Super 120B-A12B | NVIDIA DGX Spark vLLM NVFP4 playbook; TensorRT-LLM comparison lane | either single Spark | persistent, single-exclusive initially |
| Nemotron 3 Nano Omni 30B-A3B | NVIDIA DGX Spark vLLM playbook with BF16 correctness and FP8/NVFP4 optimized lanes | either single Spark | persistent; shareable only after combined-load tests |
| Qwen-Image | accepted ModelOpt NVFP4 SGLang Spark path; official Diffusers as non-serving correctness oracle | either single Spark | persistent, fully resident |
| Qwen-Image-Edit-2511 | accepted Nunchaku NVFP4 or ModelOpt FP8 Spark path, selected by quality and performance; DiffSynth as oracle | either single Spark | persistent, fully resident |
| Pixal3D | audited Spark-native Pixal3D/TRELLIS.2 build with official fully resident or staged mode | either single Spark | fully resident; official staged mode as fallback |
| TRELLIS.2 4B | audited CUDA 13/ARM64 DGX Spark build of the official Microsoft pipeline | either single Spark | fully resident |
| Qwen3-VL-8B-Instruct | accepted GB10-native vLLM or SGLang build with optimized vision attention | either single Spark | persistent server with paged KV cache |
| SkinTokens / TokenRig | audited FP16 Spark integration or GB10-native TokenRig build | either single Spark | persistent Qwen3-0.6B plus FSQ-CVAE |
| Step1X-3D | GB10-native build of the official Step1X geometry and texture pipelines | either single Spark | sequential stage residency |
| TripoSG | GB10-native build of the official TripoSG Diffusers pipeline | either single Spark | persistent lightweight worker |
| Hunyuan3D-Omni | GB10-native official runtime with accepted FlashVDM acceleration | either single Spark | persistent lightweight worker |

`either single Spark` means the controller may place the profile on Spark 1 or Spark 2 only when its complete verified cache and compatible image exist on that node. It never migrates a live request.

## Loader-specific rules

### DeepSeek with Mia/vLLM

- Both nodes keep the complete verified Hugging Face snapshot on local NVMe.
- vLLM partitions runtime tensors across TP rank 0 and rank 1; the two 128 GB unified-memory domains do not become one coherent 256 GB address space.
- Start Spark 2's worker before Spark 1's head; stop the head before the worker.
- Use explicit fabric interfaces, HCAs, GID indexes, offline cache mode, capacity limits, and pinned sampling presets.
- This is the default DeepSeek service because it uses the validated NCCL/RoCE fabric and supports concurrent API serving.

### DeepSeek with DS4

- Store a pinned GGUF on local NVMe and use DS4's `mmap` path.
- Prefer CUDA registration of the mapped pages; record whether startup used registered no-copy mappings or a copy fallback.
- Do not set `DS4_CUDA_COPY_MODEL` by default because it may create an unnecessary second resident copy.
- Use one Spark for the lighter-agent profile. Treat DS4's documented two-host TCP layer pipeline as a separate experimental profile: it divides layers and KV state but adds an inter-node hop to every decoded token and does not use NCCL tensor parallelism.
- SSD streaming paths documented for other backends are not assumed valid for Spark CUDA.

### Nemotron

- Keep DeepSeek 0731 as the default agent; Nemotron profiles are explicit alternatives rather than an automatic replacement.
- Start Nemotron 3 Super from NVIDIA's DGX Spark NVFP4 vLLM recipe. Pin its Marlin/CUTLASS MoE backend, FP8 KV-cache setting, MTP setting, reasoning parser, tool-call parser, context, and concurrency limits. TensorRT-LLM is a measured comparison lane, not an assumed upgrade.
- Start Nemotron 3 Nano Omni with the official NVIDIA Spark vLLM recipe. Preserve BF16 as the semantic reference and evaluate the official FP8 and NVFP4 artifacts separately.
- Nano Omni is the first candidate for a lightweight resident agent beside a creative profile on the other Spark. It still begins as `single-exclusive`; only a recorded pairwise co-residency test can make it `single-shareable`.
- Super and Nano expose OpenAI-compatible endpoints and receive the same pinned sampling, reasoning, tool-use, concurrency, and long-context admission controls as DeepSeek.

### Qwen image generation and editing

- Use official Diffusers output only as the correctness oracle.
- Serve Qwen-Image through the accepted ModelOpt NVFP4 SGLang Spark path. Serve Qwen-Image-Edit through the accepted Nunchaku NVFP4 or ModelOpt FP8 path, selected by the cluster's quality, memory, and throughput results.
- Keep DiffSynth as the Qwen-Image-Edit-2511 compatibility reference and as an offload fallback. Its staged and disk-offload modes are not enabled merely because they use less CUDA allocator space.
- Cache-based denoising, quantization, Lightning/distilled checkpoints, or approximate step skipping are separate profiles because they may change output quality.
- SGLang's documented multi-GPU diffusion modes do not establish two-host Spark support. Cross-host execution remains disabled until a strict fabric-only acceptance test proves it; one Spark has sufficient capacity for the requested image models.

### Pixal3D and TRELLIS.2

- Use each official repository and checkpoint as the acceptance oracle, while the deployed profile uses an audited CUDA 13, ARM64, GB10 Spark build.
- Run the standard fully resident path first. Pixal3D's official `--low_vram` mode may stage components by pipeline phase when allocator headroom or co-residency requires it.
- CPU offload does not create a second physical memory pool on GB10 unified memory. It may relieve CUDA allocator pressure, but total host memory remains the admission constraint.
- Multi-node code in these repositories is training/data-tooling support, not evidence of distributed inference. Initial inference is single-Spark.
- Community ComfyUI and low-memory forks are candidates only after source, license, checkpoint, kernel, output-quality, and maintenance audits. They cannot replace the official baseline before comparison.

### Qwen3-VL

- Serve the 8B Instruct checkpoint through a persistent vLLM or SGLang endpoint rather than loading Transformers for every evaluation.
- Leave explicit space for image/video feature tensors when setting KV utilization and request concurrency.
- Pin processor settings, maximum pixels/frames, context, sampling, structured-output behavior, and the vision attention backend.

### Step1X-3D

- Preserve its two-stage geometry-then-texture flow and official model separation.
- Release or offload a completed stage before the next stage only when the accepted wrapper proves identical artifacts and improved peak memory.
- Published distributed features for training or rendering do not make inference a dual-Spark profile.

### TripoSG, TokenRig, and Hunyuan3D-Omni

- Use persistent single-Spark workers to avoid reloading weights for each asset.
- TripoSG uses its official Diffusers pipeline and separately verified RMBG dependency.
- TokenRig loads both the autoregressive rigging checkpoint and the SkinTokens FSQ-CVAE; its output test must validate skeleton hierarchy and normalized skin weights, not only GLB syntax.
- Hunyuan3D-Omni serves through the accepted FlashVDM lane. The official non-FlashVDM path remains its correctness oracle and diagnostic fallback.

## Optimized artifact policy and current survey

Every model keeps a non-serving correctness lane based on its official upstream checkpoint and runtime. The deployed and user-selectable profile always uses the best accepted DGX Spark-optimized path available for the exact model. An optimized checkpoint, quantization, kernel fork, or Spark-specific container remains quarantined only until it proves equivalent enough for its declared use; after acceptance it replaces the generic lane as the normal profile. Reduced memory use alone is not sufficient.

If no exact Spark path is available, implementation produces and benchmarks an ARM64/CUDA 13/GB10-native build of the official runtime. The model is not considered complete merely because a generic upstream command happens to run. The generic lane remains available only for qualification, regression comparison, and recovery diagnostics.

Candidate status has four meanings:

1. **Official Spark path:** NVIDIA or the model owner publishes a DGX Spark recipe or artifact for the exact model.
2. **Spark community path:** a Spark-specific integration exists, but source, image, checkpoint, licensing, and results require independent audit.
3. **Upstream optimization:** the exact model has an optimized artifact or mode, but two-Spark or GB10 validation is not established.
4. **No exact Spark path found:** the current primary-source survey found no maintained optimization for the exact requested model; this is not evidence that none can exist.

| Model | Best optimized candidate found on 2026-08-02 | Status and adoption rule |
| --- | --- | --- |
| DeepSeek-V4-Flash-0731 | Mia's dual-Spark vLLM recipe with MTP and padded `nvfp4_ds_mla`; NVIDIA `DeepSeek-V4-Flash-NVFP4`; `Entrpi/ds4-on-spark` | Mia remains the first dual-Spark service candidate. NVIDIA's NVFP4 card demonstrates a larger TP configuration, so TP=2 is unproven. The DS4 Spark fork and concurrency patch are community comparison lanes only. |
| Nemotron 3 Super 120B-A12B | NVIDIA's exact NVFP4 checkpoint and DGX Spark vLLM/TensorRT-LLM playbook | Official Spark path and preferred initial profile. Validate the pinned Marlin/CUTLASS, FP8 KV, MTP, reasoning, and tool settings on this cluster. |
| Nemotron 3 Nano Omni 30B-A3B | NVIDIA's DGX Spark vLLM BF16/FP8/NVFP4 recipes and exact FP8 artifact | Official Spark path. BF16 is the semantic reference; FP8 and NVFP4 compete on quality, memory, and throughput. |
| Qwen-Image | `lmsys/qwen-image-modelopt-nvfp4-sglang` plus NVIDIA's published NVFP4-on-Spark path | Official/upstream Spark path. Compare against official BF16 Diffusers using fixed prompt, text-rendering, and identity fixtures before promotion. |
| Qwen-Image-Edit-2511 | Nunchaku SVDQuant W4A4/NVFP4 build reporting DGX Spark measurements; ModelOpt FP8 transformer; community FP8 checkpoint | Exact Spark community and upstream optimized paths. Audit Nunchaku and compare edit fidelity and protected-region preservation against BF16 before promotion. |
| Pixal3D | Official `--low_vram` staging; Super-Idol-Master Spark integration | Upstream optimization plus recent community Spark integration. The official fully resident path remains the reference; the community ARM64 patches are audited independently. |
| TRELLIS.2 4B | `dgx-trellis2` and `Trellis2-DGX-Spark-Docker` | Spark community paths. Use them as CUDA 13/ARM64 build references, not as trusted deployment pins, until reproducibility and output parity pass. |
| Qwen3-VL-8B-Instruct | vLLM/SGLang FlashAttention path; no exact official 8B NVFP4 Spark artifact found | Upstream optimization. Do not substitute NVIDIA's different-size Qwen3-VL NVFP4 artifacts for the required 8B model. Benchmark BF16, FP8, and an audited weight-quantized 8B candidate if available. |
| SkinTokens / TokenRig | FP16 Spark integration in Super-Idol-Master | Spark community path. No dedicated exact-model optimized loader was found; audit the integration while retaining official TokenRig as the non-serving correctness oracle. |
| Step1X-3D | Official sequential geometry/texture loading and offload controls | No exact Spark path found. Build the official runtime for ARM64/GB10 and measure phase release rather than assuming a community quantization. |
| TripoSG | Official lightweight Diffusers pipeline | No exact Spark path found. Its published memory requirement already makes single-Spark serving practical; produce and profile the GB10-native build before declaring the profile ready. |
| Hunyuan3D-Omni | Official FlashVDM mode | Upstream optimization. A Spark container for Hunyuan3D 2.1 is useful as an ARM64 build reference but is not the requested Omni model and cannot replace it. |

Before an optimized lane becomes selectable, its checked-in evidence must include immutable source, container, checkpoint, and quantization-recipe pins; proof of `aarch64` and GB10 `sm_121` compatibility; offline startup; model-specific output comparison; memory and throughput measurements; license and provenance review; and three clean lifecycle cycles. A community claim or benchmark is discovery evidence, never an acceptance result for this cluster. Once a lane passes, the profile points to it by default; users do not have to opt into Spark optimization manually.

## Memory and residency policy

Each Spark remains an independent 128 GB unified-memory host. The admission calculation is per node:

```text
resident weights
+ replicated encoders and processors
+ KV cache or diffusion/3D activations
+ CUDA graphs, kernels, and scratch space
+ container and operating-system headroom
<= measured safe per-node limit
```

Profiles have one of these placement classes:

1. `dual-exclusive`: reserves both Sparks, such as the Mia DeepSeek profile.
2. `single-exclusive`: uses one Spark and forbids other GPU profiles there until measured otherwise.
3. `single-shareable`: may coexist with an explicitly listed profile after combined-load acceptance.
4. `dual-pipeline-experimental`: uses both hosts through a non-NCCL model-specific pipeline, such as DS4 TCP.

All profiles begin as exclusive on their selected node. Co-residency is enabled only by a checked-in compatibility record containing clean baseline memory, each standalone peak, combined startup order, combined peak, sustained-run result, thermal result, stop/restart recovery, and output-quality results for both profiles.

## Storage layout

Each Spark uses local NVMe:

```text
/srv/models/
|-- snapshots/       immutable HF, safetensors, GGUF, and auxiliary checkpoints
|-- manifests/       revisions, filenames, sizes, hashes, and license metadata
|-- runtime-cache/   writable JIT, kernel, vLLM, SGLang, and framework caches
`-- outputs/         generated images, meshes, textures, rigs, and reports
```

- Snapshot mounts are read-only during serving where the runtime permits.
- Writable runtime caches and generated outputs use separate mounts.
- A profile verifies every primary and auxiliary checkpoint before offline start.
- The NAS may archive or distribute artifacts later, but it is never the live checkpoint, JIT, KV, temporary-latent, or output-work path.
- A model needed on either Spark is synchronized and verified independently on both nodes before it is declared portable.

## API and pipeline boundary

The developer machine orchestrates the asset pipeline. Caddy and the future control host advertise capabilities but do not compose the creative workflow.

The platform exposes two endpoint classes:

- OpenAI-compatible endpoints for DeepSeek, Nemotron, and Qwen3-VL, and for image runtimes only where the selected upstream provides a compatible API.
- Typed job endpoints for image generation, image editing, 3D generation, texturing, and rigging. These return a job identifier, status, runtime/model identity, pinned parameters, and artifact references.

Generated artifacts remain on Spark-local output storage during initial testing and are retrieved through SSH. The later gateway may provide an authenticated artifact route after size, timeout, and NAS-transfer behavior are measured.

## Switching and failure behavior

Before a switch, the controller checks the target's placement, declared conflicts, cache manifest, free memory, free disk, boot IDs, runtime image, and required fabric state. A distributed target additionally requires the strict fabric test state.

A failed start or failed quality gate leaves every process started for the target stopped. It does not delete snapshots, runtime caches, inputs, outputs, or diagnostic logs. The prior heavyweight profile is not automatically restarted.

Distributed and GPU-heavy profiles never auto-start after reboot. Lightweight profiles may gain auto-start only through a later explicit design change.

## Acceptance requirements

Every required model receives:

1. pinned source, image, checkpoint, processors, auxiliary models, and generation parameters;
2. a successful native ARM64/GB10 container build or an audited compatible image;
3. offline cache verification;
4. cold-start time, warm-start time, clean memory baseline, peak memory, recovered memory, disk use, and thermal measurements;
5. a deterministic or seed-controlled fixture where the runtime supports it;
6. model-specific semantic output validation;
7. three start-infer-stop cycles with no orphan process or material memory drift; and
8. direct SSH-tunnel validation before any Caddy advertisement.

Model-specific minimums are:

| Model family | Required semantic result |
| --- | --- |
| DeepSeek | correct language, reasoning/tool behavior, no repetition/XML leakage, declared concurrency behavior |
| Nemotron Super / Nano Omni | correct reasoning mode, tool calls, declared multimodal behavior, context and concurrency limits, and no parser leakage |
| Qwen-Image | valid image dimensions plus prompt/content and text-rendering fixture checks |
| Qwen-Image-Edit | instructed edit occurs while protected identity/regions remain within the fixture tolerance |
| Pixal3D / TRELLIS.2 / Step1X-3D / TripoSG / Hunyuan3D-Omni | valid nonempty geometry; declared texture/PBR channels where supported; rendered turntable acceptance |
| Qwen3-VL | expected defect classification, ranking, and structured response for pinned turntable fixtures |
| TokenRig | nonempty acyclic skeleton, valid joint references, normalized bounded skin weights, and loadable rigged artifact |

Availability means every required profile can be selected, started, used, stopped, and reselected reproducibly. It does not mean all models remain resident simultaneously.

## Research snapshot

The design was checked on 2026-08-02 against these upstream source snapshots. They are research inputs, not deployment pins; implementation audits resolve immutable production pins and image digests.

| Project | Reviewed commit |
| --- | --- |
| DS4 | `54b36ed9ba42da31b24f2d1a5feb075c2475dbb1` |
| MiaAI-Lab dual Spark | `b131b2a22164675890dd1465fd8862b5cfb6ff13` |
| Qwen-Image | `6b5e1f5cec987d404be5ac6657db3b9aacb56a89` |
| SGLang | `8d106c3d79ef885f2fc0684f1915ebc404acfbe8` |
| DiffSynth-Studio | `6e2b14bc73ff317229b2a28487fe09250bbf463f` |
| Pixal3D | `cdbb2bbffbf4e6f298b5f2af3d1d76a8d823d2af` |
| TRELLIS.2 | `75fbf0183001ed9876c8dbb35de6b68552ee08bd` |
| SkinTokens | `273b691d35989d71cd17ff2895fdc735097b92d1` |
| Step1X-3D | `cb5ac944709c6c913109070c7b90c3447f57f3d4` |
| TripoSG | `fc5c40990181e2a756c4e0b1c2f4d6b5202faf8c` |
| Hunyuan3D-Omni | `4d47c0cc2bd0c4281963a7314ab330a5af36bfa8` |

## References

- [DS4](https://github.com/antirez/ds4)
- [MiaAI-Lab DeepSeek dual-Spark recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
- [NVIDIA DeepSeek-V4-Flash NVFP4](https://huggingface.co/nvidia/DeepSeek-V4-Flash-NVFP4)
- [DS4 on Spark](https://github.com/Entrpi/ds4-on-spark)
- [NVIDIA Nemotron DGX Spark playbook](https://build.nvidia.com/spark/nemotron)
- [NVIDIA vLLM DGX Spark playbook](https://build.nvidia.com/spark/vllm/instructions)
- [NVIDIA Nemotron 3 Super 120B-A12B NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4)
- [NVIDIA Nemotron 3 Nano Omni 30B-A3B Reasoning FP8](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8)
- [Qwen-Image](https://github.com/QwenLM/Qwen-Image)
- [Qwen-Image ModelOpt NVFP4 for SGLang](https://huggingface.co/lmsys/qwen-image-modelopt-nvfp4-sglang)
- [NVIDIA: NVFP4 Qwen-Image on DGX Spark](https://blogs.nvidia.com/blog/dgx-spark-and-station-open-source-frontier-models/)
- [Nunchaku Qwen-Image-Edit-2511](https://huggingface.co/stuqiu/nunchaku-qwen-image-edit-2511)
- [SGLang Diffusion](https://docs.sglang.io/docs/sglang-diffusion)
- [DiffSynth-Studio](https://github.com/modelscope/DiffSynth-Studio)
- [Pixal3D](https://github.com/TencentARC/Pixal3D)
- [TRELLIS.2](https://github.com/microsoft/TRELLIS.2)
- [NVIDIA forum: TRELLIS.2 on DGX Spark](https://forums.developer.nvidia.com/t/trellis-2-on-dgx-spark/355816)
- [dgx-trellis2](https://github.com/raziel2001au/dgx-trellis2)
- [TRELLIS.2 DGX Spark Docker](https://github.com/dr-vij/Trellis2-DGX-Spark-Docker)
- [Super-Idol-Master DGX Spark integration](https://github.com/SidneyArt/Super-Idol-Master)
- [Qwen3-VL-8B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct)
- [SkinTokens / TokenRig](https://github.com/VAST-AI-Research/SkinTokens)
- [Step1X-3D](https://github.com/stepfun-ai/Step1X-3D)
- [TripoSG](https://github.com/VAST-AI-Research/TripoSG)
- [Hunyuan3D-Omni](https://github.com/Tencent-Hunyuan/Hunyuan3D-Omni)
- [Hunyuan3D 2.1 DGX Spark Docker build reference](https://github.com/dr-vij/Hunyuan3D-2.1-DGX-Spark-Docker)
- [NVIDIA DGX Spark NGC best practices](https://docs.nvidia.com/dgx/dgx-spark/ngc.html)
- [NVIDIA DGX Spark clustering](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
- [NVIDIA DGX Spark playbooks](https://github.com/NVIDIA/dgx-spark-playbooks)
