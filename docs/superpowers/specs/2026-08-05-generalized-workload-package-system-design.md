# Generalized workload package system design

Date: 2026-08-05

Status: architecture approved; implementation planning belongs to the existing
agent process

## Purpose

Define a generic package and release system for every Spark-hosted workload,
including Mia, DS4, model servers, model weights, tokenizers, auxiliary models,
source trees, containers, native executables, and private Python environments.

New workload families and new upstream workload releases must not require a
DGX-Forge platform release. The NAS control plane stores and serves definitions,
resolved release locks, desired state, trust metadata, and operational state.
Each Spark downloads, verifies, installs, caches, validates, activates, and
removes the software and model data needed by its assigned workloads.

This is a standalone design input for the existing agent process. It does not
rewrite an existing implementation plan or authorize changes to the code that
is currently being developed.

## Scope

This design covers:

- discovery of releases from external projects and artifact services;
- immutable resolution of source, software, environment, and model inputs;
- composition of a workload from reusable package components;
- Spark-local fetching, installation, validation, activation, rollback, and
  garbage collection;
- manual and policy-controlled promotion;
- supply-chain validation and provenance;
- compatibility reporting for newly discovered upstream versions; and
- the boundary between workload releases and DGX-Forge platform releases.

It does not replace DGX-Forge platform updates, operating-system management,
driver and firmware updates, node enrollment, or host bootstrap. Those remain
separate platform capabilities.

## Existing context

The existing model-profile designs already describe immutable model
definitions, runtime adapters, model preparation, and Spark-local NVMe caches.
The outbound-agent design already limits the Spark agent to a versioned set of
typed operations and permits content-addressed runtime adapters. The
platform-update orchestration design separately updates the DGX-Forge control
plane, agent, supervisor, and host tooling.

Those pieces do not yet define one generic release plane spanning application
source, Python environments, containers, models, and auxiliary assets. Without
that plane, Mia, DS4, and future solutions risk becoming special cases in the
agent or platform release manifest.

This design adds the missing workload release plane. It preserves the existing
platform release plane and its trust boundary.

## Core decisions

- The NAS contains workload definitions and control metadata, not workload
  software or model payloads.
- Sparks fetch payloads directly from immutable upstream locations or approved
  external mirrors.
- Mia, DS4, and model names are data in package definitions. They are never a
  compiled catalog in DGX-Forge.
- A package family tracks an independently released upstream project or
  component.
- A package release is an immutable, fully resolved lock. Human-readable
  versions are discovery labels; the canonical release identity is a digest.
- A deployment is mutable desired state and is separate from the immutable
  package release.
- Workloads are composed as a shallow, acyclic graph of digest-pinned package
  releases.
- The package engine provides typed `oci`, `python-venv`, and `native`
  execution backends behind one lifecycle contract.
- Workload-specific adapter code may ship as a signed, content-addressed
  package component and runs without host privilege.
- Definitions cannot request arbitrary privileged installation scripts.
- Manual promotion is the default. Policy-controlled automatic promotion is
  optional and uses the same trust, validation, canary, and audit gates.
- A new upstream version or a new package family does not require a DGX-Forge
  release when the existing package schema, fetch protocols, execution
  backends, and unprivileged adapter ABI can express it.
- A new privileged operation, host dependency, source protocol, execution
  backend, or incompatible package-schema feature remains a DGX-Forge platform
  change.

## Alternatives considered

### One monolithic manifest per solution

A monolithic Mia or DS4 manifest would be simple initially but would duplicate
shared runtimes, environments, and models. It would also make independent model
updates and local cache reuse difficult. This option is rejected.

### Adopt a complete general-purpose package manager

Nix-like derivations or treating every object as a container would provide
strong content-addressing, but adopting either as the complete abstraction
would impose an additional platform and force unsuitable artifacts into one
execution model. This option is rejected.

### Composable workload packages

The selected design uses a small DGX-Forge workload schema with OCI-like
content descriptors, TUF-inspired update trust, reproducible environment locks,
and Spark-local generations. It borrows established package-system properties
without replacing the host operating system or requiring every workload to be
containerized.

## Authority and storage boundaries

The authority boundaries are:

- Git on the NAS is authoring authority for package families, promotion policy,
  promoted release locks, deployments, and fleet desired state.
- TUF metadata published by the control plane authorizes definition and release
  lock versions and protects their freshness and consistency.
- PostgreSQL records discovered candidates, resolver output, validation runs,
  jobs, progress, observations, and audit history. It is not desired-state
  authority.
- External upstreams and approved mirrors transport source, images, wheels,
  weights, and other payloads. Their names and tags are not trusted identities.
- Digests and verified source revisions identify payload content.
- Each Spark's local content-addressed store is the installation and execution
  source of truth for materialized workload bytes.

The control API may serve bounded definition documents, release locks, trust
metadata, and bootstrap material. It must not proxy multi-gigabyte model data,
act as the routine wheel or container registry, or make the NAS a workload
artifact hot path.

The OCI registry and TUF mechanisms used for DGX-Forge platform releases remain
valid for that separate release plane. This design does not require workload
payloads to be copied into a NAS-hosted registry.

## Domain model

### Package family

A `PackageFamily` describes how to discover and resolve releases of one
independently versioned upstream project or reusable component. Examples might
include a solution, inference runtime, reusable model, tokenizer, encoder, or
native tool, but the schema has no knowledge of those product names.

A family definition includes:

- stable package identifier and schema version;
- external release source and discovery provider;
- upstream version scheme and channel selectors;
- polling interval and prerelease policy;
- required signature, provenance, license, and origin policy;
- a bounded declarative resolution recipe;
- component and dependency templates;
- supported hardware, operating-system, driver, CUDA, architecture, and
  storage constraints;
- execution backend and adapter ABI version;
- health and acceptance checks;
- canary and promotion policy; and
- retention and rollback policy.

The resolution recipe may bind fields from validated discovery metadata into
typed component descriptors. It is not an unrestricted template language and
cannot execute shell commands or perform arbitrary network access on the NAS.

### Package release candidate

A `PackageReleaseCandidate` is operational state produced when the discovery
controller sees an upstream release. It records:

- the upstream's raw version, channel, publication time, and source identity;
- discovery metadata and its digest;
- resolution, policy, compatibility, and validation state;
- a proposed immutable release lock when resolution succeeds; and
- structured reasons when the release cannot be resolved or supported.

Candidates remain visible when they fail. Discovery must not silently discard
a release merely because its artifact layout, dependency set, signature, or
compatibility differs from earlier versions.

### Package release

A `PackageRelease` is the immutable lock produced from a candidate. It contains:

- package-family identity;
- original upstream version and immutable upstream identity;
- canonical release-lock digest;
- exact component descriptors;
- exact dependency release digests;
- adapter digest and ABI version;
- compatibility constraints;
- required validation suite;
- provenance and policy evidence references; and
- resolver and schema versions.

The canonical digest is computed over a deterministic representation of the
complete lock. The upstream version is display and selection metadata, not an
installation identity.

Changing any input, dependency, source revision, artifact digest, compatibility
constraint, or adapter produces a different package-release digest. An existing
release is never edited in place.

### Deployment

A `Deployment` is mutable desired state that selects one exact package-release
digest and adds operational configuration, including:

- Spark placement or selector;
- replicas and rollout strategy;
- resource limits and accelerator requirements;
- ports and routing;
- runtime arguments and non-secret configuration;
- secret references;
- network policy; and
- activation and availability policy.

Secrets, node assignments, live scaling, and site-specific routing are never
part of a package release.

## Package composition

A solution release may depend on other package releases. For example, a
solution may reference a runtime environment, one primary model, and several
auxiliary models. Reusable or independently versioned components should be
separate package families. Components private to one solution may remain
inside that solution's family.

Family recipes may express dependency constraints during discovery and
resolution. A release lock must replace every constraint with an exact package
release digest before it can be validated or promoted.

The resolver enforces:

- no dependency cycles;
- a configured maximum depth and component count;
- deterministic dependency selection;
- compatibility across the complete graph;
- one canonical lock for the resolved graph; and
- an aggregate size estimate before rollout.

Activation is atomic at the root solution release. A deployment cannot observe
a partially updated dependency graph.

## Component descriptors

Every fetched component uses a typed descriptor containing at least:

- component name and kind;
- media type or fetch-provider type;
- one or more ordered source locations;
- cryptographic digest or provider-specific immutable identity;
- expected size and optional unpacked size;
- target platform selectors;
- materialization method; and
- applicable provenance or signature references.

Supported component classes include:

- version-control source snapshots;
- OCI images and generic OCI artifacts;
- Python lock files, wheels, and source inputs for bounded wheel builds;
- model, tokenizer, dataset, encoder, and other repository snapshots;
- native userspace archives; and
- configuration and workload adapter artifacts.

Source locations are transport hints. A Spark may use any policy-approved
mirror that returns the exact expected content. Redirects, authentication,
domain allowlists, private-address restrictions, declared size, and unpacked
size are validated before content enters the local store.

A branch name, floating tag, OCI tag, abbreviated commit, or model alias is not
sufficient in a release lock. Provider contracts resolve those names to full
immutable identities and content metadata.

## External release discovery

The control plane runs discovery on the NAS because package definitions and
promotion policy live there. Sparks never independently choose which upstream
version to install.

Generic discovery providers initially cover standard release mechanisms such
as:

- Git releases and tags;
- OCI repository tags and indexes;
- Hugging Face repositories and full revisions;
- Python package indexes; and
- signed HTTP release indexes.

This is a set of protocols, not a catalog of supported applications. Adding a
Mia, DS4, or other family is a definition change. A genuinely new discovery
protocol requires a separately reviewed provider capability.

Providers preserve the raw upstream version and apply the version scheme named
by the family, such as SemVer, PEP 440, or an explicitly ordered channel. Opaque
versions are not guessed into an ordering.

Discovery uses conditional requests and durable cursors where the upstream
supports them. It is safe to repeat and never changes a promoted release.

If an upstream reuses a version or moves a tag so previously observed metadata
resolves to different content, the controller quarantines it as an upstream
mutation. It does not silently create a replacement release under the same
identity.

## Resolution and dynamic support

For each discovered candidate, the resolver attempts to produce the complete
immutable lock. Successful resolution means that all components and dependency
constraints have immutable identities, the required evidence is available,
and at least one permitted Spark class satisfies the compatibility constraints.

Resolution on the NAS is metadata-only. The resolver may inspect release
indexes, manifests, checksums, signatures, and provenance, but it does not
download workload payloads merely to invent missing identities. If an upstream
does not expose enough immutable metadata for safe resolution, the candidate is
unsupported until the family points to an approved source or mirror that does.

Resolution does not mean deployment approval. A release becomes supported only
after its policy and validation gates pass.

An ordinary new upstream version is dynamically supported when its existing
family recipe can resolve it. If an upstream release changes file names,
dependency metadata, launch arguments, or artifact topology, the candidate is
reported as unsupported with a structured reason. An administrator may update
the family definition on the NAS and resolve it again. That definition update
does not require a DGX-Forge platform release.

If the changed release needs a capability that the current agent cannot safely
express, it remains incompatible until the platform gains that capability.
Dynamic packaging must not become a bypass around the agent's privilege and
protocol boundary.

## Promotion

Manual promotion is the default:

```text
discovered
  -> resolved
  -> policy checked
  -> validated
  -> awaiting admin approval
  -> canary
  -> fleet promotion
```

Promotion through the admin CLI or web application produces the same reviewed,
audited Git desired-state change as other administrative mutations. The
promoted release lock is published with the required trust metadata before any
Spark is instructed to install it.

A package family may opt into policy-controlled automatic promotion. Automatic
promotion:

- uses a dedicated audited automation identity;
- follows the same Git-backed desired-state path;
- cannot bypass signature, provenance, compatibility, validation, or canary
  gates;
- stops when its failure budget is exceeded; and
- remains reversible by selecting an earlier promoted release digest.

Automatic discovery is always enabled according to family policy. Automatic
execution of newly discovered code is not.

## Spark package store

Every Spark maintains a package store on local NVMe with separate logical
areas for:

- immutable downloaded blobs;
- source and model snapshots;
- OCI runtime content;
- Python wheels and derived wheels;
- immutable environments;
- staged package generations;
- active generation pointers;
- download and installation journals; and
- leases and garbage-collection metadata.

The physical layout is an implementation detail, but all stored content is
addressed by verified identity and cannot be modified through an active
workload path.

Downloads are resumable and idempotent. Temporary data is written outside the
verified namespace and moved into it only after digest and size checks succeed.
Concurrent installations share downloads and use per-content and per-release
locks.

Package releases reference store objects rather than copying common weights or
wheels into each generation. This permits deduplication without weakening
release isolation.

## Python environments

Each package release references a private immutable Python environment. The
environment identity is derived from its interpreter, platform, complete
dependency lock, source inputs, and build recipe.

The Spark:

1. downloads the exact locked wheels and verifies their hashes;
2. performs any permitted source-to-wheel derivation in an unprivileged,
   network-disabled build sandbox after all inputs are present;
3. records derived-artifact provenance and the complete installation result;
4. constructs the environment in a staging path;
5. validates imports and package metadata; and
6. atomically publishes the immutable environment.

Dependency resolution does not occur against the live Python index during
installation. Lock input determines the chosen distributions before the Spark
builds the environment.

Two releases with the same environment identity may reuse the same immutable
environment. Otherwise they receive separate environments. No environment is
upgraded or repaired in place.

## Execution backends and workload adapter

All packages use one versioned workload lifecycle ABI. The initial logical
operations are:

```text
prepare -> verify -> start -> health -> infer -> stop -> verify-release
```

The package engine implements the privileged boundary. It fetches and verifies
content, creates staging paths, applies resource policy, switches identity, and
controls activation. A signed workload adapter implements workload-specific
behavior within those boundaries.

The adapter is a content-addressed package component. It may change with any
package release and therefore does not make Mia, DS4, or another solution part
of the DGX-Forge agent binary. It runs as the unprivileged workload identity,
uses a versioned structured input/output contract, and receives only the paths
and capabilities declared for its release.

The execution backends are:

- `oci`: run a digest-pinned image with declared mounts, identity, devices,
  resources, and network policy;
- `python-venv`: run the package entry point from its immutable environment and
  source snapshot; and
- `native`: run verified userspace executables from an immutable release
  directory.

Definitions cannot request `apt`, arbitrary root shell hooks, kernel modules,
driver changes, unrestricted host paths, or undeclared devices. A workload that
needs those facilities declares a platform compatibility requirement instead
of installing them itself.

## Spark installation state machine

For an exact desired package-release digest, a Spark performs:

```text
preflight
  -> fetch
  -> verify
  -> materialize
  -> package validation
  -> activate
  -> runtime health
```

Preflight checks trust metadata, compatibility, disk capacity, credentials,
source policy, and operation fencing. Fetching obtains payloads directly from
upstream or approved mirrors. Verification completes before content is made
available to materialization.

Materialization creates a complete staged generation. Package validation runs
against that generation without changing the active pointer. Activation is one
atomic pointer change only after every required check succeeds.

Running processes hold leases on their release generation and referenced store
objects. Previous healthy generations remain available according to retention
policy. Rollback reactivates an already verified generation rather than
reconstructing an old environment from mutable upstream state.

Cancellation is safe before activation. After activation, cancellation becomes
a normal desired-state transition or rollback so that the active pointer and
reported state cannot diverge.

## Reconciliation and reporting

The control plane sends the Spark an exact desired package-release digest. The
Spark reports:

- desired and actual release digests;
- lifecycle phase and attempt;
- bytes and objects completed and remaining;
- cache hits and new storage consumption;
- current environment and generation identity;
- validation and health results;
- active and retained rollback generations; and
- structured, retry-classified failures.

Reconciliation is idempotent. Repeating the same desired digest after success
does not fetch, rebuild, or restart unless an explicit repair or restart policy
requires it.

If fetching, verification, materialization, or validation fails, the existing
active release remains unchanged. A canary failure stops fleet rollout. A
failed node does not cause another node to accept a different dependency
resolution for the same package-release digest.

## Credentials, network access, and licenses

Some upstream models and packages require authentication or license acceptance.
Definitions contain credential references and required policy identifiers, not
tokens or private credentials.

The Spark obtains narrowly scoped download credentials through the existing
secret-delivery boundary. Credentials are never written into package locks,
download URLs, process arguments, provenance, or shared cache metadata.

Package policy may require recorded license acceptance before promotion or
installation. A release that cannot be legally or technically fetched by an
assigned Spark reports a policy or credential error; it does not fall back to
an unpinned public alternative.

## Garbage collection and repair

Store objects are eligible for garbage collection only when they are not
reachable from:

- an active generation;
- a retained rollback generation;
- a staged or in-progress installation;
- a running-process lease;
- a locally pinned operator release; or
- another referenced store object.

Garbage collection is quota-aware, journaled, and interruptible. It deletes
derived material before irreplaceable cached downloads when policy permits.
Operators can inspect the proposed reclamation set before forced cleanup.

Repair verifies existing content against its recorded identity. Corrupt
objects are quarantined and refetched; active content is not modified in place.

## Failure taxonomy

At minimum, package operations distinguish:

- discovery unavailable;
- upstream mutation;
- resolution unsupported;
- trust or provenance failure;
- policy or license rejection;
- incompatible platform;
- missing credential;
- insufficient capacity;
- retryable transport failure;
- digest or size mismatch;
- build or environment failure;
- package validation failure;
- activation failure;
- runtime health failure; and
- rollback failure.

Failures include the package family, upstream version, release digest when one
exists, component identity, Spark identity, operation fence, and a redacted
diagnostic summary.

## Platform release boundary

The workload and platform release planes are independent:

| Change | Workload definition/release | DGX-Forge platform release |
|---|---:|---:|
| New Mia, DS4, model, or other package family using existing capabilities | Yes | No |
| New upstream release resolved by an existing family | Yes | No |
| Family recipe update for changed upstream layout | Yes | No |
| New source, model, image, wheel, or adapter content | Yes | No |
| Deployment placement, arguments, secrets, or resources | Yes | No |
| New unprivileged adapter behavior within the existing ABI | Yes | No |
| New discovery or fetch protocol | Possibly | Yes, unless delivered through an already approved extension boundary |
| New execution backend or adapter ABI | Possibly | Yes |
| New host privilege, driver, kernel, or system dependency | No | Yes |
| Agent, supervisor, control protocol, or trust-root change | No | Yes |

The platform must reject a package requiring an unsupported capability with a
specific compatibility result. It must never reinterpret that package as an
arbitrary command.

## Mia, DS4, and existing model migration

Mia and DS4 become initial `PackageFamily` definitions and validation fixtures,
not dedicated package-engine branches. Their source, environments, containers,
weights, tokenizers, encoders, and adapters become ordinary typed components.

Existing model definitions become package families or package dependencies
when independently versioned and reusable. Existing workload/profile desired
state becomes deployments that reference exact package releases.

The migration should preserve existing external APIs until the generic package
path proves equivalent behavior. No new Mia- or DS4-specific install operation
may be added during migration. Any missing capability must be stated in generic
package terms and reviewed against the platform release boundary.

## Verification and acceptance

Automated and end-to-end verification must prove that:

- a package family with a name unknown to the installed DGX-Forge version can
  be added through NAS definitions;
- a new upstream Mia or DS4 version unknown at platform build time is
  discovered, resolved, installed, validated, and activated;
- discovery records unsupported versions and their reasons;
- source, OCI images, wheels, models, tokenizers, and auxiliary assets are
  fetched by the Spark rather than through the NAS;
- every deployed component and dependency is pinned to an immutable identity;
- a private Python environment is built from a complete lock and never mutated
  in place;
- interrupted multi-gigabyte downloads resume without exposing partial content;
- identical blobs, models, wheels, and environments are reused safely;
- moved tags, reused versions, digest mismatches, expired trust metadata, and
  invalid signatures are rejected;
- an incompatible CUDA, driver, architecture, or storage requirement prevents
  activation with a structured result;
- preparation and validation failure preserve the active generation;
- activation is atomic across the complete dependency graph;
- a previous healthy generation can be reactivated without network access;
- manual promotion is the default;
- policy-controlled automatic promotion cannot bypass validation or canary
  gates;
- canary failure stops wider rollout;
- concurrent reconciliation does not duplicate downloads or environments;
- cancellation and restart recover from the operation journal safely;
- garbage collection respects active, retained, staged, and leased content;
- credentials and secrets do not appear in definitions, locks, logs, or store
  metadata; and
- the complete flow requires neither SSH nor a DGX-Forge release for ordinary
  new package families and upstream versions.

The decisive acceptance test starts with a running DGX-Forge version, creates a
new synthetic package family and upstream release after that version was built,
and deploys it successfully using only NAS definition changes and the existing
generic Spark package capabilities.

## Operational risks and mitigations

- **Upstream disappearance:** retain active and rollback generations locally;
  allow multiple approved external sources for the same digest.
- **Mutable upstream metadata:** lock full immutable identities and quarantine
  changed versions.
- **Unbounded downloads or archives:** validate declared and observed sizes,
  quotas, redirects, and expansion limits.
- **Dependency explosion:** bound graph depth and component count and show the
  aggregate download/storage plan before promotion.
- **Arbitrary installation behavior:** keep host mutation in typed package
  primitives and run workload adapters unprivileged.
- **Configuration becoming code:** use a bounded versioned resolver schema;
  reject unknown fields and capabilities.
- **Target-built Python artifacts:** build from locked inputs in a networkless
  sandbox and record derivation evidence.
- **Automatic upstream compromise:** default to manual promotion and require
  trust, policy, validation, and canary gates for automation.
- **NAS bandwidth and capacity pressure:** serve definitions only and fetch
  payloads directly on Sparks.

## Relationship to existing plans

This specification is an input to the existing design and agent process. It
does not replace or rewrite `2026-08-03-platform-update-orchestration.md`.

That plan continues to own DGX-Forge control, agent, supervisor, tooling,
protocol, and host-capability updates. Future planning should add or reference a
separate workload-package implementation path based on this specification and
should remove any assumption that Mia, DS4, or a fixed model catalog must be
compiled into a DGX-Forge release.

Existing reconciliation, agent runtime, model definition, and observability
plans should consume the generic package identity and lifecycle rather than
invent parallel per-workload installation flows.

Implementation is decomposed without renumbering the existing roadmap:

- W1–W4: `2026-08-05-generalized-workload-contracts-and-trust.md`;
- W5–W10: `2026-08-05-spark-workload-package-engine.md`;
- W11–W16: `2026-08-05-workload-package-control-plane.md`;
- W17–W20: `2026-08-05-workload-package-migration-acceptance.md`; and
- ordering/conflict gates: `2026-08-05-generalized-workload-package-roadmap.md`.

The incoming GitHub container-release work remains authoritative for building
and publishing the three DGX-Forge NAS service images and for pull-only NAS
Compose deployment. The workload-artifact builder is a separate generic
workflow for workload payloads; it produces digests, SBOMs, and provenance but
has no workload TUF key and cannot promote desired state.

## Primary references

- [OCI content descriptors](https://specs.opencontainers.org/image-spec/descriptor/)
- [OCI image manifests and artifact guidance](https://specs.opencontainers.org/image-spec/manifest/)
- [OCI Distribution Specification](https://specs.opencontainers.org/distribution-spec/?v=v1.1.1)
- [TUF roles and metadata](https://theupdateframework.io/docs/metadata/)
- [SLSA artifact verification](https://slsa.dev/spec/v1.2/verifying-artifacts)
- [Hugging Face download revisions](https://huggingface.co/docs/huggingface_hub/main/en/guides/download)
- [Hugging Face cache model](https://huggingface.co/docs/huggingface_hub/en/guides/manage-cache)
- [Python `pylock.toml` specification](https://packaging.python.org/en/latest/specifications/pylock-toml/)
