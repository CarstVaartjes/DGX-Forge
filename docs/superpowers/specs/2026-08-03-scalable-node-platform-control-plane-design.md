# Scalable GPU node Platform and Control Plane Design

**Status:** approved for implementation planning on 2026-08-03.

## Purpose

Vonk Forge will support a small, administrator-owned fleet of Vonk Forge GPU nodes without
encoding a fixed node count, node names, user names, LAN addresses, or fabric
addresses in application logic. Adding a GPU node is a repeatable installation
operation. Models, model revisions, profiles, and desired cluster state remain
repository-backed. A Docker-capable Linux machine runs the surrounding control
plane; the first deployment target is a UGREEN DXP480T, but the design does not
depend on UGREEN software or hardware.

The system is optimized for a small number of administrators and GPU nodes. It has
no product-level node limit. Capacity and topology constraints come from
validated workload definitions and the physical cluster, not constants such as
two or sixteen.

## Relationship to the current roadmap

This design is a parallel platform-enablement program, not a replacement for
the model-runtime roadmap.

The existing roadmap retains ownership of:

- model research, runtime adapters, immutable release artifacts, and acceptance
  evidence;
- the current DS4 and Mia work;
- model-definition and profile semantics until a planned schema migration
  changes them;
- AI-only operation on every GPU node; and
- the developer-machine SSH transport work already in progress.

This design owns:

- generic per-node onboarding and installation;
- N-node inventory, topology, and placement contracts;
- the service-host Compose platform;
- the control API, worker, CLI, and browser administration interface;
- Git-backed proposal, review, reconciliation, and audit workflows; and
- gateway, API routing, observability, backup, and recovery services.

The historical `2026-08-01-external-control-plane.md` plan is superseded. It
assumed exactly two named GPU nodes and did not cover generic onboarding,
repository maintenance, a complete admin interface, or operational services.
Its useful safety properties are incorporated here: no support containers on a
GPU node, restricted SSH, serialized mutations, fail-closed routing, and durable
state.

During implementation, changes to files being modified by the runtime-roadmap
agent are deferred until that work lands. The generic platform consumes the
resulting SSH transport and runtime-release interfaces rather than creating a
competing implementation.

## Architectural shape

The deployment is one Docker Compose application made of separate containers.
It is not one container containing every dependency, and it is not a network of
custom microservices.

Vonk Forge itself is a modular application with typed internal boundaries. Its
API and worker run as separate processes from the same image and codebase:

- `control-api` serves the control API and, initially, the compiled admin UI;
- `control-worker` claims durable jobs and performs onboarding, probes,
  validation, reconciliation, and deployments;
- `postgres` stores operational state, durable jobs, audit events, and cached
  observations;
- `caddy` terminates TLS, authenticates ingress, serves maintenance responses,
  and routes admin and inference traffic;
- `litellm` provides OpenAI-compatible aliases, routing, quotas, and usage
  accounting but has no cluster-control authority;
- `prometheus` collects metrics;
- `grafana` presents dashboards; and
- `alertmanager` is optional until alert destinations are configured.

All standard services have independent images, health checks, resource limits,
volumes, upgrades, and backup procedures. PostgreSQL is the only required
database. A message broker is not introduced: workers claim PostgreSQL-backed
jobs with transactional locking. This is sufficient for the expected scale and
avoids an additional recovery domain.

The initial UI can be served by `control-api`. It may become a separate static
container if its build or release lifecycle warrants it; that does not change
the control API.

## Sources of truth

Git and PostgreSQL have deliberately different responsibilities.

Git is authoritative for desired and reproducible state:

- cluster and topology declarations;
- sanitized node identity and capability records;
- model definitions and immutable dependency pins;
- cluster profiles and placement requirements;
- accepted evidence references;
- policy and configuration schemas; and
- the desired deployed profile or deployment declaration.

PostgreSQL is authoritative only for operational state:

- users, roles, and sessions where not delegated to an identity provider;
- durable job status, attempts, leases, and progress;
- observations, heartbeats, and time-series references;
- audit events and request correlation;
- the last successfully reconciled Git commit; and
- transient maintenance and routing state.

The database cannot silently create a deployable model or profile. The worker
reconciles only a validated repository commit. Generated repository changes use
canonical serialization so the CLI, UI, and CI produce identical diffs.

Before the first real release, authorized administrators may create ordinary
commits through the admin interfaces to accelerate development. Starting with
the first production release, the repository policy changes irreversibly to
review mode: the control plane creates branches and pull requests, protected
deployment branches reject direct writes, and only merged commits are eligible
for reconciliation. Emergency rollback selects a previously merged, accepted
commit; it does not bypass review by writing new desired state.

Secrets never enter Git, job arguments, audit payloads, or collected evidence.
Compose secrets or an external secret provider supplies Git credentials,
service credentials, API keys, and SSH key references.

## Generic node identity and inventory

Every GPU node receives a generated immutable node ID, separate from mutable
display names, hostnames, roles, SSH aliases, and addresses. Node collections
are maps keyed by node ID. Schemas use `minItems` and uniqueness constraints
where necessary but no fixed property names or maximum fleet size.

A node record contains:

- immutable node ID and hardware identity evidence;
- administrator-selected display name and optional labels;
- current hostname and management endpoint;
- SSH user and credential reference, never private key material;
- platform, GPU, memory, storage, network, and RDMA capabilities;
- topology links and interface observations;
- installation version and applied policy digest;
- lifecycle state such as discovered, installing, ready, quarantined, draining,
  or retired; and
- timestamps and evidence references.

Addresses and interface names are discovered or supplied as installation
inputs and then validated. They are data, not application constants. Changing a
management address does not change node identity.

## Per-GPU node installation mode

Installation is an explicit, resumable operation invoked once for every GPU node
addition and again only for upgrade, repair, or policy reconciliation. Both the
bootstrap CLI and admin UI drive the same installation state machine.

The operation accepts a reachable management endpoint, SSH user, credential
reference, and optional labels. It then:

1. establishes a trusted physical or out-of-band identity gate before accepting
   host keys or credentials;
2. detects cloned or unsafe machine and SSH identities and requires console
   repair when remote trust cannot be established safely;
3. captures immutable pre-change inventory and checks supported platform
   prerequisites;
4. installs and verifies the administrator public key without copying a private
   key or forwarding an agent;
5. installs idempotent SSH hardening through a staged positive and negative
   access check with a documented recovery path;
6. applies other node policy, including early-OOM policy and required runtime
   prerequisites, through versioned idempotent installers;
7. collects post-change inventory and records the applied policy digest;
8. discovers fabric-capable interfaces and proposes topology work without
   assuming a two-node direct link;
9. runs health and safety acceptance; and
10. commits or proposes the sanitized node record and marks the node ready only
    after every required gate passes.

Mutating steps are individually journaled, idempotent, checksum-verified, and
safe to resume. A failure leaves the node in a declared state and never advances
to another node implicitly. Operations that can sever access require a live
recovery channel and a fresh-session proof.

The installer does not automatically join arbitrary nodes into a fabric.
Topology configuration is a separate reviewed operation because direct links,
switch fabrics, subnets, MTUs, routing, and distributed runtime support vary.

## Topology and placement

Topology is represented as nodes plus typed links. A link can describe a direct
fabric connection, a switched fabric membership, or a management-network path.
It records endpoints, interfaces, addresses, measured capabilities, and
accepted evidence without assuming `head` and `worker` roles.

Model definitions declare requirements rather than specific fleet names, for
example:

- one healthy GPU node with a minimum measured memory budget;
- two nodes connected by an accepted RDMA link;
- N homogeneous nodes with a runtime-validated topology; or
- an explicit pinned node set when evidence is hardware-instance-specific.

Profiles contain desired workloads and placement constraints. A deterministic
planner produces a concrete placement. The exact placement and definition
hashes are accepted evidence inputs. The planner never infers that code capable
of N processes is valid across N physical GPU nodes; distributed placement remains
disabled until that model definition and topology pass acceptance.

The existing two-node schemas and `node1`/`node2` definitions migrate through
versioned compatibility readers. Current accepted evidence remains readable.
New schemas are written only in the generic form. Migration is explicit,
testable, and does not rewrite runtime-roadmap files while they are in flight.

## Control API, CLI, and web administration

The control API is the only normal mutation boundary. It exposes typed,
versioned operations for nodes, topology, models, profiles, repository changes,
jobs, deployments, health, routes, and audit events.

The CLI and browser use the same API, authorization checks, validation,
canonical serializers, and job system:

- the CLI covers bootstrap, automation, CI, advanced diagnostics, recovery, and
  every administrative operation;
- the web interface covers fleet overview, node onboarding, topology, model and
  profile maintenance, validation results, diffs, pull requests, deployments,
  job progress, logs, and audit history; and
- neither interface shells out to the other or implements independent cluster
  logic.

A narrow offline CLI initializes or recovers the Compose platform when the API
is unavailable. Offline commands are explicit, local to the service host, and
cannot silently compete with a healthy control plane. Routine CLI commands call
the API.

The initial roles are viewer, operator, and administrator. Viewers inspect;
operators run already-approved profiles and safe diagnostics; administrators
onboard nodes, propose repository changes, manage policy, and perform recovery.
Destructive or access-affecting jobs require an administrator and a preview of
the exact targets and commit.

## Repository-driven model and profile workflow

The admin interfaces discover models and profiles from the checked-out
repository. Creating or updating one is a structured repository change:

1. load the schema version and immutable base commit;
2. edit a typed draft;
3. validate schema, policy, references, pins, placement, and acceptance status;
4. show the canonical diff and affected profiles/nodes;
5. create a commit directly during pre-release, or a branch and pull request
   after the first release;
6. let repository CI run contract and adapter tests; and
7. reconcile only after the eligible commit is merged and selected as desired
   state.

The UI does not edit arbitrary repository paths. Server-side allowlists and
typed writers constrain changes to supported documents. Advanced changes remain
possible through normal Git tooling and enter through the same CI and
reconciliation gates.

Model artifacts and container images are referenced by immutable digests. They
are not stored in PostgreSQL or proxied through the service host during
inference. GPU nodes retain verified local caches, preserving the existing rule
that the NAS is not in the model-data or tensor-traffic path.

## Deployment and reconciliation flow

Reconciliation is commit-based and fail-closed:

1. resolve and verify the selected Git commit;
2. validate all referenced definitions, profiles, topology, policy, and
   acceptance evidence;
3. compute a read-only plan with exact nodes, routes, releases, and stop/start
   order;
4. enter gateway maintenance for affected routes and drain requests;
5. acquire a durable cluster or node-scoped lease;
6. execute resumable worker steps through restricted node commands;
7. require health, identity, capacity, topology, and model-specific acceptance;
8. publish LiteLLM and Caddy routing only for healthy endpoints; and
9. record the resulting commit, placement, evidence, and audit trail.

Failure leaves affected routes unavailable and heavyweight workloads in a known
stopped state. It never advertises the previous or partially started profile as
healthy. Recovery can retry from a safe checkpoint or select a previously
accepted commit through a new audited job.

Workers use leases with heartbeats and fenced attempt numbers. After a process
or host restart, an expired job is inspected before retry; non-idempotent steps
require an explicit compensating or verification step. Only one incompatible
mutation may own a node at a time, while independent read-only probes can run
concurrently.

## Networking and security

Caddy is the only exposed HTTP entry point. Admin, metrics, PostgreSQL, Caddy's
admin endpoint, and worker interfaces remain on private Compose networks.
LiteLLM accepts inference traffic from Caddy and routes only to endpoints
published by the control plane.

The service host reaches GPU nodes over restricted SSH identities. Node-side
forced commands or narrowly scoped command dispatchers validate operation names
and arguments; the control plane does not receive unrestricted root shells for
routine operation. Administrator bootstrap credentials are separate from
service credentials. Agent forwarding and copying private administration keys
to GPU nodes remain forbidden.

TLS, session security, CSRF protection, request limits, and security headers are
enforced at the appropriate Caddy and application layers. Every mutation has an
authenticated actor, request ID, base commit, target set, result, and redacted
audit record.

## Observability, evidence, and backups

Prometheus collects control-plane, job, route, and sanitized GPU node health
metrics. It does not replace checked-in acceptance evidence. Grafana provides
fleet, node, profile, model endpoint, job, and capacity dashboards. Alerts are
introduced only with actionable destinations and runbooks.

Logs are structured and correlated by request and job ID. Initial deployments
use Docker's bounded local logging plus exportable job logs; a separate log
database is deferred until retention or search needs justify it.

Backups cover PostgreSQL, Compose configuration, Caddy state, Grafana
provisioning, encrypted secret-provider metadata as applicable, and repository
mirrors. Git remains independently recoverable from its remote. Backups are
encrypted, copied off the service host, retention-managed, and restore-tested.
Prometheus samples are disposable unless operational requirements later demand
long-term retention.

The service host is an availability dependency for shared administration and
gateway access, but not for node-local model data or direct fabric traffic. A
documented recovery procedure rebuilds the Compose stack on another generic
Docker-capable Linux host from Git, secrets, and database backups.

## Delivery phases and conflict boundaries

### Phase 0: Contracts and migration seam

Define generic node, topology, installation, job, and repository contracts.
Add compatibility readers for the current two-node files. Do not change active
runtime adapters or the in-flight SSH transport implementation.

### Phase 1: Generic onboarding CLI

Implement the resumable per-node installation state machine, idempotent node
installers, evidence capture, and Git node-record proposal. This phase can run
from a developer machine before the service host is ready.

### Phase 2: N-node controller core

Generalize inventory, health, placement, and backend iteration over configured
node IDs. Migrate existing two-GPU node configurations without changing their
accepted runtime behavior. Begin only after the roadmap agent's transport work
lands; consume its interface.

### Phase 3: Service-host foundation

Add the portable Compose stack, PostgreSQL schema, control API, durable worker,
Caddy admin ingress, authentication, secrets, backups, and offline bootstrap
CLI. Validate first on a generic Linux Docker environment, then on the DXP480T.

### Phase 4: Git-backed administration

Add typed model/profile editors, canonical diffs, pre-release commit mode,
post-release branch/PR mode, CI status, reconciliation, deployment history, and
the web administration experience. The existing repository definitions remain
the source material.

### Phase 5: Shared inference and operations

Add fail-closed Caddy route publication, LiteLLM aliases and policy, Prometheus,
Grafana, optional Alertmanager, capacity views, and tested service-host recovery.
Enable a route only for model definitions already accepted by the runtime
roadmap.

### Phase 6: Hardening and first release

Complete threat modeling, role and permission review, upgrade/rollback tests,
backup restoration, service-host loss drills, multi-node scale tests, operator
documentation, and release acceptance. Enable protected-branch/PR-only mutation
when the first real version is released.

Each phase has its own implementation plan and commits. Phases may overlap only
where file ownership and interfaces are disjoint. Runtime-roadmap acceptance
does not wait for the entire control plane, and control-plane work does not
claim unfinished models as available.

## Testing and acceptance

The program requires:

- schema fixtures for one, two, and at least sixteen nodes, plus an unbounded
  generated fleet test that detects accidental fixed-name assumptions;
- migration tests preserving current two-GPU node definitions and evidence;
- installer tests for clean install, resume, retry, already-applied policy,
  changed address, access-check failure, and recovery-channel loss;
- property and contract tests ensuring CLI and web requests produce identical
  canonical repository changes;
- job lease, restart, fencing, concurrency, and compensation tests;
- Git tests for stale base commits, validation failure, merge eligibility,
  protected branches, and rollback to an accepted commit;
- topology and placement tests that reject unsupported distributed inference;
- security tests for authorization, command allowlists, redaction, CSRF,
  secrets, SSH restrictions, and untrusted repository content;
- Compose integration tests with clean install, upgrade, rollback, backup, and
  restore on generic Linux;
- failure injection for PostgreSQL, worker, Caddy, LiteLLM, Git remote, network,
  and service-host restart; and
- end-to-end acceptance from onboarding a fresh GPU node through proposing,
  merging, reconciling, serving, observing, and safely withdrawing a profile.

No phase is complete based only on unit tests. Mutating installation and
deployment paths require disposable or explicitly approved hardware acceptance
with preserved recovery access.

## Explicit non-goals

- Running Caddy, LiteLLM, databases, monitoring, or the admin UI on a GPU node.
- Kubernetes, a service mesh, an event bus, or custom microservices for the
  expected fleet size.
- Automatically trusting discovered hosts or configuring fabric without a
  reviewed topology operation.
- Treating LiteLLM, PostgreSQL, or the UI as a second model/profile authority.
- Storing model weights on the service host as part of the inference path.
- Claiming generic multi-node runtime support from generic process-launch
  capability.
- Reimplementing active DS4, Mia, SSH transport, or model-qualification work.

## Success criteria

The design is delivered when an administrator can install Vonk Forge on a
generic Docker-capable Linux host, onboard any additional GPU node without source
edits or predetermined names/addresses, observe the fleet through CLI and web,
propose repository-backed model and profile changes, deploy only eligible
merged state after the first release, route healthy inference through Caddy and
LiteLLM, recover safely from failed jobs and service-host loss, and demonstrate
that no non-AI service or unreviewed model authority exists on the GPU nodes.
