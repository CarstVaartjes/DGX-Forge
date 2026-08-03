# Dual DGX Spark Platform Implementation Roadmap

The approved design is implemented in phases. Execute them in order because
each later phase consumes verified artifacts from the preceding phase.

1. **Complete:** [Secure host bootstrap and fabric](2026-08-01-secure-host-bootstrap-and-fabric.md) installed the 1Password public key, repaired cloned identities, inventoried and hardened both Sparks, validated their matched platform, checked `earlyoom`, configured the direct one-link/two-function CX-7 fabric, and passed aggregate RDMA, latency, error-counter, and NCCL gates. The as-built result is in the [installation record](../../installation-record.md).
2. **Complete:** [Model profile framework](2026-08-02-model-profile-framework.md) built the developer-machine catalog, adapters, placement rules, fail-to-stopped switching, live node health, and `sparkctl` interface used by every model.
3. **In progress:** The pinned [Mia DeepSeek 0731 dual-Spark runtime](2026-08-02-mia-deepseek-dual-runtime.md) is installed, operational, quality-verified, and recorded at `verified` maturity. Final performance, thermal, lifecycle, and reboot acceptance is deferred to the cross-model optimization phase. The next executable milestone is the audited [DS4 DeepSeek 0731 single-Spark runtime](2026-08-03-ds4-deepseek-single-runtime.md), which preserves the same client-facing `deepseek` model name while freeing Spark 2 for later creative workloads.
4. **Then:** Implement the remaining Model Definitions and adapters from the [approved multi-runtime design](../specs/2026-08-02-multi-runtime-model-profiles-design.md): Nemotron, Qwen image and vision, Pixal3D, TRELLIS.2, rigging, Step1X-3D, TripoSG, and Hunyuan3D-Omni. Every user-facing Cluster Profile uses the best accepted Spark-optimized definition for each model, and co-residency requires acceptance of its exact N-way definition set.
5. **In parallel, without changing active runtime work:** implement the approved [scalable Spark platform and control-plane design](../specs/2026-08-03-scalable-spark-platform-control-plane-design.md). It adds generic per-Spark onboarding, N-node contracts, and a portable Docker Compose control plane with a shared CLI/web API, Caddy, LiteLLM, PostgreSQL, and observability. Its phased plans must consume—rather than reimplement—the SSH transport and runtime-release interfaces produced by phases 2–4.
6. **Release gate:** complete control-plane security, backup/restore, service-host recovery, and end-to-end acceptance. At the first real release, enable protected-branch and PR-only repository mutation. The earlier [secondary services plan](2026-08-01-secondary-ai-and-access-services.md) remains historical input, not the current service or model plan.

The model-runtime execution scope remains phases 2–4. Generic onboarding may
proceed independently where its file ownership is disjoint; service-host phases
begin after the Docker-capable host is available. Every phase leaves the system
in a usable, testable state and ends with a commit. Distributed AI profiles
never auto-start after reboot, and no non-AI container is deployed to any
Spark.
