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
without renumbering the existing 37. The implementation and local acceptance
work for all 57 tasks is now landed on `main`; the remaining release status is
limited to the physical/protected-host evidence listed below. Planning and
design acceptance are not counted as implementation completion.

## Implementation status (2026-08-06)

| Lane | Tasks | Current status | Evidence |
| --- | --- | --- | --- |
| Existing platform lane | Tasks 1–31 | Complete in the repository | Platform update, NAS generation, agent A/B, rollout, CLI/web, and version-skew suites |
| Workload contracts and trust | W1–W4 | Complete | Canonical locks, Git/TUF separation, promotion, artifact provenance, and supply-chain verification |
| Spark package engine | W5–W8, W10 | Complete | Agent/protocol suites, resumable acquisition, immutable environments, generic adapter ABI, generations, rollback, and GC |
| Backend execution capability | W9 | ABI and native backend complete; OCI/python-venv fail closed | Signed deployment policy propagation, ABI/capability/compatibility preflight, and explicit non-native rejection; OCI rootfs/runtime and Python interpreter helpers remain a separate privileged platform capability |
| Workload control plane | W11–W16 | Complete | Alembic `0013_workload_packages`, discovery/resolution, validation, reconciliation, API/CLI/web, metrics |
| Migration and acceptance | W17–W20 | Complete | Mia/DS4-compatible generic projection, unknown-family E2E, failure/scale/security matrix, operator runbooks |
| Final hardening | Tasks 32–37 | Implemented locally; release-gated | Simulated evidence passes; physical/protected-host evidence remains external |

The reproducible workload acceptance report records zero SSH calls and zero
`agent.update` calls. A new workload family, runtime, image, environment,
checkpoint, or adapter release is therefore independent of the DGX-Forge
platform release unless it requires a genuinely new privileged capability or
ABI.

W9 deliberately does not treat an enum value as an installed runtime. The
current Spark helper executes the reviewed native backend and rejects OCI or
python-venv requests before opening content or invoking a process. Adding an
OCI rootfs/runtime boundary or an immutable Python interpreter boundary is a
future DGX-Forge platform capability; once that capability is installed, new
workload releases selecting it remain NAS-admin-driven and do not require a
model-specific agent release.

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
set must be explicit: native workloads pass; OCI/python-venv workloads are
rejected unless their reviewed privileged runtime capability and evidence are
installed on the Spark.

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
