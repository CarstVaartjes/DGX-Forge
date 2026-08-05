# Repository-backed administration

Git is the only authority for fleet nodes, topology, model repositories,
profiles, policy, and desired deployment state. PostgreSQL holds operational
jobs, observations, audit events, and reconciliation results; it must never be
used to create an alternative desired state.

## Inspect and propose

Use either the web application or `sparkctl admin`. Both call `/api/v1` and
produce the same canonical proposal bytes. Every proposal pins a full 40-hex
base commit, operates only on allowlisted typed documents, and shows validation,
affected targets, and a diff before submission.

```bash
sparkctl admin fleet --json
sparkctl admin models --json
sparkctl admin profiles --json
sparkctl admin proposal --file change.json --json
```

Before the first real release, an administrator may explicitly submit a signed,
audited direct commit. Enabling `release-pr-only` at the first release is a
one-way transition. From then on, submission creates `dgx-control/<digest>` and
a pull request; it never force-pushes or deploys an unreviewed branch.

## Reconcile

Only a commit reachable from the protected deployment branch with every exact
required check in the successful state is eligible. Planning pins that commit,
sorted node targets, placements, routes, immutable releases, and all input
digests. Execution rechecks eligibility immediately before any node mutation.

Affected routes enter maintenance before work begins. Node leases are acquired
in sorted stable-ID order. Workloads must pass health and acceptance before
routes publish atomically. A failed apply, verification, stale lease, revoked
check, or changed digest fails closed: affected routes remain withdrawn and the
job/audit records explain the bounded failure.

Each `routes` entry in the commit-pinned reconciliation document names an
alias, the certificate-bound target identity, and a repository workload. It
does not contain an address or port:

```json
{
  "routes": {
    "deepseek": {
      "node_id": "spk_0123456789abcdef0123456789abcdef",
      "workload": "deepseek-agent-single",
      "requests_per_minute": 30,
      "tokens_per_minute": 10000
    }
  }
}
```

The worker resolves the port from that exact commit's
`config/workloads/<workload>.toml`, resolves the address from fresh
certificate-authenticated presence, and probes `/v1/models` with the
file-backed upstream credential. It then writes a generated JSON-as-YAML config
to the dedicated `litellm-routes` volume. LiteLLM's in-container supervisor
restarts the proxy only when that atomic file changes. Every 60 seconds the
worker repeats presence resolution and the probe; an expired agent observation,
failed probe, changed checkout, or invalid definition replaces the live config
with an empty `model_list`.

Never use `dgx-control-offline` for ordinary repository administration. Its
exclusive lock and stopped-service proof are only for bootstrap and recovery.
