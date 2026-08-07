# Vonk Install Admission and Cluster UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an administrator select a local recipe revision, prove whether it fits one or more Sparks, install it with exact disk accounting, run it with live memory/topology checks, and understand the cluster from one maintenance UI.

**Architecture:** The controller computes immutable installation and run plans from recipe requirements plus fresh agent inventory. Installation reserves disk and materializes image/weight digests; running separately reserves memory and ports. Multi-node plans use one atomic operation group and do not publish a LiteLLM route until every assigned rank returns matching fresh identity/readiness evidence.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, Alembic, React, TypeScript, TanStack Query, Vitest, Playwright, LiteLLM, Rust agent operation protocol.

---

## Task 1: Persist inventory snapshots, artifact presence, reservations, and runs

**Files:**
- Create: `control/migrations/versions/0017_admission_and_run_state.py`
- Modify: `control/src/dgx_control/models.py`
- Create: `control/src/dgx_control/admission_models.py`
- Create: `control/tests/test_admission_migration.py`
- Create: `control/tests/test_inventory_repository.py`

- [ ] Write the migration test first. Assert upgrade/downgrade creates typed tables for `node_inventory_snapshots`, `node_artifacts`, `resource_reservations`, `recipe_installations`, `installation_nodes`, `recipe_runs`, and `run_nodes`, including unique active-reservation and digest constraints.
- [ ] Run `uv run --project control pytest control/tests/test_admission_migration.py -q`; confirm failure because migration `0017` is missing.
- [ ] Add SQLAlchemy models with decimal byte quantities, explicit inventory timestamps, reservation states, node/rank identity, artifact digest/source/size/presence state, and immutable plan JSON/hash.
- [ ] Use exclusion/partial unique indexes so the same node/port or active run allocation cannot be double-booked, and foreign keys prevent deleting referenced recipe revisions or nodes.
- [ ] Add repository tests for snapshot replacement, stale evidence, artifact deduplication, concurrent reservation conflicts, and terminated-run release.
- [ ] Run the migration and repository tests on PostgreSQL.
- [ ] Commit: `feat(admission): persist inventory and resource reservations`

## Task 2: Calculate explainable install admission

**Files:**
- Create: `control/src/dgx_control/install_admission.py`
- Create: `control/src/dgx_control/artifact_sizes.py`
- Create: `control/tests/test_install_admission.py`
- Create: `control/tests/fixtures/admission/single-node.json`
- Create: `control/tests/fixtures/admission/multi-node.json`

- [ ] Write failing tests for exact-fit disk, safety margin, existing shared image layers, existing weights, unknown registry size, stale inventory, read-only store, partial multi-node fit, and concurrent install reservations.
- [ ] Run `uv run --project control pytest control/tests/test_install_admission.py -q`; confirm failure at missing imports.
- [ ] Implement `plan_install(recipe_revision_id, node_ids)` to resolve image manifest/layer sizes and model artifact sizes without downloading bytes, subtract already verified digests per node, add configurable temporary unpack overhead and disk floor, then return `allowed`, blockers, warnings, per-node bytes, and evidence timestamps.
- [ ] Make unknown required sizes blocking until metadata is resolved or an administrator explicitly supplies a persisted verified size override. Never silently assume zero.
- [ ] Persist the canonical plan and SHA-256 before enqueueing work; recheck free disk and the plan hash at execution time.
- [ ] Run tests and property-test non-negative, monotonic accounting across randomized shared-layer layouts.
- [ ] Commit: `feat(admission): add disk-aware install planning`

## Task 3: Calculate memory, port, runtime, and topology admission

**Files:**
- Create: `control/src/dgx_control/run_admission.py`
- Create: `control/src/dgx_control/topology.py`
- Create: `control/tests/test_run_admission.py`
- Create: `control/tests/test_topology.py`
- Modify: `control/src/dgx_control/package_compatibility.py`

- [ ] Write failing tests for recipe memory floor, observed model allocation, configured safety margin, other active reservations, stale inventory, missing runtime/plugin capability, occupied port, mixed Spark hardware, rank count, tensor/pipeline parallel divisibility, worker fabric reachability, and entrypoint selection.
- [ ] Run `uv run --project control pytest control/tests/test_run_admission.py control/tests/test_topology.py -q`; confirm failure.
- [ ] Implement `plan_run(installation_id, placement)` using each node's fresh available RAM/GPU memory minus active reservations and safety floor. Return raw inputs, calculations, blockers, warnings, and one reservation proposal per node.
- [ ] Implement topology validation that produces a deterministic rank map, requires a single compatible recipe/runtime/image/weight identity, validates declared multi-node strategy, and designates exactly one LiteLLM entrypoint.
- [ ] Require capabilities reported by agents to match the recipe compiler output; DS4 or future custom runtimes remain blocked unless both recipe and agent advertise the same versioned capability.
- [ ] Acquire reservations transactionally before creating operations; a database conflict returns a new plan instead of overcommitting.
- [ ] Run all admission/topology tests on PostgreSQL.
- [ ] Commit: `feat(admission): add memory and topology run planning`

## Task 4: Orchestrate install, start, stop, and uninstall lifecycles

**Files:**
- Create: `control/src/dgx_control/recipe_operations.py`
- Create: `control/src/dgx_control/recipe_operation_worker.py`
- Create: `control/src/dgx_control/recipe_api.py`
- Create: `control/tests/test_recipe_operations.py`
- Create: `control/tests/test_recipe_api.py`
- Modify: `control/src/dgx_control/api.py`
- Modify: `control/src/dgx_control/agent_jobs.py`

- [ ] Write failing state-machine tests for plan expiration, install success, partial multi-node install failure, retry, cancellation, start group abort, start readiness, stop, force-stop, uninstall blocked by active run, and idempotent request keys.
- [ ] Run the two tests; confirm missing routes/service failures.
- [ ] Add endpoints to preview and execute install/run plans, inspect operation progress, stop a run, and uninstall node artifacts. Separate preview from mutation and require the submitted plan hash.
- [ ] Compile operations only from the accepted immutable recipe revision and frozen placement. Queue one fenced operation group per lifecycle action and treat partial multi-node start as failed, withdrawing/never publishing the route and stopping successful ranks.
- [ ] Release run reservations only after stop evidence or an explicit operator-confirmed lost-node recovery path. Retain installation/artifact records until verified garbage collection.
- [ ] Add audit records with actor, recipe revision, plan hash, node/rank set, operation group, result, and override reason.
- [ ] Run API/state-machine/security authorization tests.
- [ ] Commit: `feat(recipes): orchestrate install and run lifecycle`

## Task 5: Publish and withdraw LiteLLM routes atomically

**Files:**
- Create: `control/src/dgx_control/recipe_routes.py`
- Create: `control/tests/test_recipe_routes.py`
- Modify: `control/src/dgx_control/litellm.py`
- Modify: `control/src/dgx_control/route_runtime.py`
- Modify: `control/src/dgx_control/settings.py`
- Modify: `deploy/compose/caddy/Caddyfile`

- [ ] Write failing tests proving no route before all ranks are ready, stale/mismatched evidence blocks publication, only rank zero/entrypoint appears as upstream, a changed generation withdraws the old route, LiteLLM rejects an invalid candidate without losing the active config, and stop withdraws before workload termination.
- [ ] Run `uv run --project control pytest control/tests/test_recipe_routes.py control/tests/test_litellm.py -q`; confirm expected failures.
- [ ] Render the complete LiteLLM candidate config from active recipe runs, validate it through LiteLLM, atomically replace the active file/database representation, reload, and verify health before marking the route published.
- [ ] Keep Caddy static: tailnet authentication/security headers and `/v1/*` reverse proxy to LiteLLM only. Do not generate per-model Caddy routes.
- [ ] Make LiteLLM call the validated Spark entrypoint directly on the restricted management LAN; never add worker ranks as independent upstreams.
- [ ] Run route tests including concurrent start/stop and LiteLLM-unavailable recovery.
- [ ] Commit: `feat(routing): publish validated recipe routes atomically`

## Task 6: Add typed frontend API models and recipe workflow pages

**Files:**
- Modify: `control/openapi.json`
- Modify: `control/web/src/api/generated.d.ts`
- Modify: `control/web/src/api/types.ts`
- Modify: `control/web/src/api/client.ts`
- Create: `control/web/src/pages/recipes.tsx`
- Create: `control/web/src/pages/recipe-detail.tsx`
- Create: `control/web/src/pages/recipe-install.tsx`
- Create: `control/web/src/pages/recipe-run.tsx`
- Create: `control/web/src/pages/recipes.test.tsx`
- Modify: `control/web/src/app.tsx`

- [ ] Write React tests first for local recipe creation/import status, revision metadata, install placement, disk breakdown, run placement, memory breakdown, topology blockers, operation progress, and disabled confirmation when evidence is stale.
- [ ] Run `npm test -- --run src/pages/recipes.test.tsx` from `control/web`; confirm failure because pages/routes are absent.
- [ ] Regenerate OpenAPI types and add client methods for recipes, import reports, plan preview, lifecycle execution, and operation streaming/polling.
- [ ] Build recipe list/detail/install/run views. Always show the immutable revision, image digest, weight identifiers, publisher/source, trust state, required runtime/plugin capabilities, and exact blocker/warning explanations.
- [ ] Require a fresh preview after placement or cluster state changes. Overrides must be limited to explicitly overridable warnings and require a reason; disk/RAM/topology hard blockers stay non-overridable.
- [ ] Run component tests and accessibility checks for keyboard flow, labels, focus on validation errors, and non-color-only status indicators.
- [ ] Commit: `feat(web): add recipe install and run workflow`

## Task 7: Build the Spark cluster maintenance view

**Files:**
- Create: `control/src/dgx_control/cluster_view.py`
- Create: `control/tests/test_cluster_view.py`
- Create: `control/web/src/pages/cluster.tsx`
- Create: `control/web/src/pages/cluster.test.tsx`
- Create: `control/web/src/components/node-capacity.tsx`
- Create: `control/web/src/components/model-storage.tsx`
- Create: `control/web/src/components/running-models.tsx`
- Modify: `control/web/src/app.tsx`

- [ ] Write backend and React tests first for one row/card per Spark, online/stale/offline state, hardware capacity, free/reserved/observed memory, disk capacity, installed model artifacts with sizes/digests/refcounts, running models with RAM reservations and observed use, multi-node grouping, rank/entrypoint labels, and orphaned artifacts/processes.
- [ ] Run the scoped tests; confirm missing endpoint/components.
- [ ] Add one aggregated read endpoint that snapshots cluster state consistently and exposes timestamps plus data provenance rather than making the browser join operational tables.
- [ ] Implement responsive cluster cards and a compact table mode. Visually connect the nodes participating in the same multi-node run and show total requested versus available capacity.
- [ ] Add actions that deep-link to the recipe/run detail; do not embed destructive one-click operations on the overview.
- [ ] Add empty, loading, partial failure, stale, and offline states. A lost node must retain last-known model/disk information with its timestamp.
- [ ] Run Vitest, Playwright at desktop/mobile widths, and screenshot assertions for single- and multi-node fixtures.
- [ ] Commit: `feat(web): add Spark cluster maintenance view`

## Task 8: Prove admission and recovery on real topology

**Files:**
- Create: `tests/e2e/test_recipe_install_run.py`
- Create: `tests/e2e/test_multinode_admission.py`
- Create: `docs/runbooks/recipe-lifecycle.md`
- Create: `docs/runbooks/resource-recovery.md`
- Modify: `control/web/e2e/admin.spec.ts`

- [ ] Write E2E scenarios first for insufficient disk, insufficient memory because another model is running, successful single-node install/run, two-node topology mismatch, successful two-node start, one-rank readiness failure, stop and capacity recovery, controller restart, and node loss.
- [ ] Run them against simulators; confirm unimplemented scenarios fail.
- [ ] Implement simulator inventory mutation and deterministic failure injection needed by the scenarios, without test-only branches in admission logic.
- [ ] Run the same critical scenarios on physical Sparks. Capture before/after inventory, operation groups, reservations, container identities, route config, and an inference request through Tailscale/Caddy/LiteLLM.
- [ ] Document normal lifecycle and the explicit lost-node recovery procedure, including what evidence is unavailable and why capacity is not silently released.
- [ ] Commit: `test(admission): prove recipe lifecycle and recovery`

## Verification

```bash
uv run --project control alembic -c control/alembic.ini upgrade head
uv run --project control pytest control/tests -q
npm --prefix control/web test -- --run
npm --prefix control/web run build
npm --prefix control/web run test:e2e
uv run pytest tests/e2e/test_recipe_install_run.py tests/e2e/test_multinode_admission.py -q
git diff --check
```

Completion requires recorded physical evidence for disk admission, memory contention, multi-node identity/topology, atomic route publication, stop-before-withdraw ordering, and capacity recovery.
