# Verify the platform supply chain

Standard service images are fixed by version and OCI index digest in
`deploy/compose/images.lock.json`; Compose uses those exact references as its
defaults. The custom control image is a release artifact and must be supplied
through `CONTROL_IMAGE` with its registry digest. Its Node and Python build
bases are separately digest-pinned in the lock.

Run the offline gate before building or deploying:

```bash
scripts/verify-supply-chain --json
```

The verifier checks image defaults, both dependency lockfiles, deterministic
SPDX 2.3 documents, Dockerfile/Compose inputs, the LiteLLM cosign public key,
and the content-addressed evidence manifest. Normal verification performs no
network access. Regeneration is an explicit reviewed operation:

```bash
scripts/verify-supply-chain --generate --json
```

At image publication, build with the locked `NODE_IMAGE` and `PYTHON_IMAGE`,
publish the immutable control image, record its registry digest as
`CONTROL_IMAGE`, generate an image/filesystem SBOM with Syft, scan it with the
release-approved scanner, and sign it with Cosign. LiteLLM signatures use the
checked-in key copied from immutable upstream commit
`0112e53046018d726492c814b3644b7d376029d0`; verify the locked digest, never a
mutable tag. Store scan/signature attestations with the release evidence.
