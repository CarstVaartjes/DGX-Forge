# DGX-Forge

DGX-Forge is a collection of contracts, controllers, runtime adapters, and
operational tooling for defining, validating, deploying, and operating
model-serving profiles across NVIDIA DGX Spark systems. The repository keeps
cluster admission and model maturity fail-closed: a checked-in definition is
not treated as production-ready until its evidence gates are accepted.

## Capabilities

- Validate and switch content-addressed cluster profiles with `sparkctl`.
- Collect live node, NVIDIA, Docker, thermal, and storage health over SSH.
- Configure and validate the direct RoCE/NCCL fabric between Spark nodes.
- Build and operate model-specific runtime adapters, including the checked-in
  DeepSeek Mia and DS4 definitions.

## Prerequisites

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- SSH access to the DGX Spark hosts configured in `inventory/cluster.toml`
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

Collect fresh, read-only health data from both configured nodes:

```bash
uv run --no-project --with jsonschema -- bin/sparkctl nodes status --json
```

The last command performs live SSH probes. Read the `sparkctl` runbook before
running mutating commands such as `prepare` or `switch`.

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
- [`sparkctl` runbook](docs/runbooks/sparkctl.md)
- [Inventory runbook](docs/runbooks/inventory.md)
- [Generic fleet migration](docs/runbooks/fleet-migration.md) — generated node
  identities and compatibility with the current inventory, with no fixed node
  count
- [Direct-fabric runbook](docs/runbooks/fabric.md)
- [Runtime release runbook](docs/runbooks/runtime-release.md)

## Security

Do not commit credentials. Keep private keys, tokens, and passwords out of
profile files, command arguments, and captured diagnostics. Membership in the
Docker group is root-equivalent and should be limited to trusted operators.

## License

DGX-Forge is available under the [MIT License](LICENSE).
