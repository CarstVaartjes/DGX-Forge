# Mia DeepSeek Flash 0731 Dual-GPU node Runtime Design

**Date:** 2026-08-02

**Status:** Approved for implementation by the user's “start” instruction

**Scope:** First executable Model Definition only; other model families and DS4 remain in the multi-runtime roadmap

## Outcome

Make the existing `deepseek-agent-dual` Model Definition runnable and
qualifiable on the two Vonk Forge GPU nodes using the MiaAI-Lab dual-GPU node approach. The
client-facing model name remains `deepseek`, and the existing
`agent-full-dual` Cluster Profile remains the default profile.

This milestone ends only when the exact definition can be prepared, verified,
started worker-first, queried through the head's loopback OpenAI API, stopped
head-first, and accepted from recorded evidence. Merely pulling an image or
receiving an HTTP response is not acceptance.

## Decisions

### Runtime and immutable pins

Use the currently audited Mia recipe, not the older provisional source pin:

| Artifact | Pin |
|---|---|
| Mia source | `MiaAI-Lab/DeepSeek-v4-Flash-draft-model-2x-Vonk Forge-GPU node@b131b2a22164675890dd1465fd8862b5cfb6ff13` |
| Runtime image | `ghcr.io/anemll/draft-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8` |
| Checkpoint | `deepseek-ai/DeepSeek-V4-Flash-0731@9e165c30e2704aec5d9d593cce3eebd58bbef1cb` |
| Precision/runtime | model checkpoint's audited quantization with `nvfp4_ds_mla` KV cache |
| Parallelism | TP=2, PP=1, `mp`, one rank per GPU node |

The source pin supersedes `914c35bd...`: `b131b2a...` is the audited research
snapshot and retains the Anemll `0.1.1` runtime after upstream reverted its
temporary alternative image. Mutable tags, repository default branches, and
Hugging Face `main` are never used during preparation or serving.

The model's `encoding/encoding_dsv4.py` is a required checkpoint artifact. It
is pinned and verified with the same revision as the weight shards. The model
uses remote code, so no unpinned model Python may execute.

### Container and host-data boundary

Runtime code and dependencies stay in the pinned container. Persistent data is
held in explicit host bind mounts:

```text
/srv/models/
|-- snapshots/deepseek-v4-flash-0731/     immutable verified snapshot
|-- manifests/deepseek-v4-flash-0731.json verification record
|-- runtime-cache/deepseek-agent-dual/     writable JIT/runtime cache
|-- outputs/deepseek-agent-dual/           quality and inference evidence
`-- logs/deepseek-agent-dual/              retained lifecycle logs
```

The snapshot is mounted read-only while serving. Runtime cache, output, and log
mounts are separate and writable. Model files are not stored in container
layers or anonymous Docker volumes.

The snapshot is mounted inside the container at
`/models/deepseek-ai/DeepSeek-V4-Flash-0731` and the runtime uses that local
path rather than a Hugging Face model ID:

```text
DVONK_MODEL=/models/deepseek-ai/DeepSeek-V4-Flash-0731
DVONK_ENCODING_FILE=/models/deepseek-ai/DeepSeek-V4-Flash-0731/encoding/encoding_dsv4.py
VLLM_CACHE_ROOT=/runtime-cache/vllm
FLASHINFER_WORKSPACE_BASE=/runtime-cache/flashinfer
```

The last two paths are writable mounts under the declared host runtime cache;
the runtime never attempts to write into the read-only model snapshot.

### Repository layout

The generic controller remains in `src/cluster_profiles`. The Mia-specific
implementation is isolated under:

```text
adapters/deepseek/mia-vllm/
|-- compose.yaml
|-- bin/mia-deepseek-dual
|-- config/
`-- runtime-manifest.json
```

`config/workloads/deepseek-agent-dual.toml` points every lifecycle operation to
the single adapter entry point. The adapter supports:

```text
mia-deepseek-dual prepare|verify|start|health|infer|stop|verify-release worker|head
```

For operations where the role is irrelevant, accepting the explicit role still
keeps the controller ABI uniform and makes accidental cross-node control
impossible. The adapter never opens SSH and never controls the other node.

The adapter release manifest contains hashes of its Compose file, executable,
and static configuration. Its manifest hash is part of the Model Definition
fingerprint so changing executable runtime content invalidates acceptance.

The Workload Definition contract therefore gains two generic fields:

- a repository-relative runtime-release manifest path plus its SHA-256; and
- per-operation deadlines for `prepare`, `verify`, `start`, `health`, `infer`,
  `stop`, and `verify-release`.

Catalog loading verifies the release manifest and every file it names before
calculating the Model Definition fingerprint. Recording a digest without
verifying its repository content is insufficient.

### Distributed ownership and ordering

`vonkctl` is the only cross-node orchestrator:

```text
start: node2 worker/rank 1 -> node1 head/rank 0
stop:  node1 head/rank 0   -> node2 worker/rank 1
```

Mia's upstream cross-node start and stop scripts are reference implementations
only. They are not wrapped because their nested SSH, file synchronization, and
two-rank ownership would conflict with `vonkctl`.

Compose is evaluated independently on each node with `network_mode: host`,
`ipc: host`, 64 GiB shared memory, all GPUs, `/dev/infiniband`, unlimited
memlock, `restart: "no"`, and the exact role/rank. Both nodes receive the same
immutable adapter release before preparation.

Rank-specific `node1.env` and `node2.env` files are generated from
`inventory/cluster.toml`, reviewed, and included in the immutable runtime
manifest. They pin each node's role, fabric addresses, interfaces, HCAs, GID
indices, rendezvous address, and API bind. The adapter refuses a role that does
not match the installed node configuration.

### Networking and API exposure

The rank rendezvous uses the accepted direct fabric and port `25000`. Interface,
HCA, GID index, MTU, and fabric acceptance floors remain those recorded in the
cluster inventory.

The head API binds only to `127.0.0.1:8888`. The worker is headless. Direct LAN
exposure, Caddy, LiteLLM, NAS services, and Tailscale routing are outside this
milestone. Local access uses SSH port forwarding until the external service host
is available.

### Runtime parameters

The initial lane uses Mia's current executable defaults unless explicitly
constrained below:

- `--max-model-len 1048576`
- `--max-num-seqs 6`
- `--max-num-batched-tokens 8192`
- `--gpu-memory-utilization 0.80`
- `--block-size 256`
- `--max-cudagraph-capture-size 36`
- draft-model speculative decoding pinned exactly to
  `{"method":"draft","num_speculative_tokens":5,"draft_sample_method":"probabilistic"}`
- prefix caching, chunked prefill, async scheduling
- DeepSeek v4 tokenizer, reasoning parser, and tool-call parser
- FlashInfer B12X MoE and autotuning
- default thinking enabled with reasoning effort `low`, passed as
  `--default-chat-template-kwargs '{"thinking":true,"reasoning_effort":"low"}'`

`max-num-seqs=6` is a scheduler ceiling, not permission for six simultaneous
1M-token requests. Admission and documentation state that the observed shared
KV pool supports roughly 2.4 full-context requests on upstream's cluster; our
own measured KV capacity becomes authoritative after bring-up. Long-context
tests enforce a derived max-context concurrency ceiling.

All serving starts with `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and the
complete verified snapshot on both nodes. Network download during `start` is an
error.

### Preparation and activation

Preparation is an explicit operation separate from profile switching. It may:

1. install the immutable adapter release on both nodes;
2. pull the pinned image digest;
3. download the pinned checkpoint revision to both nodes;
4. generate and verify per-file manifests; and
5. create only the declared writable directories.

Preparation may not start or stop a serving container. It is an independent
download/install operation and remains available while an unchanged Cluster
Profile is serving: artifact pulls, checkpoint materialization, and offline
validation run in a separate generation while the current generation keeps
serving. Activation is the only operation that may change the serving
generation, and it is explicitly requested after preparation succeeds. One
node must never wait for the other node's full download. Progress and
resumability are required because each node receives approximately 155.4 GiB
of checkpoint artifacts.

Bootstrapping the node-local adapter is a separate developer-machine operation:

```text
scripts/deploy-runtime-release deepseek-agent-dual
```

It verifies the repository manifest, transfers a release to both nodes, verifies
the transferred hashes, and atomically installs it at:

```text
/opt/node/model-adapters/deepseek-agent-dual/releases/<manifest-sha256>/
```

The Workload Definition invokes the absolute digest-qualified adapter path. No
mutable `current` symlink participates in serving. Deployment neither downloads
model artifacts nor starts containers.

After deployment, preparation is exposed as:

```text
vonkctl prepare agent-full-dual
```

It acquires the controller's host lock, refuses any non-stopped or transitional
state, submits both node preparations concurrently, and writes a deterministic
resumable per-node report. Worker-first/head-first ordering applies to runtime
start and stop, not artifact downloads. A failed or interrupted prepare remains
safe to repeat and never changes the active-profile state.

Preparation runs as a deterministic named one-shot Docker job on each node.
The adapter starts or reattaches to that job and persists progress plus final
status under the declared runtime-cache directory. Losing the developer-machine
process or SSH connection does not terminate the node-local download. Re-running
`vonkctl prepare` inspects the same job and resumes or reports its status; a
different release/checkpoint fingerprint is refused while that job exists.
The controller's initial prepare deadline is 86,400 seconds. Reaching that
client-side deadline stops polling but does not kill the preparation job.

The expected checkpoint manifest is committed before node download. It is built
from the pinned Hugging Face revision metadata and Git LFS SHA-256 object IDs,
including all weight shards and `encoding/encoding_dsv4.py`. Node preparation
only verifies downloaded content against that pre-existing expected manifest;
it cannot generate new expected hashes from the files it just downloaded.

Profile activation remains acceptance-gated. The maturity sequence is:

```text
planned -> prepared -> verified -> accepted
```

The workload TOML is the controller-facing declaration while each immutable
adapter release remains independently executable on a GPU node. Consequently its
offline shell entrypoint mirrors the image, checkpoint, and resource pins. A
repository contract test requires those mirrored values to equal the parsed
Model Definition and rejects duplicate literal checkpoint hashes; this is
deliberate offline defense-in-depth, not an unchecked second source of truth.

- `prepared`: exact adapter, image, and complete checkpoint are present on both
  nodes and hashes match.
- `verified`: offline preflight, architecture, fabric, Compose rendering, and
  role checks pass.
- `accepted`: live quality, capacity, performance, thermal, lifecycle, and
  release gates pass, and exact evidence is committed.

Evidence lives at these stable repository paths:

```text
inventory/reports/model-definitions/deepseek-agent-dual-prepared.json
inventory/reports/model-definitions/deepseek-agent-dual-verified.json
inventory/reports/model-definitions/deepseek-agent-dual-verified-correction-<history-position>.json
inventory/reports/model-definitions/deepseek-agent-dual-accepted.json
inventory/reports/accepted-cluster-profiles.json
```

The correction form is used only for a legal `rejected -> verified` transition.
It is immutable, its numeric suffix equals the maturity-history position, and
its predecessor points to the prior verified report. Arbitrary alternative
evidence names remain invalid.

Each definition report names its definition fingerprint, runtime-manifest
digest, image digest, source/checkpoint pins, node boot IDs, timestamps, gate
results, and predecessor report. The accepted-profile index remains the
canonical activation source; `accepted_evidence` configuration fields must
resolve to that canonical index rather than selecting an alternative index.

A packaged `model-definition-evidence` JSON Schema validates all three report
stages. Stage-specific required gates are enforced by `Catalog.load`: prepared
requires artifact and per-node manifest results; verified requires offline,
release, image, architecture, fabric, role, and Compose gates; accepted requires
quality, lifecycle, capacity, performance, thermal, release, and reboot gates.
The fingerprint and immutable pins in every report must equal the current
definition, and each predecessor must be the immediately prior valid report.
Maturity cannot be advanced by adding an arbitrary existing filename.

The existing `minimum_free_memory_bytes = 120000000000` is a pre-start raw
`MemAvailable` floor, not the post-reserve Model Definition budget. Both live
nodes currently exceed it at about 118.2 GiB available. Runtime headroom and KV
capacity are measured separately during acceptance; any resource-envelope
change creates a new fingerprint.

### Lifecycle deadlines

The controller gains operation-specific deadlines. A single 120-second timeout
is insufficient for cold vLLM startup. The initial values are conservative
bounds, not performance targets:

| Operation | Deadline |
|---|---:|
| prepare | 86,400 s |
| verify | 300 s |
| start | 1,800 s |
| health | 120 s |
| infer | 900 s |
| stop | 300 s |
| verify-release | 300 s |

Timeouts are definition data and therefore fingerprinted.

### Acceptance gates

Acceptance requires all of the following on the exact pinned lane:

1. `/v1/models` reports the expected model identity. The 1,048,576-token limit
   is proved by the hashed rendered configuration plus parsed startup logs and
   a bounded long-context request; it is not inferred from `/v1/models`.
2. Deterministic English prompts pass content and script checks at temperature
   zero, with repetition-loop and XML-leak detection.
3. Reasoning content and tool arguments pass exact structural checks for
   `off`, `low`, `high`, and `max` effort where supported.
4. Streaming and an OpenAI-compatible tool-use request pass. Integration with
   a particular developer-machine agent harness is a later client-plane gate.
5. A prompt longer than 411 tokens passes, guarding against the historical
   true-layout regression.
6. Context and concurrency tests respect the measured shared-KV admission
   ceiling rather than combining both maxima blindly.
7. The Mia benchmark sweep records TTFT, prefill, per-stream decode, aggregate
   decode, errors, temperatures, and fabric counters. Initial acceptance floors
   are 35 output tokens/s single-stream decode, 90 aggregate output tokens/s at
   concurrency three, and 1,500 input tokens/s for the 2K-prompt single-stream
   case. These are deliberately below the audited upstream results and become
   replaceable cluster baselines after acceptance.
8. Three complete worker-first start / head-first stop cycles pass.
9. A 15-minute sustained run shows no GPU thermal-throttle reason, remains at
   least 5 C below the device-reported slowdown threshold where exposed, and
   suffers no more than 15% decode-throughput decline from its warmed baseline.
10. Before each role starts, its node-local adapter records `MemAvailable` after
    60 idle seconds in a release-qualified, boot-ID-qualified lifecycle record
    under the runtime-cache directory. After stop, `verify-release` polls every
    five seconds for up to 120 seconds and requires memory to return within
    `stop_memory_tolerance_bytes` (initially 1 GiB) of that durable baseline.
    After a failed start that produced no baseline, release verification records
    `not-started` and still requires container and port absence.
11. Reboot leaves the distributed profile stopped. Verification rejects any
    matching enabled systemd unit, non-`no` Docker restart policy, or surviving
    runtime container; the live reboot test repeats those checks.

Failed activation ends stopped or degraded with retained logs; it does not
delete snapshots, caches, outputs, or evidence and does not auto-start after
reboot.

## Continuous improvement and upgrades

Pins make a working release reproducible; they do not freeze the platform.
Upstream source, runtime images, checkpoint revisions, kernels, and serving
parameters are expected to improve continuously.

Updates follow an explicit candidate-to-production flow:

```text
discover upstream candidate
        |
        v
audit source, image, model, and license changes
        |
        v
create a new immutable candidate fingerprint
        |
        v
prepare and test without replacing accepted evidence
        |
        v
compare quality, capacity, performance, and stability
        |
        +---- rejected: retain the reason and keep production unchanged
        |
        `---- accepted: atomically promote locks and profile evidence
```

An update never mutates an accepted Model Definition in place. Changed pins or
adapter contents produce a different fingerprint and return that definition to
the qualification path. The last accepted runtime remains the rollback target
until the candidate has completed live acceptance. Model snapshots and images
may coexist during qualification because both GPU nodes have sufficient storage.

Routine maintenance should periodically check the Mia repository, runtime image,
DeepSeek checkpoint, vLLM/FlashInfer support, and NVIDIA Vonk Forge GPU node software
releases. Discovery can be automated later; promotion always remains an explicit
recorded operation.

## Deliberately deferred

- DS4 and other DeepSeek Model Definitions
- other requested model families
- Caddy, LiteLLM, browser UI, NAS runtime services, and Tailscale
- automatic checkpoint garbage collection
- historical telemetry/database storage
- public or LAN API binding

## Superseded DeepSeek lane notes

This design supersedes the staged 200K-versus-1M lane matrix in
`docs/superpowers/plans/2026-08-01-deepseek-0731-runtime.md`. The first accepted
Mia definition is the full upstream 1M-capable `deepseek-agent-dual` lane. A
future separate `deepseek-long-dual` ID is unnecessary unless it changes
behavioral pins or admission policy enough to constitute a genuinely different
Model Definition.

## Upstream authorities

- [Mia pinned source](https://github.com/ node/tree/b131b2a22164675890dd1465fd8862b5cfb6ff13)
- [Mia executable Compose recipe](https://github.com/ node/blob/b131b2a22164675890dd1465fd8862b5cfb6ff13/docker-compose.draft.yml)
- [Mia DeepSeek 0731 runtime notes](https://github.com/ node/blob/b131b2a22164675890dd1465fd8862b5cfb6ff13/docs/DEEPSEEK_V4_FLASH_0731.md)
- [Pinned DeepSeek checkpoint](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731/tree/9e165c30e2704aec5d9d593cce3eebd58bbef1cb)
- [NVIDIA Vonk Forge GPU node hardware guide](https://docs.nvidia.com/)
