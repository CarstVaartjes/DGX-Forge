# README and MIT License Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a concise, accurate public README and the canonical MIT License to Vonk Forge.

**Architecture:** Keep the repository root as the public entry point: `README.md` provides orientation and links into the existing detailed documentation, while `LICENSE` carries the complete legal terms. Derive commands and claims from checked-in configuration and runbooks, without changing application behavior or contacting Vonk Forge GPU nodes.

**Tech Stack:** Markdown, MIT License text, Git, `uv`, Python 3.12+

## Global Constraints

- Use `Copyright (c) 2026 Carst Vaartjes` in the MIT License.
- Keep the README concise and operator-facing.
- Do not add badges, contribution policy, support promises, or production-readiness claims for unaccepted model definitions.
- Require Python 3.12 or newer, `uv`, SSH access to the Vonk Forge GPU nodes, and Docker on those nodes.
- Do not exercise or mutate remote Vonk Forge GPU nodes during verification.

---

### Task 1: Add the MIT License

**Files:**
- Create: `LICENSE`

**Interfaces:**
- Consumes: The approved copyright holder and year.
- Produces: The repository's complete MIT licensing terms for the README to reference.

- [ ] **Step 1: Create the license file**

Create `LICENSE` using the unmodified canonical MIT License text, beginning with:

```text
MIT License

Copyright (c) 2026 Carst Vaartjes
```

Include the standard permission grant, copyright-notice inclusion condition,
and warranty/liability disclaimer.

- [ ] **Step 2: Verify the license content**

Run:

```bash
test "$(sed -n '1p' LICENSE)" = "MIT License"
test "$(sed -n '3p' LICENSE)" = "Copyright (c) 2026 Carst Vaartjes"
test "$(wc -l < LICENSE)" -ge 20
```

Expected: all three commands exit `0` and produce no output.

- [ ] **Step 3: Commit the license**

```bash
git add LICENSE
git commit -m "docs: add MIT license"
```

### Task 2: Add the Concise Project README

**Files:**
- Create: `README.md`
- Reference: `pyproject.toml`
- Reference: `docs/architecture-overview.md`
- Reference: `docs/runbooks/vonkctl.md`
- Reference: `docs/runbooks/inventory.md`
- Reference: `docs/runbooks/fabric.md`
- Reference: `docs/runbooks/runtime-release.md`

**Interfaces:**
- Consumes: The `LICENSE` file from Task 1, the `vonk-cluster-profiles` package metadata, the repository launcher `bin/vonkctl`, and existing documentation paths.
- Produces: The root landing page and navigation path into operator documentation.

- [ ] **Step 1: Write the README**

Create `README.md` with these sections and content boundaries:

```markdown
# Vonk Forge

Vonk Forge is a collection of contracts, controllers, runtime adapters, and
operational tooling for defining, validating, deploying, and operating
model-serving profiles across NVIDIA Vonk Forge GPU node systems. The repository keeps
cluster admission and model maturity fail-closed: a checked-in definition is
not treated as production-ready until its evidence gates are accepted.

## Capabilities

- Validate and switch content-addressed cluster profiles with `vonkctl`.
- Collect live node, NVIDIA, Docker, thermal, and storage health over SSH.
- Configure and validate the direct RoCE/NCCL fabric between GPU nodes.
- Build and operate model-specific runtime adapters, including the checked-in
  DeepSeek Mia and DS4 definitions.

## Prerequisites

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- SSH access to the Vonk Forge GPU node hosts configured in `inventory/cluster.toml`
- Docker installed and accessible on each Vonk Forge GPU node host

## Quick start

Install the locked development environment and run the local test suite:

```bash
uv sync --dev
uv run pytest
```

Inspect the local catalog and controller state without changing either GPU node:

```bash
uv run --no-project --with jsonschema -- bin/vonkctl catalog --json
uv run --no-project --with jsonschema -- bin/vonkctl status --json
```

Collect fresh, read-only health data from both configured nodes:

```bash
uv run --no-project --with jsonschema -- bin/vonkctl nodes status --json
```

The last command performs live SSH probes. Read the `vonkctl` runbook before
running mutating commands such as `prepare` or `switch`.

## Repository layout

- `bin/` — repository-local command launchers
- `src/cluster_profiles/` — profile catalog, admission, state, health, and CLI
- `adapters/` — model-specific runtime definitions and lifecycle tooling
- `config/` — controller, workload, and cluster-profile configuration
- `nodes/` — node bootstrap, health, fabric, and recovery utilities
- `schemas/` — JSON contracts for profiles, workloads, and health evidence
- `tests/` — Python and shell test suites
- `docs/` — architecture notes, design records, plans, and runbooks

## Documentation

- [Architecture overview](docs/architecture-overview.md)
- [`vonkctl` runbook](docs/runbooks/vonkctl.md)
- [Inventory runbook](docs/runbooks/inventory.md)
- [Direct-fabric runbook](docs/runbooks/fabric.md)
- [Runtime release runbook](docs/runbooks/runtime-release.md)

## Security

Do not commit credentials. Keep private keys, tokens, and passwords out of
profile files, command arguments, and captured diagnostics. Membership in the
Docker group is root-equivalent and should be limited to trusted operators.

## License

Vonk Forge is available under the [MIT License](LICENSE).
```

Keep model definitions described according to their checked-in maturity; do
not imply that planned or verified definitions are accepted for production
use.

- [ ] **Step 2: Check links and required sections**

Run:

```bash
for path in LICENSE docs/architecture-overview.md docs/runbooks/vonkctl.md docs/runbooks/inventory.md docs/runbooks/fabric.md docs/runbooks/runtime-release.md; do test -e "$path" || exit 1; done
for heading in "# Vonk Forge" "## Capabilities" "## Prerequisites" "## Quick start" "## Repository layout" "## Documentation" "## Security" "## License"; do rg -F "$heading" README.md >/dev/null || exit 1; done
```

Expected: both loops exit `0` and produce no output.

- [ ] **Step 3: Check documentation quality and repository state**

Run:

```bash
if rg -n 'T[B]D|T[O]DO|PLACEH[O]LDER' README.md LICENSE; then exit 1; fi
git diff --check
git status --short
```

Expected: no placeholder or whitespace-error output; status lists only the
intended new README before it is committed.

- [ ] **Step 4: Commit the README**

```bash
git add README.md
git commit -m "docs: add project README"
```

- [ ] **Step 5: Perform final verification**

Run:

```bash
git diff --check HEAD~2..HEAD
git status --short --branch
```

Expected: the diff check exits `0`; the working tree is clean and the branch
is ahead of `origin/main` only by the intentionally created local commits.
