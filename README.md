# DGX-Forge

The generic Spark platform design and phased implementation plans live in
[`docs/superpowers/specs/2026-08-03-scalable-spark-platform-control-plane-design.md`](docs/superpowers/specs/2026-08-03-scalable-spark-platform-control-plane-design.md).
Each Spark is onboarded independently; the Docker-capable service host runs
separate Caddy, API/worker, PostgreSQL, LiteLLM, Hermes Agent, Prometheus, and Grafana
services. Administration is available through both `sparkctl admin` and the
web UX, with Git-backed fleet, model, and profile definitions.

Before a real release, run `scripts/verify-platform-release --candidate X.Y.Z
--json`. A blocked result is expected until external hardware, recovery, and
protected-code-host evidence exists. PR-only repository mutation is a one-way
transition and must not be enabled from simulator evidence.

DGX-Forge is a collection of contracts, controllers, runtime adapters, and
operational tooling for defining, validating, deploying, and operating
model-serving profiles across NVIDIA DGX Spark systems. The repository keeps
cluster admission and model maturity fail-closed: a checked-in definition is
not treated as production-ready until its evidence gates are accepted.

## Capabilities

- Validate and reconcile content-addressed cluster profiles from Git.
- Execute routine lifecycle and probe operations through outbound, fenced,
  mutually authenticated Spark agents; the control worker never SSHes to a
  Spark.
- Collect durable node, NVIDIA, Docker, thermal, and storage state reported by
  authenticated agents.
- Configure and validate the direct RoCE/NCCL fabric between Spark nodes.
- Build and operate model-specific runtime adapters, including the checked-in
  DeepSeek Mia and DS4 definitions.

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

Inspect the local catalog and controller state without changing either Spark:

```bash
uv run --no-project --with jsonschema -- bin/sparkctl catalog --json
uv run --no-project --with jsonschema -- bin/sparkctl status --json
```

The old direct health command remains available for explicit break-glass and
development use:

```bash
uv run --no-project --with jsonschema -- bin/sparkctl nodes status --json
```

That command performs live SSH probes and is not the production control-plane
transport. Routine production work is repository-planned by the API, persisted
in PostgreSQL, claimed outbound by each Spark agent over mTLS, and reconciled by
the repository-less worker. Read the `sparkctl` runbook before using any direct
mutating command such as `prepare` or `switch`.

## Repository layout

- `bin/` — repository-local command launchers
- `src/spark_profiles/` — profile catalog, admission, state, health, and CLI
- `adapters/` — model-specific runtime definitions and lifecycle tooling
- `config/` — controller, workload, and cluster-profile configuration
- `nodes/` — node bootstrap, health, fabric, and recovery utilities
- `schemas/` — JSON contracts for profiles, workloads, and health evidence
- `tests/` — Python and shell test suites
- `docs/` — architecture notes, design records, plans, and runbooks

## Documentation

- [Architecture overview](docs/architecture-overview.md)
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

## Security

Do not commit credentials. Keep private keys, tokens, and passwords out of
profile files, command arguments, and captured diagnostics. Membership in the
Docker group is root-equivalent and should be limited to trusted operators.

## License

DGX-Forge is available under the [MIT License](LICENSE).
