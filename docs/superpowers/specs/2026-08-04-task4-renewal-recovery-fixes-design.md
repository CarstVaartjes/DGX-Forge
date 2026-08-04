# Task 4 Renewal and Recovery Fixes Design

## Scope

This fix round closes three correctness defects in Task 4 without changing the
agent protocol or weakening the active/staged certificate boundary:

1. Certificate renewal must durably record issuance authority before calling
   an external CA, and an ambiguous CA outcome must never cause automatic
   reissuance.
2. Downgrading the rotation migration must not make a staged certificate
   authenticatable by the pre-rotation application predicate.
3. Restarting with an expired active claim must produce and durably replay one
   bounded exact-fence terminal result before accepting new work.

## Durable renewal intent

Add an `agent_certificate_rotations` table and matching ORM model. A node has at
most one unresolved rotation. Each row records the node, source certificate,
reserved generation, canonical CSR, CSR public-key fingerprint, a provider
request ID, state, and bounded lifecycle timestamps. The meaningful states are
`issuing` and `manual-recovery`.

Renewal is split across committed transaction boundaries:

1. Validate and lock the node/source certificate, return an existing staged
   exact replay when present, and otherwise insert an `issuing` intent. This
   transaction commits before any provider call.
2. Only the request that created the intent may call the CA. Separate service
   instances that observe a fresh exact `issuing` intent return a retryable
   in-progress response without calling the CA. A different CSR is denied.
3. A successful provider result is validated and atomically persisted as the
   staged certificate while deleting the intent. The staged certificate then
   remains the durable replay authority.
4. A provider exception, invalid provider result, or post-provider persistence
   failure durably changes the intent to `manual-recovery` and returns a
   terminal denial. Exact retries never call the CA.
5. A process death can leave `issuing`. It remains retryable for one bounded
   provider window so a concurrent owner can finish. Once stale, the next
   exact request atomically changes it to `manual-recovery`; no request calls
   the CA from a pre-existing intent.

The persisted provider request ID is passed through the CA interface. Smallstep
uses it as the sign token `jti`, so provider logs and replay protection refer to
the durable control-plane operation rather than a new random token per attempt.
The built-in CA accepts the request ID at the same boundary but does not need it
for local signing because the control-plane intent already permits one call.

The agent API maps fresh in-progress intent to canonical 503 so the same local
CSR is retried, and maps `manual-recovery` to canonical terminal 403. Durable
control-plane state, not agent retry suppression, is authoritative.

## Reversible downgrade safety

Migration 0005 creates the rotation-intent table along with the certificate
state/generation columns. Before downgrade removes the state discriminator, it
sets `revoked_at` on every certificate whose state is not `active`, then drops
the intent table and rotation columns. Under the 0004 application predicate,
which recognizes validity through `revoked_at IS NULL`, a formerly staged
certificate therefore remains denied. Migration tests exercise upgrade,
staged-row downgrade, the literal prior authentication predicate, re-upgrade,
and current model/head parity.

## Expired active claim recovery

`OperationRegistry.execute` continues to prefer completed inspection evidence.
If an unresolved exact active claim cannot bind its wall-clock deadline, the
registry constructs a failed result with the original job, operation, attempt,
fence, node, and deadline and the stable bounded error code
`claim_deadline_expired`. It calls `AgentStateStore.finish` before returning.

`Agent.run_once` then submits that persisted result. Transport failure leaves it
pending; restart replays identical canonical bytes. A 204 or exact stale 409
acknowledges it locally. Only a later run may claim new work.

## Verification

Strict RED/GREEN tests cover provider exceptions, process death, response-loss
and persistence ambiguity, exact retry, CSR mismatch, SQLite and PostgreSQL
concurrency, transaction ordering, downgrade denial/parity, and expired active
result replay before a new claim. The amended Task 4 report records focused and
full agent/control/protocol/migration/Compose suites, Ruff, compile/build,
fresh-wheel smoke tests, and supply-chain verification.
