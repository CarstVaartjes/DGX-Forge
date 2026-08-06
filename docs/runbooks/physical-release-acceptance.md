# Perform physical v0.1.0 release acceptance

This runbook owns the six physical gates reported by:

```bash
scripts/verify-platform-release --candidate 0.1.0 --json
```

It separates operations that can be exercised today from evidence-export and
candidate-publication capabilities that are not yet implemented. A physical
test result is not release evidence until the shipped verifier can authenticate
its candidate, release digest, run, claims, and approving authority.

## Current status and ordering blocker

The repository currently has only simulated lifecycle/recovery/update reports.
It validates three signed platform-update envelope types, but does not ship the
approved physical exporter/signer. It also has no verifier-bound signed format
for the physical Spark lifecycle, authenticated encryption, or replacement-host
reports.

In addition, the current stable-tag workflow builds and finalizes one release
chain without a staged physical-candidate boundary. Physical acceptance needs
the exact protected candidate bytes and signed target, while the current
release order requires acceptance before the official tag. A reviewed workflow
must separate immutable candidate creation/target authorization from `stable`
channel and GitHub Release finalization before `v0.1.0` can close.

Do not resolve either gap by building on a workstation, relabelling a simulator
report, editing `remaining_release_gates`, copying a test key, or publishing
`stable` before acceptance.

## Authority separation

The physical acceptance operator performs the drills and stores detailed
evidence in an access-controlled evidence store. A separate approver reviews
sanitized content digests. The approved exporter then creates canonical signed
projections. The release host receives only the public key document and
sanitized envelopes; it never receives the signing private key.

The root-owned public document must be canonical JSON with exactly:

```json
{"algorithm":"ed25519","key_id":"64-lowercase-hex","public_key":"64-lowercase-hex","schema_version":1}
```

`key_id` is the SHA-256 of the 32-byte public key. Provision it out of band at
`/srv/dgx-forge/trust/physical-acceptance-public.json` beneath an entirely
root-owned, non-group/world-writable, non-symlink directory chain.

## Six-gate matrix

| Gate | Physical operation | Passing observation | Verifier/exporter status |
| --- | --- | --- | --- |
| `approved-physical-spark-lifecycle` | Onboard a real Spark with console-verified identity, install the agent, reconcile one repository-backed profile, publish/withdraw routes, and prove post-install health. | Accepted immutable node ID, outbound mTLS presence, seven installation gates, healthy model/profile, route publication and withdrawal, no routine SSH. | No approved signed physical projection exists; blocked pending implementation. |
| `authenticated-encryption-drill` | Produce a real PostgreSQL/site backup with the root-owned age recipients boundary, move it through authenticated off-host storage, and decrypt only through the trusted restore boundary. | Ciphertext/receipt digests match; wrong identity and modified ciphertext fail; restored database/site state is exact; no secret enters evidence. | No approved signed physical projection exists; blocked pending implementation. |
| `physical-replacement-host-drill` | Rebuild a separate Docker-capable host, restore trust/site/generation backup, recover the journaled generation, and keep routes closed until health. | Exact generation/database/CA/audit continuity, maintenance-before-health, route-after-health, and tamper rejection. | No approved signed physical projection exists; blocked pending implementation. |
| `signed-platform-update-manifest-evidence` | Resolve the protected candidate's immutable platform target and native agent payloads through TUF. | Target filename equals its SHA-256; build digest and every agent architecture payload hash match published evidence; positive TUF targets version. | Envelope schema is implemented in the verifier; approved exporter is missing. |
| `physical-control-host-update-recovery` | Upgrade a physical control host from an authorized predecessor, inject a crash, recover the candidate, then roll back to the predecessor. | Candidate generation is recovered; subsequent rollback selects the exact predecessor; recovery/rollback detail digests are retained. | Envelope schema is implemented in the verifier; approved exporter is missing. |
| `physical-spark-canary-rollback` | Through the Admin/CLI signed rollout, update one real Spark's inactive A/B slot, verify it, induce/observe rollback, and keep standard transport on outbound mTLS. | Before/target versions and hashes, slot transition, supervisor generation, rollback evidence, `ssh_used_for_standard_path: false`. | Envelope schema is implemented in the verifier; approved exporter is missing. |

Use [node onboarding](node-onboarding.md), [Spark agent PKI](agent-pki.md),
[control-plane recovery](control-plane-recovery.md), and
[platform release update](platform-release-update.md) for the underlying
operations. Those runbooks do not authorize an operator to manufacture release
evidence.

## Implemented update-envelope contract

All three update envelopes use the fixed filenames:

- `inventory/reports/platform-update-signed-manifest.json`;
- `inventory/reports/platform-update-control-host-recovery.json`; and
- `inventory/reports/platform-update-spark-canary-rollback.json`.

Each is canonical JSON containing `schema_version`, `evidence`, and
`signature`. The evidence binds schema version `1`, `evidence_kind: physical`,
exact evidence type, candidate `0.1.0`, `sha256:` release digest, one UUIDv4 run
ID shared by the parent and all envelopes, UTC observation time, protected
details digest, `physical_exercised: true`, `status: passed`, and typed claims.
The signature is Ed25519 and names the accepted public key ID.

The signed-manifest claims are:

- `platform_target_name` and `platform_target_sha256`;
- `build_digest`;
- positive `tuf_targets_version`; and
- `agent_payload_sha256`, keyed by each published `linux-*` architecture.

The control-host claims are:

- `from_generation` and digest-derived `candidate_generation`;
- `recovered_generation` equal to the candidate;
- `rolled_back_generation` equal to the predecessor; and
- `recovery_evidence_sha256` plus `rollback_evidence_sha256`.

The Spark claims are:

- architecture and canonical `spk_` canary node ID;
- before/target platform versions and distinct agent payload hashes;
- target build digest matching the signed-manifest envelope;
- before, target, and rollback A/B slots;
- positive supervisor generation;
- rollback evidence digest;
- `transport: outbound-mtls-agent-channel`; and
- `ssh_used_for_standard_path: false`.

The parent `inventory/reports/platform-update.json` must be canonical and
content-addressed, name the same candidate/release/run, use
`evidence_kind: physical`, mark all three physical evidence entries exercised,
and bind each envelope SHA-256. The approved exporter—not a text editor—must
produce it.

## Missing exporter requirements

Before physical acceptance can run as a release gate, a reviewed implementation
must add:

1. an offline or separately controlled signer/exporter that accepts only
   content-addressed sanitized detail records and a pre-issued candidate/run;
2. verifier-bound physical formats for Spark lifecycle, encryption, and
   replacement-host evidence instead of trusting removable strings in
   `remaining_release_gates`;
3. negative tests for wrong candidate/release/run/key, malformed claims,
   simulator relabelling, replay across releases, mismatched envelope hashes,
   and cross-envelope manifest/Spark disagreement;
4. a staged candidate workflow that builds the exact protected artifacts,
   authorizes an immutable non-stable target for physical use, and permits
   final `stable` publication only after all accepted evidence is attached; and
5. a revocation/expiry policy for issued run IDs and the physical authority.

This implementation is a prerequisite for the tag, not a documentation-only
exception.

## Evidence handling

Detailed records may contain node IDs, versions, slot/generation identities,
timestamps, content digests, bounded error categories, and observable health
states. They must not contain tokens, private keys, certificate private
material, registry credentials, database contents, model prompts/outputs,
passwords, environment dumps, or secret-bearing paths.

Sanitize before signing. Do not edit a signed artifact or replace a protected
detail record after its digest is approved. Preserve raw evidence in the
restricted store and publish only its content-addressed sanitized projection.

## Final verification

After the missing implementation lands and the six real drills pass, run:

```bash
sudo scripts/verify-platform-release \
  --candidate 0.1.0 \
  --physical-evidence-public-key \
    /srv/dgx-forge/trust/physical-acceptance-public.json \
  --json
```

Acceptance requires exit status `0`, `status: passed`, the expected physical
key ID, all three `physical_update_gates` set to `true`, and an empty
`missing_gates` array. Any other result blocks candidate deletion, tagging,
channel publication, and installation.
