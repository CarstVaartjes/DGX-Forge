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

## Agent-derived availability and address changes

The dashboard reports `agent_state`, `agent_last_seen_at`, and `agent_online`
for each Git-accepted node. Online means an active, non-revoked agent has made
an authenticated claim within 150 seconds. Raw observed management addresses
are intentionally omitted from the dashboard.

Installed agents find the control plane through the configured LAN DNS name and
initiate outbound mTLS long polling. The control plane does not scan the subnet.
It learns the direct peer address from the trusted Caddy boundary, validates it
against the management and direct-fabric CIDR policy, and associates the
observation with the certificate-bound `spk_` identity.

When DHCP changes an address, the next authenticated claim supplies the new
observation. Route reconciliation enters maintenance before validating and
publishing that replacement, so the prior address is not retained on failure.
DHCP reservations remain recommended for operational stability, but neither
the fleet document nor Compose needs a hard-coded address for each Spark.
