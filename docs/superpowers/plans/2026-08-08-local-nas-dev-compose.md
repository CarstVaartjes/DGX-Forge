# Local NAS Development Compose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (inline execution is approved for this task). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Run the Vonk Forge control API and worker locally on a Docker-capable NAS before any production release.

**Architecture:** Add a standalone `deploy/compose/compose.dev.yaml` that builds the existing API and worker Dockerfile targets from the checkout, starts PostgreSQL and migrations, and exposes the bundled control web/API on a loopback port. A small development initializer creates a valid local active-generation identity and writable runtime volumes; it never touches the production Compose file or release authority.

**Tech Stack:** Docker Compose, existing `control/Dockerfile`, PostgreSQL, Python host-state identity contracts, shell wrapper, pytest contract tests.

## Global Constraints

- Production `deploy/compose/compose.yaml` remains unchanged.
- Development mode uses local builds and never requires GHCR, TUF keys, mTLS CA material, Caddy, LiteLLM, or Cloudflare/Railway.
- The API binds to `127.0.0.1:8080` by default; operators may expose it through an explicit NAS access mechanism.
- Development secrets are generated outside Git under `.dev/vonk-forge-secrets` by the wrapper.
- The development identity is synthetic and clearly uses platform version `0.1.0` plus zero/one digest sentinels; it is not valid production release evidence.

## Tasks

### Task 1: Compose contract tests

**Files:**
- Create: `deploy/compose/tests/test_dev_compose.py`

- [x] Write tests asserting local API/worker/migration build targets, development mode, no released control image, loopback port, and successful identity-init dependency ordering.
- [x] Run the focused test file and observe failure because `compose.dev.yaml` is absent.

### Task 2: Development identity initializer

**Files:**
- Create: `scripts/dev-compose-init.py`

- [x] Generate a canonical active selection using `HostOperationPlan` and `SelectionReceipt`.
- [x] Create/chown identity, state, route, supervisor, and runtime-secret directories for the expected service users.
- [x] Write the active projection atomically with a root-owned, world-readable projection file.

### Task 3: Local NAS Compose stack and wrapper

**Files:**
- Create: `deploy/compose/compose.dev.yaml`
- Create: `scripts/dev-compose`

- [x] Build API and worker targets from the checkout.
- [x] Start PostgreSQL, migration, identity initialization, API, and worker with development-only settings.
- [x] Generate local database URL, password, tokens, admin key, and SSH signing key files when absent.
- [x] Keep all runtime state in named development volumes and expose only API port `8080` on loopback.

### Task 4: Operator documentation

**Files:**
- Modify: `deploy/compose/README.md`
- Modify: `README.md`

- [x] Document the wrapper commands for NAS development, logs, status, and teardown.
- [x] State clearly that this stack is not a production deployment and does not publish anything.

### Task 5: Verification and handoff

- [x] Run focused Compose tests, Python checks, and `docker compose config`.
- [x] Run a local build/start smoke test when a Docker daemon is available.
- [x] Review the diff and commit/push the development stack.
