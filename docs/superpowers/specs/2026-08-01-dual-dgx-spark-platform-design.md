# Dual DGX Spark Model Platform Design

Date: 2026-08-01

## Purpose

Configure two DGX Spark systems as a reliable local model platform. The primary workload is DeepSeek-V4-Flash-0731 running across both Sparks. The platform must also run TRELLIS.2 and additional models through explicit workload profiles, expose an OpenAI-compatible API, provide a browser UI, and support secure Tailscale access later.

## Current Environment

- Spark 1 LAN address: `192.168.1.211`
- Spark 2 LAN address: `192.168.1.212`
- Linux user on both systems: `carst`
- Both LAN addresses are static.
- One QSFP/CX-7 cable directly connects matching ports on the two systems. Its part number and 200 Gb/s compatibility must be verified before fabric configuration.
- The administration computer is a Mac using the 1Password SSH agent.
- A dedicated Ed25519 key named `DGX Spark Admin` exists in 1Password.
- A Synology DS218+ is available as a possible later control-plane host.

## Goals

1. Establish secure, key-based administration from the Mac to both Sparks.
2. Update and inventory both systems before changing cluster networking.
3. Configure and validate the direct ConnectX-7 fabric with NVIDIA-supported tooling.
4. Validate NCCL/RoCE communication independently of any model runtime.
5. Serve `deepseek-ai/DeepSeek-V4-Flash-0731` across both nodes with vLLM tensor parallelism.
6. Support explicit switching between DeepSeek, TRELLIS.2, maintenance, and future model profiles.
7. Provide a stable OpenAI-compatible API and a browser interface.
8. Add Tailscale access only after the LAN deployment is stable.

## Non-goals

- Kubernetes, Slurm, or Docker Swarm during the initial deployment.
- Serving DeepSeek and TRELLIS.2 concurrently when their combined memory demand is unsafe.
- Loading model weights from the NAS during inference.
- Public internet exposure or router port forwarding.
- Automatic operating-system, firmware, container, or model updates.
- Running unreviewed remote installation scripts directly from a pipe to a shell.

## Architecture

### Administration plane

The Mac is the trusted administration workstation. Its 1Password-managed private key never leaves 1Password. Only the public key is installed in `carst`'s `authorized_keys` on each Spark. SSH host aliases provide stable names for the two LAN addresses and select only the dedicated DGX key.

Cluster jobs require separate node-to-node SSH credentials. These credentials are generated on the Sparks, are not reused for Mac administration, and are restricted to the private cluster fabric where the supported tooling permits. SSH agent forwarding is not used.

### Compute plane

Spark 1 is the head node and Spark 2 is the worker. DeepSeek runs as one logical vLLM service with tensor parallel size two. Inter-node model traffic uses the direct ConnectX-7 fabric; client traffic uses the normal LAN address of Spark 1.

The fabric has no default route and is not used for internet or client access. NVIDIA Sync Cluster Assistant is the preferred configuration path because it validates topology, software readiness, fabric addressing, and node-to-node SSH. The official manual two-Spark playbook is the fallback if Cluster Assistant cannot complete the configuration. The resulting interface names, addresses, GID index, and RDMA device names are recorded in a checked-in, non-secret inventory file.

### Storage plane

Each Spark keeps its own complete, verified local model cache. The same pinned model revision and container image are present on both nodes before a distributed service starts. Serving switches to Hugging Face offline mode after both caches have been verified.

The NAS may store configuration backups, benchmark results, and optional download archives. It is not mounted into the model's runtime path because NAS latency and network bandwidth would make it a performance and availability dependency.

### Access plane

During initial validation, clients connect directly to the authenticated vLLM endpoint on Spark 1. A browser UI is added only after the API passes functional and load smoke tests.

LiteLLM is optional and deferred. If the DS218+ has adequate memory and a supported container runtime, it may later host a lightweight LiteLLM gateway and browser UI. Otherwise, the UI runs as a resource-limited container on Spark 1 and LiteLLM is omitted until routing multiple active endpoints provides real value.

Tailscale is added after LAN acceptance. Remote users connect to a named Tailscale Service protected by grants or ACLs. No Spark API port is exposed directly to the public internet.

## Runtime Profiles

### `deepseek`

- Reserves both Sparks.
- Uses the MiaAI-Lab dual-Spark approach as the reference implementation.
- Runs `deepseek-ai/DeepSeek-V4-Flash-0731` with vLLM TP=2.
- Enables DSpark speculative decoding and the validated NVFP4 DS-MLA KV-cache path.
- Uses the model's calibrated maximum context of 1,048,576 tokens.
- Starts the worker first, then the head.
- Exposes the API only from the head node.

The reference repository, runtime sources, container image, model revision, encoder, and patches are reviewed before deployment. The local configuration pins:

- the source repository commit;
- the model snapshot revision;
- the container image by immutable digest;
- every locally applied patch by checksum.

The prebuilt Anemll image is acceptable only after provenance and contents are inspected. If that review is unsatisfactory, the same pinned runtime is built locally from reviewed sources.

### `trellis2`

- Requires DeepSeek to be fully stopped first.
- Runs in its own container or environment on one selected Spark.
- Uses local checkpoints and output storage.
- Starts with 512-cubed generation for acceptance testing before higher resolutions.
- Exposes its web application on the LAN only, with host binding and firewall rules recorded in configuration.

TRELLIS.2 is initially assigned to Spark 2, leaving Spark 1 available for control-plane services and diagnostics. This assignment can change without changing the external interface.

### `maintenance`

- Stops all GPU model containers on both nodes.
- Leaves SSH, monitoring, and the DGX Dashboard available.
- Is the required state before OS, firmware, driver, or fabric maintenance.

### Future profiles

Each future model is added as an isolated profile with declared nodes, ports, local cache paths, startup order, health checks, stop behavior, and acceptance tests. A profile is not advertised to clients until its health checks pass.

## Workload Controller

There is no NVIDIA-standard model-profile switcher for DGX Spark. The platform therefore uses a thin, project-local shell wrapper over ordinary Docker Compose and SSH. The wrapper is not a daemon and does not hide the underlying commands.

The controller provides these operations:

- `switch <profile>`
- `start <profile>`
- `stop`
- `status`
- `logs <profile>`
- `doctor`

A profile switch performs the following sequence:

1. Acquire a host-level lock so two switches cannot run concurrently.
2. Mark the current endpoint as draining and reject new work.
3. Wait for active requests up to a bounded grace period.
4. Stop the head service before its worker services.
5. Confirm all profile containers have exited on both nodes.
6. Confirm sufficient unified memory and disk capacity for the target profile.
7. Validate required local images, model snapshots, fabric connectivity, and configuration.
8. Start target workers before the target head.
9. Wait for container and application health checks.
10. Run a functional inference smoke test.
11. Advertise the target model to the gateway or browser UI.

If startup or validation fails, the controller stops the partial target deployment and leaves the system in a known stopped state. It does not automatically restart the previous heavyweight workload because that could repeat the failure or consume memory needed for diagnosis.

Compose definitions use explicit project names, health checks, bounded log rotation, suitable stop grace periods, and `restart: unless-stopped` only where it cannot conflict with intentional profile shutdown. Production overrides contain runtime settings without duplicating the base definitions.

## Security

- Keep password SSH login enabled until key access is verified in fresh sessions on both nodes.
- Do not copy the Mac's private key to either Spark.
- Do not use SSH agent forwarding.
- Keep secrets out of Git, Compose files, logs, and command histories.
- Store API keys and future Tailscale or LiteLLM credentials in 1Password and inject them at runtime.
- Require an API key on vLLM even on the LAN.
- Bind public-facing application ports only to the intended LAN or Tailscale interface.
- Do not expose cluster-fabric addresses to clients.
- Run containers without root and with read-only mounts where the GPU/RDMA runtime permits; grant only the devices and capabilities required by the workload.
- Verify signed images when publishers provide signatures. Pin all images by digest rather than using `latest`.
- If LiteLLM is deployed, use a signed stable release at version `1.83.7` or later, configure a master key, and keep its management UI off the public internet.

## Updates and Change Control

Both Sparks must run matching supported DGX OS, driver, CUDA, firmware, and container-runtime versions. Initial updates use DGX Dashboard, which NVIDIA recommends over ad-hoc package upgrades.

Updates occur only in the `maintenance` profile and follow this process:

1. Back up configuration and record current versions.
2. Review release notes and known issues.
3. Update both nodes in the same maintenance window.
4. Reboot and compare versions.
5. Re-run fabric, NCCL, container, and model smoke tests.
6. Roll forward configuration only after both nodes pass.

Model, runtime, and image changes use new pins and repeat the same acceptance tests. Automatic floating updates are disabled.

## Observability and Operations

The `status` and `doctor` operations report:

- active profile and transition state;
- container state and health on both nodes;
- LAN and fabric connectivity;
- disk free space and model-cache verification;
- CPU and unified-memory pressure using DGX-supported reporting;
- vLLM model identity, maximum context, and API health;
- recent bounded logs;
- last successful smoke test and benchmark versions.

Metrics and logs remain local during initial setup. Prometheus or centralized logging is added only if normal operation demonstrates a need.

## Failure Handling

- A failed profile switch ends in the stopped state with diagnostics preserved.
- Loss of the worker makes the distributed DeepSeek endpoint unhealthy; the head must not continue advertising it as usable.
- A failed model-cache verification prevents startup and triggers a targeted re-download of the affected snapshot.
- A failed fabric or NCCL check prevents DeepSeek startup but does not block single-node maintenance or TRELLIS.2 diagnostics.
- Unexpected memory pressure stops new admissions and preserves logs; the controller does not use destructive cache-clearing as an automatic response.
- Recovery instructions are documented for removing profile containers and configuration without deleting model caches or user data.

## Validation Sequence

1. Verify key-based Mac-to-node SSH in fresh sessions.
2. Inventory and align software versions on both nodes.
3. Configure the ConnectX-7 fabric with NVIDIA Sync Cluster Assistant.
4. Verify fabric IP connectivity in both directions.
5. Validate raw RDMA and NCCL bandwidth with NVIDIA's tests.
6. Validate Docker GPU access and image architecture on both nodes.
7. Audit and pin the MiaAI-Lab runtime artifacts.
8. Download and verify identical model snapshots on both nodes.
9. Start DeepSeek worker-first and validate `/v1/models`.
10. Test non-thinking, high, and max reasoning requests through the OpenAI-compatible API.
11. Test tool calls, streaming, context growth, restart behavior, and concurrent requests.
12. Record baseline prefill, decode, and aggregate throughput.
13. Stop DeepSeek and verify the system returns to a clean profile state.
14. Install and validate TRELLIS.2 at 512-cubed resolution.
15. Switch repeatedly between DeepSeek and TRELLIS.2 and confirm deterministic recovery.
16. Add the browser UI, then optional LiteLLM and Tailscale in separate acceptance steps.

## Acceptance Criteria

- The Mac reaches both Sparks by SSH without a Linux password and without a private key on disk.
- Both nodes run matching supported platform software.
- The direct fabric passes NVIDIA's connectivity, RDMA, and NCCL validation.
- DeepSeek-V4-Flash-0731 serves through one authenticated OpenAI-compatible endpoint using both Sparks.
- The endpoint reports the pinned model identity and supports the intended 1M maximum context configuration.
- Functional, streaming, reasoning-effort, and tool-call smoke tests pass.
- TRELLIS.2 produces a valid GLB from a sample image.
- Profile switches are serialized, health-checked, repeatable, and recover to a known stopped state on failure.
- The browser UI displays only the currently healthy model profile.
- No model service is publicly exposed, and later remote access is restricted by Tailscale policy.
- All runtime configuration is reproducible from this repository without committed secrets.

## References

- [NVIDIA DGX Spark user guide](https://docs.nvidia.com/dgx/dgx-spark/)
- [NVIDIA ConnectX-7 Cluster Assistant](https://docs.nvidia.com/sync/latest/cluster-assistant.html)
- [NVIDIA two-Spark networking guide](https://build.nvidia.com/spark/connect-two-sparks/stacked-sparks)
- [NVIDIA DGX Spark update guide](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)
- [DeepSeek-V4-Flash-0731 model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731)
- [MiaAI-Lab dual-Spark 0731 recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
- [Microsoft TRELLIS.2](https://github.com/microsoft/TRELLIS.2)
- [Docker Compose production guidance](https://docs.docker.com/compose/how-tos/production/)
- [Tailscale Services](https://tailscale.com/kb/1552/tailscale-services)
- [LiteLLM repository and signed releases](https://github.com/BerriAI/litellm)
