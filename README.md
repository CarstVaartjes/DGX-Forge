# vonk-forge

`vonk-forge` is a local-first control plane for one or more NVIDIA DGX Sparks.
Each Spark is onboarded independently; the Docker-capable service host runs
separate Caddy, API/worker, PostgreSQL, LiteLLM, Hermes Agent, Prometheus, and Grafana
services. Administration is available through both `sparkctl admin` and the
web UX. The target recipe workflow is local-first: PostgreSQL is authoritative
for recipe families, authored/imported revisions, installations, placements,
and runs. Git/TUF remains the authority for platform source and the existing
workload-release path while the catalog migration is completed; a recipe never
needs a Git commit or pull request in order to be imported or run.

Before a real release, run `scripts/verify-platform-release --candidate X.Y.Z
--json`. A blocked result is expected until external hardware, recovery, and
protected-code-host evidence exists. PR-only repository mutation is a one-way
transition and must not be enabled from simulator evidence.

`vonk-forge` is a collection of contracts, controllers, runtime adapters, and
operational tooling for defining, validating, deploying, and operating
model-serving profiles across NVIDIA DGX Spark systems. The repository keeps
cluster admission and model maturity fail-closed: a checked-in definition is
not treated as production-ready until its evidence gates are accepted.

## Capabilities

- Validate and reconcile the existing content-addressed platform and cluster
  definitions from Git/TUF.
- Author recipes locally, import SparkRun profiles with a field-by-field report,
  or download immutable revisions from the optional global catalog. Local
  PostgreSQL remains usable when the global service or Git remote is unavailable.
- Execute routine lifecycle and probe operations through outbound, fenced,
  mutually authenticated Spark agents; the control worker never SSHes to a
  Spark.
- Collect durable node, NVIDIA, Docker, thermal, and storage state reported by
  authenticated agents.
- Configure and validate the direct RoCE/NCCL fabric between Spark nodes.
- Build and operate model-specific runtime adapters, including the checked-in
  DeepSeek Mia and DS4 definitions.
- Publish and operate generic, signed workload packages independently from
  `vonk-forge` platform releases; ordinary model/runtime releases do not require
  an agent update.
- Review and apply NAS-to-Spark platform skew updates through the Admin web UX
  or `sparkctl`, with explicit signed fan-out over the outbound agent channel.

## Prerequisites

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- SSH access for one-time onboarding and explicit operator recovery only
- Docker installed and accessible on each DGX Spark host

## Quick start

Install the locked development environment and run the local test suite:

```bash
uv sync --dev
uv run pytest
```

The repository deliberately keeps expensive acceptance work local. Pull
requests run only the focused contract smoke checks and generated-client drift
check in GitHub Actions. Before requesting review, run the full local tiers
that match the change:

```bash
uv run --frozen pytest -q
uv run --project control --frozen --with-editable . pytest -q control/tests
npm ci --prefix control/web && npm test --prefix control/web -- --run
uv run --frozen pytest -q deploy/compose/tests
scripts/verify-supply-chain --json
```

The simulated workload acceptance and failure matrix run on manual/tagged
workflow executions so release evidence is still produced without charging
every PR for the longest jobs.

The protected `Main` ruleset requires the three PR checks (`Ruff`, `Generated
control clients`, and `PR contract smoke`). A successful merged PR lifecycle is
recorded in `inventory/reports/code-host-protection.json`; heavyweight
acceptance remains outside the PR path by design.

See [Testing and CI policy](docs/testing-and-ci.md) for the exact local tiers,
the hosted smoke subset, and the release-only acceptance gates.

Configure the authenticated control origin and restrictive token file, then
inspect current node state and preview the exact server reconciliation plan:

```bash
export DGX_CONTROL_URL=https://control.example.invalid
export DGX_CONTROL_TOKEN_FILE=/run/secrets/dgx-control-token
uv run --project /path/to/vonk-forge sparkctl nodes status --json
uv run --project /path/to/vonk-forge sparkctl validate PROFILE --json
uv run --project /path/to/vonk-forge sparkctl switch PROFILE --json
```

`prepare`, `switch`, and `restore-default` are plan-only unless `--apply` is
present. Applied commands wait for the accepted job by default; use `--no-wait`
to return its job ID for later API polling. Routine commands fail with
`error_type=control_api` when the control plane is unavailable and never fall
back to SSH.

The old local controller remains available only as an explicitly named
migration/recovery compatibility tool:

```bash
uv run --no-project --with jsonschema -- bin/sparkctl-legacy status --json
```

Never use or configure `sparkctl-legacy` as a production command. It is never
selected implicitly. Routine production work is repository-planned by the API,
persisted in PostgreSQL, claimed outbound by each Spark agent over mTLS, and
reconciled by the repository-less worker.

## Repository layout

- `bin/` — repository-local command launchers
- `src/spark_profiles/` — profile catalog, admission, state, health, and CLI
- `adapters/` — model-specific runtime definitions and lifecycle tooling
- `config/` — controller, workload, and cluster-profile configuration
- `nodes/` — node bootstrap, health, fabric, and recovery utilities
- `schemas/` — JSON contracts for profiles, workloads, and health evidence
- `tests/` — Python and shell test suites
- `docs/` — architecture, security, testing, and operator runbooks

## Documentation

- [Documentation index](docs/README.md)
- [Architecture overview](docs/architecture-overview.md)
- [Recipe catalog and SparkRun operations](docs/runbooks/workload-packages.md)
- [NAS pull-only Compose deployment](deploy/compose/README.md)
- [Control-plane bootstrap](docs/runbooks/control-plane-bootstrap.md)
- [`sparkctl` runbook](docs/runbooks/sparkctl.md)
- [Inventory runbook](docs/runbooks/inventory.md)
- [Generic fleet migration](docs/runbooks/fleet-migration.md) — generated node
  identities and compatibility with the current inventory, with no fixed node
  count
- [Direct-fabric runbook](docs/runbooks/fabric.md)
- [Runtime release runbook](docs/runbooks/runtime-release.md)
- [Spark agent PKI and recovery runbook](docs/runbooks/agent-pki.md)
- [Tailnet-only NAS ingress runbook](docs/runbooks/tailscale.md)
- [Hermes Agent runbook](docs/runbooks/hermes-agent.md)
- [Workload package operations](docs/runbooks/workload-packages.md) — generic
  family/release publication, rollout, rollback, repair, GC, and first-release
  evidence
- [Platform update runbook](docs/runbooks/platform-update.md) — NAS/Spark
  platform skew and recovery boundaries

## Security

Do not commit credentials. Keep private keys, tokens, and passwords out of
profile files, command arguments, and captured diagnostics. Membership in the
Docker group is root-equivalent and should be limited to trusted operators.

## License

`vonk-forge` is available under the [MIT License](LICENSE).
