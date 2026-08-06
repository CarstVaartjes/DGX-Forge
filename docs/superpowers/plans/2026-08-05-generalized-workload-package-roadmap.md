# Generalized Workload Package Roadmap Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the approved generalized workload package design into the committed roadmap without renumbering, duplicating, or colliding with existing Tasks 26–37.

**Architecture:** Tasks 26–31 remain the DGX-Forge platform-update lane and now include authenticated version-skew telemetry plus the NAS-newer update prompt. A separately numbered W1–W20 workload lane owns generic workload contracts, Spark package execution, NAS administration, migration, and acceptance. W21 is a focused UX/resource presentation lane that consumes the W15/W16 API and does not create a second control plane. Tasks 32–37 remain final platform hardening but consume W20/W21 evidence before the first real release.

**Tech Stack:** The four implementation plans referenced below; Git worktrees; GitHub-hosted CI

## Global Constraints

- Existing roadmap task numbers and already accepted evidence do not move.
- No W task begins implementation in a file owned by an unmerged Task 26–31 branch.
- The GitHub container-release baseline landed in `e9f7695`. W tasks consume its
  GHCR metadata, immutable three-image Compose references, public-input checks,
  pull-only NAS deployment, UID/GID policy, and supply-chain evidence; they do
  not recreate or weaken those contracts.
- Platform release and workload release trust roots, manifests, operations, rollouts, and evidence remain independently typed.
- A W task may reuse an existing primitive only through its stable interface; it may not silently widen a legacy model-specific or privileged boundary.
- Every task uses an isolated worktree, TDD, an independent review gate, a scoped commit, push to `main` before the first real release, and hosted CI verification.
- At the first real release the repository switches to protected-branch PR-only mutation as already approved.

---

## Ordered task graph

```text
Task 25 complete -> landed GitHub container-release baseline (`e9f7695`)
  -> Tasks 26-31: DGX-Forge platform update and NAS/Spark version-skew UX
      -> W1-W4: contracts, Git authority, workload TUF, promotion
          -> W5-W10: Spark acquisition, store, environments, adapters, generations
              -> W11-W16: discovery, database, validation, reconciliation, API/CLI/web
                  -> W17-W20: Mia/DS4 migration, unknown-package E2E, hardening, docs
                      -> W21: Spark inventory, capacity UX, safe removal, visual polish
                          -> Tasks 32-37: whole-platform hardening and first release
```

W1–W4 are specified in
[`2026-08-05-generalized-workload-contracts-and-trust.md`](2026-08-05-generalized-workload-contracts-and-trust.md).
W5–W10 are specified in
[`2026-08-05-spark-workload-package-engine.md`](2026-08-05-spark-workload-package-engine.md).
W11–W16 are specified in
[`2026-08-05-workload-package-control-plane.md`](2026-08-05-workload-package-control-plane.md).
W17–W20 are specified in
[`2026-08-05-workload-package-migration-acceptance.md`](2026-08-05-workload-package-migration-acceptance.md).

W21 is specified below as a presentation and operator-experience follow-up;
its typed data comes from the W15 package API and W16 web administration work.

The original roadmap therefore expands from 37 to 58 implementation tasks
without renumbering the existing 37. The implementation and local acceptance
work for the original 57 task areas is landed on `main` for the installed capability
set; Python virtual-environment execution now has a signed interpreter and
environment-tree boundary, while OCI runc execution remains explicitly
release-gated rather than silently treated as native. W21's inventory,
capacity-envelope, safe-removal, and visual acceptance are landed locally; the
remaining release status is limited to
the physical/protected-host and capability evidence listed below. Planning and
design acceptance are not counted as implementation completion.

## Implementation status (2026-08-06)

| Lane | Tasks | Current status | Evidence |
| --- | --- | --- | --- |
| Existing platform lane | Tasks 1–31 | Complete in the repository | Platform update, NAS generation, agent A/B, rollout, CLI/web, and version-skew suites |
| Workload contracts and trust | W1–W4 | Complete | Canonical locks, Git/TUF separation, promotion, artifact provenance, and supply-chain verification |
| Spark package engine | W5–W8, W10 | Complete | Agent/protocol suites, resumable acquisition, immutable environments, generic adapter ABI, generations, rollback, and GC |
| Backend execution capability | W9 | ABI/native/python-venv complete; OCI runc capability release-gated | Signed deployment policy propagation, immutable Python interpreter/environment-tree verification, and an OCI bundle/rootfs planner with a fixed-runc capability boundary |
| Workload control plane | W11–W16 | Read/projection/UI and worker-owned removal/GC dispatch complete; release mutation remains trust-gated | Alembic `0014_package_action_plans`, Git/SQL projections, discovery/resolution, typed API/CLI/web, metrics, digest-bound removal/GC jobs; publication, validation, promotion, and rollout still require their signer/runner boundaries |
| Migration and acceptance | W17–W20 | Complete | Mia/DS4-compatible generic projection, unknown-family E2E, failure/scale/security matrix, operator runbooks |
| Operator experience | W21 | Implemented locally; acceptance evidence landed | Per-Spark inventory, resource-envelope/co-residency projections, digest-bound removal/GC dispatch, rollback identity, and polished responsive web UX; focused API/worker tests and web build are green |
| Final hardening | Tasks 32–37 | Implemented locally; release-gated | Simulated evidence passes; physical/protected-host evidence remains external |

The reproducible workload acceptance report records zero SSH calls and zero
`agent.update` calls. A new workload family, runtime, image, environment,
checkpoint, or adapter release is therefore independent of the DGX-Forge
platform release unless it requires a genuinely new privileged capability or
ABI.

W9 deliberately does not treat an enum value as an installed runtime. The
current Spark helper executes native workloads and Python virtual environments
through signed, generation-local interpreter metadata and an independently
checked environment tree. OCI bundles are materialized and verified, but the
fixed-runc capability remains disabled unless the root-owned runtime pin and
reviewed helper authority are installed. Once that capability is installed,
new workload releases selecting it remain NAS-admin-driven and do not require
a model-specific agent release.

## Ownership and overlap matrix

| Area | Existing owner | Workload owner | Sequencing rule |
| --- | --- | --- | --- |
| Platform release manifest, control-host generations, agent A/B slots | Tasks 26–31 | None | Workload locks cannot contain these artifacts. |
| GHCR API/worker/Hermes image build and pull-only NAS deployment | Landed container-release baseline (`e9f7695`) | W3/W5/W20 extend merged secrets/protocol/docs | Never add production `build:` or use the fixed three-image workflow for workload payloads. |
| Agent version/build/protocol/slot telemetry and NAS-newer prompt | Tasks 26–31 | W16 displays workload impact only | Implement once in platform update services and reuse its projection. |
| Shared immutable workload lock bytes | None | W1 | `dgx_agent_protocol` is the sole shared wire owner. |
| Git family/deployment authoring | Existing proposal/repository services | W2/W4 | Extend typed allowlists; do not create another Git writer. |
| TUF/OCI primitives | Existing platform/agent release code | W3/W7 | Reuse validation/transport code with separate roots, roles, prefixes, and state. |
| Agent operation queue/fencing | Existing reconciliation services | W5/W14 | Add generic operation enum/payloads; do not create a second agent transport. |
| Current `spark-runtime-v1` adapter path | Accepted legacy migration source | W9/W17 | Keep until generic equivalence passes; never expand its hard-coded catalog. |
| Model/profile repository APIs | Existing admin clients | W15/W17 | Preserve public projections; new writes use family/release/deployment documents. |
| PostgreSQL migrations | Tasks 29–31 use `0011_update_rollouts` then `0012_control_process_heartbeats` | W11 uses `0013_workload_packages` | One linear Alembic head; rebase the filename if intervening migrations land. |
| Metrics/Grafana | Existing bounded observability | W16 | Add bounded state/reason/backend/provider/phase labels only. |
| Spark inventory, capacity envelope, safe removal, and polished web presentation | W15/W16 typed projections | W21 | W21 consumes the existing API/CLI/job contracts; it must not add a second reconciler, trust root, or model catalog. |
| Final release evidence | Tasks 32–37 | W18–W20 | First-release verifier requires both independent evidence sets. |

## Release-plane decision table

| Change | Workload package flow | Platform update flow |
| --- | ---: | ---: |
| New family/model/Mia/DS4 version using ABI v1 | Yes | No |
| New adapter, image, environment, source, wheel, weight, tokenizer, or checkpoint | Yes | No |
| Family recipe change for an upstream layout | Yes | No |
| Deployment placement, routing, arguments, resources, or secret references | Yes | No |
| New discovery/fetch protocol, backend, adapter ABI, privileged helper operation, driver, or kernel need | No | Yes |
| New control image, agent, supervisor, protocol, node tooling, or trust root | No | Yes |

## Approved-spec coverage

| Design responsibility | Owning tasks |
| --- | --- |
| Authority/storage boundaries and immutable domain model | W1–W4, W11 |
| Package composition, descriptors, dependency bounds, canonical locks | W1, W2, W12 |
| External discovery, metadata-only resolution, unsupported reasons | W12 |
| Build/provenance handoff, manual/automatic promotion, workload TUF | W3, W4, W13 |
| Spark content store, reservations, resumable providers, deduplication | W6, W7 |
| Signed resource envelopes, KV/cache sizing, storage headroom, and co-residency admission | W1–W2, W6–W7, W13–W16 |
| Single/replicated/gang topology, rank/world-size/fabric fencing, and atomic multi-Spark lifecycle | W1–W2, W5, W14–W16 |
| Immutable Python environments and bounded source-wheel builds | W8 |
| OCI/Python/native backends and unprivileged adapter ABI | W9 |
| Prepare/activate/health/stop/rollback generations and reporting | W5, W10, W14, W16 |
| Credentials, license policy, repair, leases, and garbage collection | W7, W10, W13 |
| Capacity envelope, co-residency/distributed placement, per-Spark inventory, and safe removal | W6, W10, W14–W16, W21 |
| Failure taxonomy, recovery, cancellation, scale, and security | W5, W10, W12, W19 |
| Platform/workload release boundary and NAS-newer Spark update prompt | Tasks 26–31, W20 |
| Mia, DS4, and existing model/profile migration | W17 |
| Unknown-family decisive E2E and first-release acceptance | W18–W20, Tasks 32–37 |

## W21: Operator experience and capacity presentation follow-up

W15/W16 remain the owners of the typed package API and functional web
workflow. W21 consumes those contracts and adds no transport, reconciler,
trust root, or model-specific catalog. If a projection is missing, W15/W16
extend its existing resource rather than introducing a parallel API.

W21 must provide:

- a per-Spark inventory of downloading, staged, available, active, retained,
  leased, and removable releases/content, with free, reserved, installed, and
  reclaimable storage and resumable byte progress;
- a digest-bound removal preview and administrator apply flow that refuses
  active, leased, retained, or dependency-reachable objects, plus a matching
  GC preview and explanation of every guard;
- a capacity view that explains install/persistent/transient bytes, runtime
  host/GPU memory, KV-cache bounds, CPU/GPU/topology requirements, and the
  aggregate impact of co-resident and distributed deployments across one,
  two, or sixteen Sparks;
- a staged-download action that proves the active generation keeps serving,
  and explicit activation/stop/rollback actions with no implicit interruption;
  and
- a polished, responsive, accessible React experience: cluster overview
  cards, storage and memory charts, workload placement graph, live download
  timeline, capacity shortfall explanations, and safe confirmation dialogs.

W21 acceptance covers active-service continuity during a large download,
safe removal of inactive content, refusal to remove active/leased content,
GC reachability, co-resident resource fit, distributed placement, and
keyboard/mobile/error/loading states. Tailscale remains the optional secure
network ingress and Caddy remains the authenticated edge; neither is a UI
framework. LiteLLM and Grafana retain their linked native administration
surfaces.

## Integration gates

The per-step checkboxes in the linked implementation plans are retained as
historical execution checklists. The status table above is the current
implementation ledger. The gates below distinguish code completion from the
external evidence required before a real release.

- [ ] **Gate 1: Finish Tasks 26–31 and land through revision `0012_control_process_heartbeats`**

Require green hosted CI, the authenticated version-skew projection, persistent
NAS-newer prompt, exact confirmation-to-`agent.update` plan, compatible-old
operation, incompatible mutation blocking, offline pending behavior, canary,
rollback, and no workload artifacts in the platform manifest.

- [ ] **Gate 2: Complete W1–W4**

Require canonical unknown-family locks, Git-backed authoring, separate workload
TUF roots/roles/routes/state, verify-then-authorize promotion, and proof that a
workload key cannot update the platform.

- [ ] **Gate 3: Complete W5–W10**

Require an installed agent to fetch, materialize, validate, activate, health
check, roll back, repair, and garbage-collect an unknown package through the
generic ABI with resumable progress and no SSH. The first-release capability
set must be explicit: native and Python-venv workloads pass with their
installed capability tokens; OCI workloads are rejected unless the reviewed
privileged runc capability and evidence are installed on the Spark.

- [ ] **Gate 4: Complete W11–W16**

Require generic discovery/resolution, structured unsupported reasons,
validation/promotion/canary, digest-driven reconciliation, and equivalent API,
CLI, web, audit, and observability workflows.  Resource-envelope admission must
show resident/auxiliary/activation/workspace/KV peaks, installation storage
headroom, and measured-vs-declared evidence.  Topology must support independent
replicas and barriered multi-Spark gangs with certificate-bound roles/ranks,
world size, and fabric requirements.  Per-Spark inventory and explicit
deactivate/remove/GC previews must preserve running services, active/leased
generations, and shared dependencies.

- [ ] **Gate 5: Complete W17–W20**

Require Mia/DS4 projection equivalence, the post-build synthetic package test,
unsigned/unapproved rejection, release-2 activation and offline release-1
rollback without agent update, complete failure/scale/security gates, docs, and
hosted CI.

- [ ] **Gate 5a: Complete W21 operator experience**

Require staged/downloaded versus active inventory, per-Spark storage and
reclaim projections, digest-bound safe removal/GC guards, generic
install/runtime/GPU/KV/transient capacity explanations, co-resident and
distributed placement views, active-service continuity during download, and
accessible responsive web acceptance. W21 must consume the W15/W16 contracts
and must not introduce a second control plane or model catalog.

- [ ] **Gate 6: Resume Tasks 32–37 final hardening**

Run whole-repository threat, backup/restore, service-host loss, Spark recovery,
platform update, workload package, multi-node scale, operator documentation,
and first-release verification. Enable PR-only mode only when every required
physical gate is genuinely recorded.
