# Dedicated Update Signer Isolation Design

## Objective

Move the Spark update-authority private key, bootstrap root, and persistent
TUF verification cache out of the generic control worker. The worker may ask
for one exact authorization over a local Unix socket, but cannot read signing
material or directly construct a root-verifiable receipt.

## Process boundary

`control-signer` runs the released control-worker image as UID `10003`, with
`network_mode: none`, a read-only root filesystem, no database or route mounts,
read-only private-key/bootstrap/publication inputs, the root-owned
`control-identity/` directory mounted read-only, and one dedicated writable
verifier-cache volume. It creates a mode `0660`, group
`10001` Unix socket in a shared socket volume and accepts only Linux
`SO_PEERCRED` UID `10001`. `control-worker` runs as UID `10001`, mounts the
socket volume, and receives neither signing secret nor bootstrap root.

The update-receipt key is not an administrative authorization credential. A
second Ed25519 key, held by the API/admin authority and never mounted into the
worker or signer, issues immutable action grants. The signer mounts only this
second key's public verification document. A grant binds action, rollout ID,
parent job ID, sorted approved node set, release digest (or null for rollback),
expiry, and nonce. It is valid for at most one hour, and every receipt expiry
must be no later than the grant expiry. The signer rejects missing, expired,
tampered, mismatched, or incorrectly signed grants before either operation.

The socket protocol is one canonical newline-terminated JSON request and one
canonical newline-terminated JSON response, each at most 64 KiB. Duplicate
keys, unknown fields, non-ASCII input, noncanonical serialization, oversized
messages, extra bytes, multiple requests per connection, and unexpected peer
UIDs fail closed.

## Independent signer policy

At startup the signer snapshots its private key and bootstrap root safely and
constructs its own persistent python-tuf verifier. For every authorization it
reopens `active.json` through the read-only identity-directory dirfd with
`O_NOFOLLOW`, requires `projection_kind=active`, and loads the selected control
generation through `ActiveControlReleaseLoader`; it never relies on an init-time
copy and never accepts a candidate projection. The loader compares the
read-only active projection and its exact versioned verified TUF platform target with
signer-local immutable `platform_version`, `release_digest`, and
`build_digest`. Exact API and worker image references are also checked by the
loader. No running-control identity is accepted from the IPC request.

For `agent.update`, the signer refreshes TUF, verifies the exact published
agent artifact and platform identity, verifies the request's expected release
digest, target SHA-256, and targets metadata version, and signs the exact node,
source, operation, fence, attempt,
deadline, and payload binding. For `agent.rollback`, it signs only the typed
operator-rollback vocabulary and exact current node/source binding. Ed25519
produces a deterministic response for an identical canonical request.

## Durable authorization intents

`update_authorization_intents` stores one immutable authorization request per
operation. Required bindings are: action, rollout/job/rollout-node/node IDs,
operation ID, fence, unsigned payload and digest, source slot/SHA/generation,
expiry, expected TUF target digest/version for update, exact API-issued admin
grant and grant digest, canonical request and request digest, response and
response digest, state, and timestamps. States
are `reserved`, `signed`, `queued`, and `stale`; only `queued` has a matching
claimable `agent_operations` row.

The transaction sequence is:

1. A short transaction locks rollout, rollout node, AgentNode, and parent Job;
   validates the state transition; reserves the exact immutable intent; then
   commits.
2. Outside every database transaction and lock, the worker calls the signer.
   A crash or timeout leaves a retryable `reserved` intent. Identical requests
   yield identical responses.
3. A short transaction re-locks the same records and intent, compares the
   exact response and all rollout/node/source/TUF expectations, stores the
   response, and atomically creates the queued operation plus transition. Any
   drift marks the intent `stale` and discards the response.

Recovery retries `reserved` or `signed` intents by request digest. It never
creates a second operation ID/fence, and a crash after the queue transaction
is idempotently observed as `queued`.

Invalid or unbound rollback operations are quarantined without starving a
later eligible operation on the same node.

## Acceptance

Focused tests cover update and rollback exact bindings, TUF/control-identity
mismatch, missing/tampered/mismatched admin grants, arbitrary requests, IPC
bounds/canonicality/peer credentials,
A/B/C crash recovery and CAS drift, deterministic duplicate requests,
unbound rollback starvation, migration reversibility, and Compose secret,
mount, UID, socket, cache, and network separation.
