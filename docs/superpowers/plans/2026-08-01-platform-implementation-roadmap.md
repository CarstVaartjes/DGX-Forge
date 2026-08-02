# Dual DGX Spark Platform Implementation Roadmap

The approved design is implemented in phases. Execute them in order because
each later phase consumes verified artifacts from the preceding phase.

1. **Complete:** [Secure host bootstrap and fabric](2026-08-01-secure-host-bootstrap-and-fabric.md) installed the 1Password public key, repaired cloned identities, inventoried and hardened both Sparks, validated their matched platform, checked `earlyoom`, configured the direct one-link/two-function CX-7 fabric, and passed aggregate RDMA, latency, error-counter, and NCCL gates. The as-built result is in the [installation record](../../installation-record.md).
2. **Next:** [Model profile framework](2026-08-02-model-profile-framework.md) builds the developer-machine catalog, adapters, placement rules, fail-to-stopped switching, and `sparkctl` interface used by every model.
3. **Then:** Reconcile and execute the [DeepSeek 0731 runtime](2026-08-01-deepseek-0731-runtime.md) through that framework, with the Mia dual-Spark service as the default agent and DS4 as the lighter alternative.
4. **Then:** Implement the remaining Model Definitions and adapters from the [approved multi-runtime design](../specs/2026-08-02-multi-runtime-model-profiles-design.md): Nemotron, Qwen image and vision, Pixal3D, TRELLIS.2, rigging, Step1X-3D, TripoSG, and Hunyuan3D-Omni. Every user-facing Cluster Profile uses the best accepted Spark-optimized definition for each model, and co-residency requires acceptance of its exact N-way definition set.
5. **After the new external host arrives:** [External control plane](2026-08-01-external-control-plane.md) adds the restricted node command, Caddy gateway, durable state/lock handling, and fail-to-stopped switching around the validated profiles.
6. **Last:** Add the external browser UI, apply the LiteLLM deployment gate, configure Tailscale ingress, and run shared-platform acceptance. The earlier [secondary services plan](2026-08-01-secondary-ai-and-access-services.md) is historical input, not the current model catalog.

The immediate execution scope covers phases 2–4. Phases 5–6 have an explicit
future-host prerequisite. Every phase leaves the system in a usable, testable
state and ends with a commit. Distributed AI profiles never auto-start after
reboot, and no non-AI container is deployed to either Spark.
