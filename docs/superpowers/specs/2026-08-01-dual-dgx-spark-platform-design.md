# Dual DGX Spark Model Platform Design

Date: 2026-08-01

## Purpose

Configure two DGX Spark systems as a reliable local model platform. The primary workload is `deepseek-ai/DeepSeek-V4-Flash-0731` running across both Sparks. The platform must also run TRELLIS.2 and additional models through explicit workload profiles, expose an OpenAI-compatible API, provide a browser UI, and support secure Tailscale access later.

This specification uses measurable defaults and acceptance gates. A measured value may replace a provisional threshold only when the command, result, date, and reason are committed to the private inventory or benchmark record.

## Current Environment

- Spark 1 LAN address: `192.168.1.211`
- Spark 2 LAN address: `192.168.1.212`
- Linux user on both systems: `carst`
- Both LAN addresses are static.
- One QSFP/CX-7 cable directly connects matching ports on the two systems. Its part number and 200 Gb/s compatibility must be verified before fabric configuration.
- The administration computer is a Mac using the 1Password SSH agent.
- A dedicated Ed25519 key named `DGX Spark Admin` exists in 1Password, but its public key has not yet been installed on either Spark.
- A Synology DS218+ is available as the preferred always-on Caddy gateway and possible later UI and LiteLLM host. Its LAN address, installed memory, free disk, DSM, and Container Manager versions remain to be inventoried.
- Installed SSD capacity, free disk space, DGX software versions, and `earlyoom` state remain to be inventoried after key access is established.

## Goals

1. Establish secure, key-based administration from the Mac to both Sparks.
2. Update and inventory both systems before changing cluster networking.
3. Configure and validate the direct ConnectX-7 fabric with NVIDIA-supported tooling.
4. Validate NCCL/RoCE communication independently of any model runtime.
5. Serve `deepseek-ai/DeepSeek-V4-Flash-0731` across both nodes with vLLM tensor parallelism.
6. Support explicit switching between DeepSeek lanes, TRELLIS.2, maintenance, and future model profiles.
7. Provide one stable authenticated API endpoint and a browser interface.
8. Add Tailscale access only after the LAN deployment is stable.

## Non-goals

- Kubernetes, Slurm, or Docker Swarm during the initial deployment.
- Serving DeepSeek and TRELLIS.2 concurrently.
- Loading model weights from the NAS during inference.
- Public internet exposure or router port forwarding.
- Automatic operating-system, firmware, container, model, or distributed-profile updates.
- Running unreviewed remote installation scripts directly from a pipe to a shell.
- Hiding low-level Docker, SSH, NCCL, or vLLM behavior behind a custom orchestration daemon.
- Running Caddy, the profile controller, browser UI, LiteLLM, Tailscale ingress, or general-purpose monitoring containers on either Spark.

## Prerequisites and Inventory

Before model installation, the repository records the following for each Spark:

- hostname, LAN address, DGX OS, kernel, firmware, NVIDIA driver, CUDA, Docker, and Compose versions;
- installed and free memory, swap configuration, SSD model, SSD capacity, filesystem, and free bytes;
- `earlyoom` package, enabled, and active state;
- LAN and fabric interface names, MTU, link mode and rate, HCA name, RoCE version, GID index, and fabric IP;
- the resolved values consumed by `NCCL_SOCKET_IFNAME`, `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`, `TP_SOCKET_IFNAME`, and `GLOO_SOCKET_IFNAME`;
- cable part number and supported link rate;
- current boot ID and thermal/throttling state.

The direct back-to-back link has no Ethernet switch, so switch-side PFC, ECN, or DSCP configuration is not required. Both fabric ends must use the same validated MTU. The fabric has no default route and permits traffic only between the two fabric addresses.

The repository is private. Fabric topology and LAN addresses remain in the checked-in inventory only while that is true; any later public release must exclude or sanitize the inventory.

### Quantitative host gates

The following initial gates apply before a heavyweight profile starts:

| Gate | Required value |
| --- | ---: |
| Available memory before DeepSeek start | at least 100 GiB per node |
| Swap in use before DeepSeek start | at most 1 GiB per node |
| Free disk before the first 0731 snapshot download | at least 350 GiB per node |
| Free disk after model, encoder, image, and JIT caches are complete | at least 150 GiB per node |
| Memory recovery after a profile stop | within 5 GiB of the recorded clean baseline within 120 seconds |
| Fabric MTU | identical on both ends; exact value taken from the NVIDIA-validated configuration |

The pinned 0731 revision contains 166,898,660,330 bytes of repository files, including 166,886,535,336 bytes of SafeTensors. Each node stores the complete snapshot even though TP=2 partitions runtime weight allocations. The installed SSD variant is therefore a hard inventory item, and unused Hugging Face revisions and build caches are never allowed to accumulate without a size report.

Upstream recommends disabling `earlyoom` because it can kill a vLLM head or worker during transient unified-memory pressure. The initial host setup records its prior state, then runs `sudo systemctl stop earlyoom` and `sudo systemctl disable earlyoom` on both nodes before any DeepSeek profile. `systemctl is-active earlyoom` and `systemctl is-enabled earlyoom` must both report a non-running/non-enabled state for DeepSeek acceptance. Memory and swap remain monitored; disabling `earlyoom` is not treated as permission to overcommit the hosts.

## Architecture

### Administration plane

The Mac is the trusted administration workstation. The `DGX Spark Admin` private key is held by the 1Password SSH agent. The security property is: no unencrypted private-key material exists in `~/.ssh`, and no private-key file is usable without unlocking the 1Password vault. Only the public key is installed in `carst`'s `authorized_keys` on each Spark.

SSH host aliases provide stable names for the two LAN addresses, select the 1Password agent explicitly, set `IdentitiesOnly yes`, and select only the dedicated DGX key. The first implementation step installs that public key on both Sparks using the existing Linux password and verifies fresh key-authenticated sessions before password authentication is disabled.

Cluster jobs require separate node-to-node SSH credentials. These credentials are generated on the Sparks, are not reused for Mac administration, and are restricted to the private cluster fabric where the supported tooling permits. SSH agent forwarding is not used.

### Compute plane

Spark 1 is the head node and Spark 2 is the worker. DeepSeek runs as one logical vLLM service with tensor parallel size two, pipeline parallel size one, and the `mp` distributed executor. TP=2 is mandatory for this checkpoint: the snapshot is about 155.44 GiB, or about 77.72 GiB of weight payload per rank before runtime workspaces and metadata.

Inter-node model traffic uses the direct ConnectX-7 fabric. Client traffic uses the stable reverse-proxy endpoint on the DS218+. Caddy, the profile controller, browser UI, optional LiteLLM, Tailscale ingress, and any later general-purpose monitoring services run on the DS218+ or another non-Spark container host.

NVIDIA Sync Cluster Assistant is the preferred fabric configuration path because it validates topology, software readiness, addressing, and node-to-node SSH. The official manual two-Spark playbook is the fallback if Cluster Assistant cannot complete the configuration.

### Storage plane

Each Spark keeps its own complete, verified local model cache. In this document, **verified model cache** means:

1. a pinned Hugging Face snapshot revision;
2. a checked-in manifest containing every required relative filename, byte count, and SHA-256 value from repository/LFS metadata;
3. a local verification run that hashes every file and reports no missing, extra-required, size-mismatched, or hash-mismatched file; and
4. the required `encoding/encoding_dsv4.py` file is present and hashes correctly.

The same verified model revision, encoder, container digest, and runtime configuration are present on both nodes before a distributed service starts. `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and `HF_HUB_DISABLE_XET=1` are hard serving requirements after cache preparation; they prevent an incomplete worker cache from silently re-downloading data and saturating its disk.

The NAS may store configuration backups, benchmark results, and optional download archives. It is not mounted into the model, KV-cache, JIT, or generated-artifact hot path. Caddy does make the NAS an explicit availability dependency for client API access, but not for model execution or CX-7 traffic. Text inference traffic is far below the NAS's 1 GbE capacity; large TRELLIS.2 artifacts are measured separately and may use a separately authenticated direct download route if the gateway becomes a demonstrated bottleneck.

### Access plane

Caddy runs on the DS218+ from day one as the stable client endpoint at `https://spark-gateway.home.arpa:8443`. The NAS receives a static DHCP reservation or static LAN address and the name is installed in local DNS. Caddy uses an internal/private CA certificate whose root is installed only on approved clients. It enforces bearer API keys, serves controller status, actively health-checks the advertised upstream, and has two atomic route states:

- **active:** proxy the advertised profile to its firewall-restricted LAN upstream and return HTTP 503 if that upstream becomes unhealthy;
- **draining/maintenance:** reject new inference with HTTP 503 and `Retry-After: 30` while existing proxied requests receive their configured grace period.

Clients and the browser UI always use Caddy, which makes drain and advertise operations real before LiteLLM exists. The vLLM upstream binds on Spark 1's LAN address, but its host firewall permits port 8888 only from the external gateway and local host. Caddy is limited to 0.5 CPU and 256 MiB of memory and may start when the control host boots, always using the fail-closed maintenance configuration until the controller advertises a healthy profile. If the DS218+ cannot run the pinned Caddy image, another non-Spark container host is required; neither Spark is a fallback gateway.

The profile controller is a one-shot container on the same external control host as Caddy. It changes Caddy state through a private container network and uses dedicated restricted SSH keys to invoke a root-owned `spark-nodectl` forced command on each Spark. That command accepts only the explicit runtime operations required by the controller; it does not provide a general shell. Caddy's admin API is reachable only on the private container network and is never exposed on the LAN.

The browser UI is added after the API passes correctness and load gates. The DS218+ is preferred if it has a supported Container Manager installation, at least 4 GiB installed memory, at least 2 GiB available memory before start, and at least 20 GiB free disk. Its UI container is limited to 1 CPU and 2 GiB. If the NAS fails those gates, the UI requires another non-Spark container host with the same minimum resources and limits.

LiteLLM is optional and deferred until routing multiple simultaneously active endpoints provides value. If deployed, the DS218+ must have at least 6 GiB installed memory and 3 GiB available before LiteLLM plus the UI start. LiteLLM is limited to 1 CPU and 1 GiB in addition to the UI allocation.

Tailscale is added after LAN acceptance as a container or signed-package installation on an external gateway host rather than through its convenience `curl | sh` installer. Remote clients use a named Tailscale Service and, where needed, a restricted subnet route protected by grants or ACLs. No Tailscale daemon is required on the Sparks initially, and no Spark API port is exposed directly to the public internet.

### Port and bind map

| Service | Node | Port | Bind/source scope | Authentication |
| --- | --- | ---: | --- | --- |
| Administrative SSH | both | 22/TCP | LAN; approved admin clients | Ed25519 public key |
| Cluster SSH | both | 22/TCP | fabric peer only | separate cluster key |
| Controller SSH | both | 22/TCP | external control-host source only | forced-command controller key |
| Caddy API/status | DS218+ | 8443/TCP | LAN, later Tailscale | private CA TLS plus bearer key |
| Caddy admin API | DS218+ | 2019/TCP | private container network only | controller-network isolation |
| vLLM API upstream | Spark 1 | 8888/TCP | Spark 1 LAN; firewall source NAS and local host only | vLLM API key plus proxy isolation |
| vLLM `mp` rendezvous | both | 25000/TCP | fabric peer only | network isolation |
| NCCL/Gloo/TP runtime traffic | both | runtime-selected | fabric peer only | direct-link firewall isolation |
| TRELLIS.2 upstream | Spark 2 | 7860/TCP | Spark 2 LAN; firewall source NAS only | upstream token plus proxy isolation |
| Browser UI | external host | 3000/TCP | LAN; exact host firewall source list | UI login plus Caddy API key |

No client route exists on the fabric. Because the fabric is a dedicated point-to-point network, its peer-to-peer runtime port range is allowed only between the two recorded fabric IPs rather than exposed on the LAN.

## Runtime Profiles

### DeepSeek bring-up ladder

The three experimental features—speculative decoding, padded NVFP4 KV, and million-token context—are not enabled simultaneously on first boot. They are introduced one at a time:

| Profile | Context ceiling | `max_num_seqs` | KV cache | DSpark | Purpose |
| --- | ---: | ---: | --- | --- | --- |
| `deepseek-baseline` | 16,384 | 1 | FP8 | off | prove TP=2 weight load, encoding, API, and deterministic output |
| `deepseek-dspark` | 16,384 | 1 | FP8 | MTP=5 | isolate speculative decoding and record acceptance |
| `deepseek-nvfp4` | 16,384 | 1 | `nvfp4_ds_mla` | MTP=5 | validate the padded Stage-C NVFP4 workaround, including an 8K prompt |
| `deepseek-agent` | 200,000 | 6 | `nvfp4_ds_mla` | MTP=5 | normal short/mid-context concurrent agent traffic |
| `deepseek-long` | 1,048,576 | derived, maximum 2 | `nvfp4_ds_mla` | MTP=5 | controlled deep-context work |

Only `deepseek-agent` and `deepseek-long` are advertised after the ladder passes. Each reserves both Sparks, starts the worker first, starts the head second, stops the head first, and exposes only the head through Caddy.

The `nvfp4_ds_mla` path is the upstream **padded Stage-C workaround** using the known-good 584-byte sparse-MLA envelope. It is not described as a true-layout NVFP4 kernel. The discarded true-layout experiment failed beyond roughly 411 real prompt tokens; the NVFP4 gate therefore uses at least an 8,192-token prompt and asserts correct sentinel output.

### Capacity and admission parameters

The 128 GB per-node memory figure is marketed unified memory, not wholly available runtime memory. TP=2 partitions roughly 155.44 GiB of SafeTensor payload to about 77.72 GiB per rank, while the OS, CUDA graphs, JIT artifacts, model metadata, and runtime workspaces consume additional memory. Raw subtraction is not used to declare KV capacity.

The adopted upstream 0731 recipe defaults to `gpu_memory_utilization=0.80`, `max_num_batched_tokens=8192`, and `MTP_NUM_TOKENS=5`. One upstream run at utilization 0.835 reported a 2,493,464-token shared KV pool and 2.38 maximum full-context concurrency. The live boot log on this cluster is authoritative.

Let `P` be the `GPU KV cache size` reported by the pinned runtime at boot:

```text
live-token invariant: sum(active prompt and generated tokens) <= P
full-context slots:   Cfull = min(2, floor(P / 1,048,576))
agent worst case:     6 * 200,000 = 1,200,000 tokens
```

The `deepseek-long` configuration renders `max_num_seqs=Cfull`; startup fails if `Cfull < 1`. It never advertises more than two full-context slots. The `deepseek-agent` configuration fixes `max_num_seqs=6` and requires `P >= 1,200,000`. Six simultaneous 1M requests are neither admitted nor claimed. If two full-context slots do not fit according to the live pool, the long profile runs with one slot rather than relying on preemption.

Context and concurrency acceptance tests are coupled to these lanes: six requests are tested only at or below 200,000 live tokens each, while 900K acceptance is tested at the derived `Cfull` limit. One request beyond the configured scheduler limit must queue or receive the documented overload response without killing either rank.

### Runtime pins and sampling

The initial candidate reference is MiaAI-Lab commit `914c35bd7d5607560048e4467c3fdd42e892e297`, model revision `9e165c30e2704aec5d9d593cce3eebd58bbef1cb`, and the Anemll `0.1.1` image resolved to an immutable digest during implementation. Deployment does not use mutable branches, tags, or an unverified image.

The local configuration pins:

- source repository commit;
- model snapshot revision and per-file manifest;
- encoder checksum and installation path;
- container image digest;
- every locally applied patch checksum;
- distributed executor, context, batching, KV, speculative-decoding, CUDA-graph, and NCCL parameters;
- named client sampling presets including temperature, top-p, maximum output tokens, reasoning mode, and seed where supported.

The server uses `--generation-config vllm` and no inherited model-repository generation override. Validation requests carry explicit sampling parameters. The deterministic quality preset uses `temperature=0`, `top_p=1`, a fixed seed where supported, and a fixed output ceiling. The production default follows the model card with `temperature=1.0` and `top_p=1.0`; benchmark presets reproduce the pinned benchmark script exactly. UI and API clients must select a checked-in preset rather than silently invent defaults.

The prebuilt Anemll image is acceptable only after provenance and contents are inspected. If that review is unsatisfactory, the same pinned runtime is built locally from reviewed sources.

### `trellis2`

- Requires every DeepSeek profile to be fully stopped first.
- Runs in its own pinned container/environment on Spark 2.
- Uses local checkpoints and output storage.
- Starts with 512-cubed generation for acceptance testing before higher resolutions.
- Binds its upstream to Spark 2's LAN address with a firewall rule allowing only the NAS and is advertised through Caddy.

### `maintenance`

- Stops all GPU model containers on both nodes.
- Leaves Spark SSH and DGX Dashboard available; external Caddy continues returning its maintenance response.
- Is the required state before OS, firmware, driver, or fabric maintenance.

### Future profiles

Each future model is isolated with declared nodes, exact ports, local cache paths, CPU and memory limits, startup order, health timeouts, stop grace period, log limits, and acceptance tests. A profile is not advertised until its health and output-quality checks pass.

## Workload Controller

There is no NVIDIA-standard model-profile switcher for DGX Spark. The platform therefore uses a thin, project-local controller container over ordinary Docker Compose, Caddy's private admin API, and restricted SSH commands. It is not a daemon and does not hide the underlying commands.

The controller executes as a one-shot container only on the external control host. Its container path `/var/lib/dgx-spark-platform` is a persistent bind mount; `state.json` contains the prior profile, target profile, phase, controller PID, start timestamp, last error, and both Spark boot IDs. The shared lock file is `/var/lib/dgx-spark-platform/switch.lock`.

The controller uses the control host kernel's `flock` on the bind-mounted lock file, not file existence, for mutual exclusion across one-shot container runs. A crashed process automatically releases the kernel lock, so stale contents cannot wedge future operations. If state shows an interrupted transition, `status` reports `recovery-required`; `recover --force` is permitted only after it proves no controller process and no profile container are still active on either Spark.

The controller provides:

- `switch <profile>`
- `start <profile>`
- `stop`
- `status`
- `logs <profile>`
- `doctor`
- `recover --force`

A profile switch performs this sequence:

1. Acquire `flock` and write transition metadata.
2. Load Caddy's draining route over the private container network and confirm new inference receives HTTP 503.
3. Poll the active-request metric for up to 300 seconds by default. The configured grace may be 30–1,800 seconds; expiry is logged.
4. Invoke restricted node commands to stop the head before the worker with a 120-second Compose stop grace.
5. Confirm all target and prior-profile containers exited on both nodes within 60 seconds.
6. Confirm the quantitative memory, swap, and disk gates for the target profile.
7. Validate image digests, model manifests, encoder checksum, offline mode, fabric connectivity, and rendered configuration.
8. Invoke restricted node commands to start target workers before the target head.
9. Wait up to 900 seconds for container and application health checks.
10. Run structural, deterministic output-quality, and profile-specific capacity smoke tests.
11. Load Caddy's active route over the private container network, verify upstream health, and publish the target in controller state.
12. Mark the transition successful and release the lock.

If startup or validation fails, the controller restores Caddy's maintenance route, stops the partial target deployment, preserves logs, and leaves the system in a known stopped state. It does not automatically restart the previous heavyweight workload.

Distributed and GPU-heavy profiles use `restart: "no"`. They never auto-start after a Spark reboot because Compose cannot enforce cross-host worker-before-head order. Caddy may auto-start on the NAS, but it starts fail-closed and returns maintenance or upstream-unhealthy HTTP 503. A Spark boot-ID change causes controller status to report `stopped-after-reboot`; an operator must run `doctor` and explicitly start a profile.

Compose uses explicit project names, health checks, `stop_grace_period: 120s`, and Docker JSON log rotation of `max-size: 50m` and `max-file: 5` per container. The external Caddy container alone may use `restart: unless-stopped`; controller runs use `restart: "no"`. Production overrides contain runtime settings without duplicating base definitions.

## Output-Quality Gate

Health and HTTP success are insufficient. Every DeepSeek lane runs fixed, versioned prompts locally against the restricted vLLM upstream and then through NAS-hosted Caddy. The gate includes:

- an exact deterministic sentinel response;
- an English prose response checked for an expected fact or phrase;
- a small code task executed or compared with its expected result;
- reasoning-content separation for `low`, `high`, and `max` modes;
- a tool-call fixture with asserted function name and JSON arguments;
- an 8K-or-longer prompt with a sentinel beyond token 411 for the padded NVFP4 lane;
- Unicode script ratios that fail unexpected CJK drift in an English-only fixture;
- a repetition detector that fails repeated character runs, repeated n-grams, or low-entropy loops;
- checks that assistant-visible output contains no leaked prompt, schema, or tool XML.

The validation fixture pins sampling parameters and records the runtime digest, source commit, model revision, encoder checksum, and output hash. Agent-harness validation runs separately with fallback models disabled so stale session replay or fallback behavior cannot be mistaken for a model-runtime defect.

## Security

- Install the `DGX Spark Admin` public key on both Sparks, then verify fresh agent-backed sessions before changing SSH authentication.
- Run `sshd -t` before every SSH configuration reload.
- After key verification on both nodes, set `PasswordAuthentication no` and `KbdInteractiveAuthentication no` in a managed drop-in and reload SSH.
- Verify key login again, then verify a connection with public-key authentication disabled is rejected. Retain local console/DGX Dashboard recovery access.
- Do not copy the Mac's private key to either Spark or use SSH agent forwarding.
- Keep the controller's dedicated private key only on the external control host; each Spark restricts its public key with a forced `spark-nodectl` command and source-address rule.
- Keep secrets out of Git, Compose files, logs, process arguments where avoidable, and command histories.
- Store API keys and future Tailscale or LiteLLM credentials in 1Password and render runtime-only secret files under `/run` with mode `0600`.
- Require a bearer API key at Caddy and a separate upstream key at vLLM.
- Rotate API keys under maintenance by temporarily accepting old and new proxy keys, updating and testing clients, then removing the old key and reloading Caddy. Record only key IDs and rotation dates.
- Bind application ports only to the addresses in the port map and enforce the corresponding host firewall rules.
- Run containers without root and with read-only mounts where the GPU/RDMA runtime permits; grant only required devices and capabilities.
- Verify signed images when publishers provide signatures and pin every image by digest.
- If LiteLLM is deployed, use a signed release at version `1.83.7` or later because GHSA-r75f-5x8p-qvmc affects earlier versions; configure a master key and keep its management UI off the public internet.

## Updates and Change Control

Both Sparks must end maintenance on matching supported DGX OS, driver, CUDA, firmware, and container-runtime versions. Initial updates use DGX Dashboard, which NVIDIA recommends over ad-hoc package upgrades.

Updates occur only in `maintenance`:

1. Back up configuration, export the inventory, and record current versions.
2. Review release notes, known issues, and firmware reversibility.
3. Update Spark 2 first.
4. Reboot Spark 2 and validate SSH, DGX Dashboard, GPU visibility, Docker GPU access, storage, and fabric-interface state without starting a distributed profile.
5. Stop if Spark 2 fails; do not update Spark 1.
6. Update and reboot Spark 1.
7. Compare both nodes, then rerun fabric, RDMA, NCCL, container, profile-ladder, quality, and performance gates.

This sequencing provides a detection point before both nodes change; it is not a promise of rollback. Firmware is commonly non-reversible, so firmware changes require a documented vendor recovery path or explicit acceptance that recovery may be roll-forward only.

Model, runtime, image, encoder, and sampling changes use new pins and repeat the same acceptance tests. Floating automatic updates are disabled.

## Observability and Operations

`status` and `doctor` report:

- persisted profile, live profile, transition phase, boot ID, and recovery state;
- NAS reachability, Caddy route/upstream-health state, and last successful advertisement;
- container state and health on both nodes;
- LAN and fabric connectivity, MTU, link rate, and resolved NCCL variables;
- disk bytes free and model-manifest verification time/result;
- available memory, swap use, and `earlyoom` enabled/active state;
- GPU/SoC temperature, clocks, power, and thermal-throttling indicators exposed by DGX-supported tools;
- vLLM model identity, context ceiling, `max_num_seqs`, live KV-pool tokens, and active requests;
- last 100 log lines plus current bounded log sizes;
- last successful structural, output-quality, capacity, and performance test with pin set.

Spark runtime metrics and logs remain on the Sparks initially and are queried by the external controller. Any later Prometheus or centralized logging containers run only on external hosts.

## Failure Handling

- A failed switch ends with Caddy in maintenance and both heavyweight profiles stopped.
- Loss of the worker makes DeepSeek unhealthy; Caddy must stop advertising it.
- Model or encoder verification failure prevents startup; download repair occurs only in an explicit online maintenance operation.
- Fabric or NCCL failure prevents DeepSeek startup but does not block maintenance or single-node TRELLIS.2 diagnosis.
- `earlyoom` is disabled before DeepSeek; vLLM scheduler limits enforce the configured lanes. The platform does not promise automatic OS-pressure draining because the controller is not a daemon.
- If memory or thermal thresholds regress during operation, the operator drains through Caddy, stops the profile, and retains logs; no automatic cache deletion occurs.
- Recovery removes profile containers and ephemeral configuration without deleting verified model caches, manifests, benchmark records, or user outputs.

## Performance Gates

Performance is tested after warm-up with the exact pinned upstream benchmark method. Initial minimums are 70% of the adopted upstream 0731 results:

| Test | Upstream reference | Initial pass floor |
| --- | ---: | ---: |
| 2K prompt, concurrency 1, prefill | 2,563 tok/s | 1,794 tok/s |
| 2K prompt, concurrency 1, decode | 68.8 tok/s | 48.1 tok/s |
| 2,048-token decode, concurrency 1 | 82.4 tok/s | 57.6 tok/s |
| 2,048-token decode, concurrency 3 aggregate | 134.6 tok/s | 94.2 tok/s |
| 2K prompt, concurrency 6 aggregate | 143.7 tok/s | 100.5 tok/s |

The source results came from specific upstream hardware state and profile settings, so they are comparison baselines rather than vendor guarantees. Falling below a floor fails acceptance until the variance is explained and recorded. A 15-minute sustained decode run must show no thermal-throttling flag and no more than 15% reduction between the first and final five-minute median throughput windows.

## Validation Sequence

1. Install the 1Password-managed public key on both nodes and verify fresh key-backed sessions.
2. Validate SSH configuration, disable password and keyboard-interactive login, then verify key login and password rejection.
3. Inventory hardware, storage, software, thermals, interfaces, NCCL variables, cable, and `earlyoom` on both nodes.
4. Enter maintenance; update Spark 2, validate it, then update Spark 1 and compare versions.
5. Disable `earlyoom` on both nodes and verify its state.
6. Configure the ConnectX-7 fabric with NVIDIA Sync Cluster Assistant.
7. Verify bidirectional fabric IP connectivity, matching MTU, link rate, raw RDMA, and NCCL bandwidth.
8. Validate Docker GPU access and image architecture on both nodes.
9. Audit and pin the MiaAI-Lab source, Anemll image digest, patches, encoder, configuration, and sampling presets.
10. Check disk gates; download the pinned snapshot online during maintenance, generate/verify manifests on both nodes, then enforce offline mode.
11. Inventory the DS218+, install the external Caddy and one-shot controller containers, install forced `spark-nodectl` access on both Sparks, and validate TLS, bearer rejection, upstream failure, private admin networking, restricted node control, route switching, state locking, and log limits.
12. Run `deepseek-baseline` and pass structural plus deterministic quality gates.
13. Add DSpark, pass quality gates, and record speculative acceptance.
14. Add padded NVFP4, pass the greater-than-411-token regression and 8K quality gates.
15. Run `deepseek-agent`, verify `P >= 1,200,000`, six concurrent requests with at most 200,000 live tokens each, and overload behavior.
16. Run `deepseek-long`, derive `Cfull` from the boot log, complete a 900K sentinel request at the admitted limit, and verify one excess request queues or rejects safely.
17. Run reasoning, tool-call, streaming, restart, output-quality, and performance gates through Caddy.
18. Stop DeepSeek; verify memory recovery and the clean stopped state.
19. Install and validate TRELLIS.2 at 512-cubed resolution.
20. Switch repeatedly between DeepSeek and TRELLIS.2 and confirm deterministic recovery.
21. Reboot both Sparks while the NAS remains available; confirm Caddy returns maintenance/upstream-unhealthy HTTP 503 and status says `stopped-after-reboot` until an explicit start.
22. Add the browser UI, then optional LiteLLM and Tailscale as separate acceptance steps.

## Acceptance Criteria

- The Mac reaches both Sparks using the 1Password agent with no Linux password, no unencrypted private-key material in `~/.ssh`, and no private-key file usable while the vault is locked.
- Password and keyboard-interactive SSH are disabled only after fresh key sessions pass on both nodes; negative password-auth tests then fail as expected.
- Both nodes have matching supported platform software and pass the numeric memory and disk gates.
- `earlyoom` is stopped and disabled on both nodes before DeepSeek starts.
- The direct fabric passes NVIDIA connectivity, RDMA, NCCL, MTU, and link-rate validation with recorded interface/HCA/GID consumers.
- The verified 0731 model and encoder manifests pass offline on both nodes.
- Each DeepSeek ladder stage passes before the next feature is enabled.
- `deepseek-agent` serves six requests of at most 200K live tokens each with `P >= 1,200,000`.
- `deepseek-long` reports the 1,048,576-token ceiling, derives one or two admitted slots from the live KV pool, and completes the 900K sentinel test without rank failure.
- Deterministic, script/language, repetition, XML-leakage, reasoning, streaming, and tool-call quality gates pass both directly and through Caddy.
- All performance floors pass, and the 15-minute run has no thermal throttling or greater than 15% sustained regression.
- NAS-hosted Caddy is the only client endpoint, enforces TLS and bearer keys, exposes no LAN admin API, and returns HTTP 503 during drains, upstream failures, and post-reboot state.
- The Sparks run only AI/model containers; gateway, controller, UI, LiteLLM, Tailscale ingress, and general monitoring containers run on non-Spark hosts.
- Profile switches are serialized by kernel `flock`, use the numeric timeouts, and fail to a known stopped state.
- After reboot, no distributed/GPU-heavy profile starts automatically.
- TRELLIS.2 produces a valid GLB from a sample image at 512-cubed resolution.
- The browser UI displays only the profile advertised as healthy by the controller.
- No model service is publicly exposed; later remote access is restricted by Tailscale policy.
- Runtime configuration is reproducible from this private repository without committed secrets.

## References

- [NVIDIA DGX Spark user guide](https://docs.nvidia.com/dgx/dgx-spark/)
- [NVIDIA ConnectX-7 Cluster Assistant](https://docs.nvidia.com/sync/latest/cluster-assistant.html)
- [NVIDIA two-Spark networking guide](https://build.nvidia.com/spark/connect-two-sparks/stacked-sparks)
- [NVIDIA DGX Spark update guide](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)
- [DeepSeek-V4-Flash-0731 model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731)
- [MiaAI-Lab dual-Spark 0731 recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark/tree/914c35bd7d5607560048e4467c3fdd42e892e297)
- [MiaAI-Lab 0731 measurements](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark/blob/914c35bd7d5607560048e4467c3fdd42e892e297/docs/DEEPSEEK_V4_FLASH_0731.md)
- [Microsoft TRELLIS.2](https://github.com/microsoft/TRELLIS.2)
- [Docker Compose production guidance](https://docs.docker.com/compose/how-tos/production/)
- [Caddy configuration API](https://caddyserver.com/docs/api)
- [Tailscale Services](https://tailscale.com/kb/1552/tailscale-services)
- [LiteLLM repository and signed releases](https://github.com/BerriAI/litellm)
- [LiteLLM GHSA-r75f-5x8p-qvmc](https://github.com/BerriAI/litellm/security/advisories/GHSA-r75f-5x8p-qvmc)
