# Operate a repository-driven Spark platform

Onboard each Spark independently with `spark-install`; never place an address,
name, or assumed fleet size in code. After acceptance, emit its canonical fleet
record and submit it through the admin CLI or web UX. Models and profiles follow
the same preview, validation, and repository review path.

The control worker reconciles only an eligible merged commit. It withdraws the
affected route first, leases stable node IDs in sorted order, applies exact
repository revisions, verifies health, then atomically publishes Caddy/LiteLLM
state. A failure or withdrawal remains HTTP 503 maintenance.

Run the simulated full lifecycle before release:

```bash
scripts/accept-platform-lifecycle --host new-spark.local --display-name new-spark \
  --output inventory/reports/platform-lifecycle.json --json
```

The report explicitly identifies simulated boundaries. The first real release
also requires an approved physical Spark lifecycle and a protected code-host
PR/merge lifecycle; do not convert simulator evidence into those claims.
