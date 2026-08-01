# Dual DGX Spark Platform Implementation Roadmap

The approved design is implemented as four plans. Execute them in order because each later plan consumes verified artifacts from the preceding plan.

1. [Secure host bootstrap and fabric](2026-08-01-secure-host-bootstrap-and-fabric.md) — install the 1Password public key, inventory and harden both Sparks, update them sequentially, disable `earlyoom`, configure the direct CX-7 link, and pass RDMA/NCCL gates.
2. [DeepSeek 0731 runtime](2026-08-01-deepseek-0731-runtime.md) — pin and audit the Mia/Anemll stack, verify model caches, execute the staged profile ladder through a loopback endpoint, and pass correctness, capacity, and performance gates without waiting for Caddy.
3. [Secondary AI and access services](2026-08-01-secondary-ai-and-access-services.md), Tasks 1–2 — add and validate TRELLIS.2 locally through an SSH tunnel.
4. After the new NAS arrives, [external control plane](2026-08-01-external-control-plane.md) — build the one-shot controller, restricted node command, Caddy gateway, state/lock handling, and fail-to-stopped switching around the validated AI profiles.
5. [Secondary AI and access services](2026-08-01-secondary-ai-and-access-services.md), Tasks 3–6 — add the external browser UI, apply the LiteLLM deployment gate, configure Tailscale ingress, and run full shared-platform acceptance.

The immediate execution scope ends after item 3. Items 4–5 have an explicit future-host prerequisite. Every plan leaves the system in a usable, testable state and ends with a commit. Distributed AI profiles never auto-start after reboot, and no non-AI container is deployed to either Spark.
