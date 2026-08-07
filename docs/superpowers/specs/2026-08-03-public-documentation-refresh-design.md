# Public documentation refresh design

## Purpose

Make the generic Vonk Forge GPU node platform understandable to a new operator without
requiring them to read historical plans, confuse the temporary SSH transport
with the production architecture, or infer which two-GPU node instructions remain
current. Preserve useful legacy material, but make the outbound-agent,
repository-driven workflow the unmistakable target and label implementation
status truthfully during migration.

## Audience and success criteria

The primary reader operates a small fleet of one or more Vonk Forge GPU nodes and a
separate Docker Compose-capable service host. They may be starting from an
empty installation or migrating the repository's original two-GPU node setup.

The documentation succeeds when that reader can:

1. identify the recommended architecture and prerequisites;
2. bootstrap the service host;
3. onboard the first GPU node and repeat the same process for every addition;
4. enroll its outbound mTLS agent and understand the bootstrap/recovery-only
   SSH boundary;
5. understand how Git-backed models and profiles reach the cluster through the
   control API and agent job path;
6. find normal operations, updates, observability, recovery, and release
   procedures;
7. distinguish current generic instructions from legacy or historical ones;
8. configure every required Compose variable and secret without reading the
   Compose implementation; and
9. follow repository-local links without encountering missing documents.

## Information architecture

The README becomes the short public entry point, not an exhaustive manual. It
will describe the service-host and GPU node roles, state that fleet size is not
fixed, present the end-to-end newcomer journey, and link to grouped runbooks:

- Start here: architecture, control-plane bootstrap, node onboarding, fleet
  migration, agent enrollment, and platform operations.
- Administration and models: repository administration, model switching,
  runtime releases, and model cache.
- Observe and recover: node health, observability, control-plane recovery,
  agent repair, SSH recovery, updates, and supply-chain/release verification.
- Legacy and reference: historical two-GPU node inventory, fabric, and bootstrap
  material whose assumptions are intentionally preserved.

Current runbooks will link forward and backward along the recommended journey.
Historical documents will receive a visible scope note when they contain fixed
names, addresses, users, or exactly-two-node procedures. Historical evidence
and audit records will not be rewritten as though they were generic.

## Recommended workflow

The documented default sequence is:

1. prepare a generic Docker Compose-capable Linux service host;
2. configure secrets and deploy the separate control services;
3. onboard exactly one GPU node with `node-install`;
4. approve its evidence and enroll its outbound mTLS agent;
5. submit its generated stable-ID fleet record through Git-backed admin;
6. repeat onboarding independently for every additional GPU node;
7. create or update repository-backed model and profile definitions;
8. preview, validate, submit, and reconcile an eligible commit through the
   control API and agent job path;
9. operate and update through the web UX or `vonkctl admin`; and
10. complete physical and protected-code-host gates before the first release.

Legacy `inventory/cluster.toml`, `node1`/`node2`, fixed addresses, and the
developer controller remain documented only as compatibility or historical
paths. They are not prerequisites for a new generic installation.

Routine SSH orchestration is documented as a temporary compatibility path
until the outbound-agent migration acceptance gates pass. It must not be
described as the completed production boundary, and routine commands must not
silently fall back to it.

## Compose configuration reference

A new runbook will describe the contract exposed by
`deploy/compose/.env.example` and `deploy/compose/compose.yaml`. It will group:

- digest-pinned image references;
- host paths and bind addresses;
- secret-file inputs and minimum handling requirements;
- deployment branch and required-check policy;
- service ownership, volumes, and network reachability; and
- preflight rendering, startup order, health checks, and backup expectations.

Examples use placeholders and generated stable node IDs. They will not embed
real credentials, administrator names, hostnames, or IP addresses.

## Consistency and verification

Documentation tests will exercise behavior meaningful to readers:

- every local Markdown link in the README and current runbooks resolves;
- the README links every required newcomer-stage runbook;
- the recommended path does not describe an exact fleet size as a platform
  requirement; and
- the Compose reference covers the environment and secret names exposed by the
  checked-in example configuration.

Tests will not require historical documents to remove factual fixed-node
records. Scope labels, rather than altered history, prevent those documents
from being mistaken for the current generic workflow.

## Scope boundaries

This refresh changes documentation and documentation verification only. Agent,
CLI, control-plane, and update behavior is specified separately in
`2026-08-03-outbound-node-agent-design.md` and must land before documents mark
that path implemented. The refresh does not alter physical acceptance evidence
or claim that the NAS deployment or first real release has occurred.
