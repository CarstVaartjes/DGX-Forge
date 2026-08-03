# Dual DGX Spark Platform Implementation Roadmap

The approved design is implemented in phases. Execute them in order because
each later phase consumes verified artifacts from the preceding phase.

1. **Complete:** [Secure host bootstrap and fabric](2026-08-01-secure-host-bootstrap-and-fabric.md) installed the 1Password public key, repaired cloned identities, inventoried and hardened both Sparks, validated their matched platform, checked `earlyoom`, configured the direct one-link/two-function CX-7 fabric, and passed aggregate RDMA, latency, error-counter, and NCCL gates. The as-built result is in the [installation record](../../installation-record.md).
2. **Complete:** [Model profile framework](2026-08-02-model-profile-framework.md) built the developer-machine catalog, adapters, placement rules, fail-to-stopped switching, live node health, and `sparkctl` interface used by every model.
3. **Complete for Mia; DS4 remains verified:** The pinned [Mia DeepSeek 0731 dual-Spark runtime](2026-08-02-mia-deepseek-dual-runtime.md) passed canonical performance, 15-minute thermal, three-cycle lifecycle, reboot/no-autostart, quality, and release gates and is recorded at `accepted` maturity. The audited [DS4 DeepSeek 0731 single-Spark runtime](2026-08-03-ds4-deepseek-single-runtime.md) remains `verified`: its pinned Q2-imatrix lane measured below the final single-decode throughput floor and is not advertised as accepted.
4. **Cataloged; qualification in progress:** The remaining Model Definitions and initial Cluster Profile intents from the [approved multi-runtime design](../specs/2026-08-02-multi-runtime-model-profiles-design.md) are present for Nemotron, Qwen image and vision, Pixal3D, TRELLIS.2, rigging, Step1X-3D, TripoSG, and Hunyuan3D-Omni. TripoSG is now `verified` on Spark 2 with isolated runtime, API, output-quality, and lifecycle evidence; the other candidates remain `planned` behind fail-closed adapters until their Spark-native images, artifacts, quality, resource, lifecycle, and exact N-way co-residency evidence are produced. No unqualified creative endpoint is activatable.
5. **In parallel, without changing active runtime work:** implement the approved [scalable Spark platform and control-plane design](../specs/2026-08-03-scalable-spark-platform-control-plane-design.md). It adds generic per-Spark onboarding, N-node contracts, and a portable Docker Compose control plane with a shared CLI/web API, Caddy, LiteLLM, PostgreSQL, and observability. Its phased plans must consume—rather than reimplement—the SSH transport and runtime-release interfaces produced by phases 2–4.
6. **Release gate:** complete control-plane security, backup/restore, service-host recovery, and end-to-end acceptance. At the first real release, enable protected-branch and PR-only repository mutation. The earlier [secondary services plan](2026-08-01-secondary-ai-and-access-services.md) remains historical input, not the current service or model plan.

The model-runtime execution scope remains phases 2–4. Generic onboarding may
proceed independently where its file ownership is disjoint; service-host phases
begin after the Docker-capable host is available. Every phase leaves the system
in a usable, testable state and ends with a commit. Distributed AI profiles
never auto-start after reboot, and no non-AI container is deployed to any
Spark.
