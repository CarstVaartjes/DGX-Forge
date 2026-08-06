# Generic Workload Validation Runner

## Decision

Every package family may declare one Git-authored `validation_deployment` ID.
The document at `config/workload-deployments/<id>.toml` is the only source of
runtime arguments, resource limits, secret references, routing, and topology
used for candidate validation. It is not a model catalog: the deployment is
bound to the candidate's exact family and release digest at validation time.

The NAS/control API creates a validation preview by loading the candidate lock,
family policy, and validation deployment from one eligible repository commit.
The worker-owned validation runner then queues the existing typed
`package.prepare` and `package.health` operations through the authenticated
agent queue on the selected canary Spark. It never invokes package code in the
API process, uses SSH, or activates desired state.

## Binding and lifecycle

The preview and durable `PackageValidationRun` bind:

- candidate ID and immutable release digest;
- family, validation-deployment ID, and deployment-config digest;
- repository commit, compatibility/fleet digest, policy digest, and plan digest;
- exact canary node IDs and operation IDs; and
- bounded progress, evidence, retry disposition, and actor.

The runner rejects any operation whose release or deployment identity differs
from the validated candidate and caps persisted evidence at the same bounded
size as the API contract.

The worker persists the parent job and agent operations before dispatch. The
authenticated result consumer accepts only exact operation, payload, release,
deployment, node, and attempt bindings. A successful prepare followed by
health evidence produces signed, bounded validation evidence. Any failure
leaves the candidate unpromotable and preserves existing active workloads.

Repository changes, candidate-lock changes, fleet observations, deployment
changes, or policy changes invalidate the preview. Validation is retryable
after transient agent/upstream failure and restart-safe because all state is in
PostgreSQL. Promotion still requires the existing administrator preview/apply
and isolated workload-TUF signer boundaries.

## Compatibility

Families without a validation deployment remain visible but cannot be
promoted; the API returns the structured reason `validation-deployment-missing`.
Existing read-only family/deployment projections remain compatible. New model,
runtime, image, checkpoint, or adapter releases only change Git/TUF workload
documents and never require a DGX-Forge agent release.

## Acceptance

The local acceptance suite must prove that an unknown family can be validated
using its newly published deployment, without SSH or `agent.update`; that
active service generations continue serving during preparation; that changing
the deployment or fleet invalidates an old preview; and that missing,
unsigned, or unapproved release inputs remain rejected.
