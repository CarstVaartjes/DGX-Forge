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

Never use `dgx-control-offline` for ordinary repository administration. Its
exclusive lock and stopped-service proof are only for bootstrap and recovery.
