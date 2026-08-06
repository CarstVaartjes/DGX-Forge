# Generalized Workload Package Roadmap Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the approved generalized workload package design into the committed roadmap without renumbering, duplicating, or colliding with existing Tasks 26–37.

**Architecture:** Tasks 26–31 remain the DGX-Forge platform-update lane and now include authenticated version-skew telemetry plus the NAS-newer update prompt. A separately numbered W1–W20 workload lane owns generic workload contracts, Spark package execution, NAS administration, migration, and acceptance. Tasks 32–37 remain final platform hardening but consume W20 evidence before the first real release.

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

The original roadmap therefore expands from 37 to 57 implementation tasks
without renumbering the existing 37. At the integration point, Tasks 1–25 are
complete and Tasks 26–37 plus W1–W20 remain: 25/57 complete (43.9%). Planning
and design acceptance do not count as implementation completion.

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
| Immutable Python environments and bounded source-wheel builds | W8 |
| OCI/Python/native backends and unprivileged adapter ABI | W9 |
| Prepare/activate/health/stop/rollback generations and reporting | W5, W10, W14, W16 |
| Credentials, license policy, repair, leases, and garbage collection | W7, W10, W13 |
| Failure taxonomy, recovery, cancellation, scale, and security | W5, W10, W12, W19 |
| Platform/workload release boundary and NAS-newer Spark update prompt | Tasks 26–31, W20 |
| Mia, DS4, and existing model/profile migration | W17 |
| Unknown-family decisive E2E and first-release acceptance | W18–W20, Tasks 32–37 |

## Integration gates

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
generic ABI with resumable progress and no SSH.

- [ ] **Gate 4: Complete W11–W16**

Require generic discovery/resolution, structured unsupported reasons,
validation/promotion/canary, digest-driven reconciliation, and equivalent API,
CLI, web, audit, and observability workflows.

- [ ] **Gate 5: Complete W17–W20**

Require Mia/DS4 projection equivalence, the post-build synthetic package test,
unsigned/unapproved rejection, release-2 activation and offline release-1
rollback without agent update, complete failure/scale/security gates, docs, and
hosted CI.

- [ ] **Gate 6: Resume Tasks 32–37 final hardening**

Run whole-repository threat, backup/restore, service-host loss, Spark recovery,
platform update, workload package, multi-node scale, operator documentation,
and first-release verification. Enable PR-only mode only when every required
physical gate is genuinely recorded.
