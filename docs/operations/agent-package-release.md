# GPU node agent package release operations

The production GPU node service is distributed as one reproducible ARM64 Debian
package. A tag such as `v0.1.0` builds natively on Ubuntu 24.04 ARM64, runs the
Rust and Debian lifecycle gates, creates a keyless Sigstore attestation, uploads
the exact artifacts to the GitHub Release, and then publishes that same verified
`.deb` to `https://packages.vonkforge.ai`.

The workflow never builds community recipe containers. This package contains
only the Vonk agent, stable supervisor, and narrow host helper.

## Trust boundaries

Two independent signing identities are intentional:

- `VONK_AGENT_RELEASE_PRIVATE_KEY` is an Ed25519 key. It signs the agent slot
  manifests and the helper's artifact authorization. Its public key is embedded
  in the authenticated `.deb`.
- `APT_REPOSITORY_GPG_PRIVATE_KEY` signs apt `InRelease` metadata. The expected
  full fingerprint is the protected `APT_REPOSITORY_GPG_FINGERPRINT` variable.
- GitHub OIDC creates the keyless Cosign bundle and GitHub artifact attestation.
  It is short-lived and is not stored as a repository secret.

The `.deb` must be authenticated by apt, GitHub attestations, or its Sigstore
bundle before the embedded agent key is trusted. A SHA-256 sidecar alone detects
corruption but does not establish publisher identity.

## GitHub environments

Create two protected environments with required reviewers and prevent untrusted
branches from deploying to them.

The `agent-release` environment contains:

- secret `VONK_AGENT_RELEASE_PRIVATE_KEY`, an unencrypted PEM Ed25519 private
  key stored only in GitHub's protected environment; and
- no R2, apt, Railway, container-registry, or controller credentials.

The `apt-release` environment contains:

- secret `APT_REPOSITORY_GPG_PRIVATE_KEY`;
- secret `APT_GPG_PASSPHRASE`;
- secret `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY` for an
  R2 token limited to the two exact buckets;
- variable `APT_REPOSITORY_GPG_FINGERPRINT` with the full uppercase fingerprint;
- variable `R2_APT_PUBLIC_BUCKET` for public repository objects; and
- variable `R2_APT_STATE_BUCKET` for private, versioned aptly state.

Do not place these secrets in Railway, repository variables, Compose, or the
local controller. The release and apt jobs are separately permissioned, and the
apt job never receives `VONK_AGENT_RELEASE_PRIVATE_KEY`.

## R2 and DNS

Use separate buckets. Bind the public bucket to the custom domain
`packages.vonkforge.ai` in Cloudflare R2 and keep the state bucket private with
no custom domain. Configure the public bucket for static object delivery and
ensure these paths are reachable without redirects:

```text
/dists/stable/InRelease
/dists/stable/main/binary-arm64/Packages.xz
/pool/...
/vonk-forge-archive-keyring.gpg
```

The publication job restores a checksum-verified private aptly state, adds one
new immutable version, signs a new snapshot, uploads the public repository, and
only then advances `latest` state. Publication is serialized. If public upload
fails, rerunning the same tag reconstructs from the previous state and the R2
copy remains idempotent.

Cloudflare caching must revalidate or use a short TTL for `InRelease`, `Release`,
and `Packages*`. Immutable files below `pool/` can use a long cache lifetime.

## First key creation

Create both keys on an offline administrator workstation. For the agent key:

```bash
umask 077
openssl genpkey -algorithm ED25519 -out vonk-agent-release.pem
openssl pkey -in vonk-agent-release.pem -pubout > vonk-agent-release-public.pem
```

Create the apt signing key as a dedicated, expiring certification/signing key,
record its complete fingerprint out of band, and export only the private key to
the protected environment. Keep encrypted offline backups and a written
revocation procedure for both identities.

## Release procedure

1. Run the complete read-only CI on the intended commit.
2. Run `Rust GPU node agent release` manually from `main` with the intended version.
   Manual runs validate, build, test, and attest but do not publish.
3. Review the package verifier output, systemd exposure reports, lifecycle test,
   Sigstore identity, and physical GPU node acceptance evidence.
4. Create the exact annotated tag `v<major>.<minor>.<patch>` on that reviewed
   commit and push it. Never move or reuse a release tag.
5. Approve `agent-release`, then separately approve `apt-release` after the
   GitHub Release assets are visible.
6. Verify `InRelease` and installation from a disposable Ubuntu 24.04 ARM64 host.

The workflow refuses prerelease-shaped tags, non-ARM64 builds, downgrade tests
that succeed, non-reproducible packages, mismatched release fingerprints, and
packages whose maintainer scripts perform network access.

## Consumer installation

Download the archive key without piping data into a shell, compare the displayed
fingerprint with the independently published release fingerprint, then install
it as an apt keyring:

```bash
curl --fail --proto '=https' --tlsv1.3 \
  --output /tmp/vonk-forge-archive-keyring.gpg \
  https://packages.vonkforge.ai/vonk-forge-archive-keyring.gpg
gpg --show-keys --with-fingerprint /tmp/vonk-forge-archive-keyring.gpg
sudo install -o root -g root -m 0644 /tmp/vonk-forge-archive-keyring.gpg \
  /usr/share/keyrings/vonk-forge-archive-keyring.gpg
printf '%s\n' \
  'deb [arch=arm64 signed-by=/usr/share/keyrings/vonk-forge-archive-keyring.gpg] https://packages.vonkforge.ai stable main' \
  | sudo tee /etc/apt/sources.list.d/vonk-forge.list >/dev/null
sudo apt update
sudo apt install vonk-forge-agent
```

Installation is offline-safe after apt has cached the `.deb`: `postinst` creates
only local users, directories, A/B state, and systemd enablement. It does not
pair, download, or start a network client. Pairing is a separate explicit
controller operation. To test an offline reinstall, disconnect egress and run
`sudo apt install --reinstall /var/cache/apt/archives/vonk-forge-agent_*.deb`.

For a GitHub Release download, verify the keyless identity before installation:

```bash
cosign verify-blob \
  --bundle vonk-forge-agent_0.1.0_arm64.deb.sigstore.json \
  --certificate-identity-regexp '^https://github.com/.*/vonk-forge/.github/workflows/agent-release.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  vonk-forge-agent_0.1.0_arm64.deb
gh attestation verify vonk-forge-agent_0.1.0_arm64.deb --repo CarstVaartjes/vonk-forge
```

## Recovery and key rotation

Do not silently replace a compromised key.

For apt key rotation, dual-publish the old and new public keyrings, communicate
both fingerprints out of band, ship the new keyring through the still-trusted
old repository, then change the repository signer. Retain old signed metadata
until the migration window closes.

For agent key rotation, release a supervisor/helper `.deb` authenticated by the
still-trusted apt/Sigstore identity that contains an explicitly reviewed key
transition. Roll it out as a topology-aware canary before signing agent slots
with the new key. A suspected compromise freezes agent updates and apt
publication until incident review establishes which trust root remains valid.

The private aptly state is recoverable from `versions/<version>/`; select the
last state whose public `InRelease` is known good, copy it to `latest`, and rerun
publication under the protected `apt-release` environment. Never reconstruct an
index by scraping untrusted package files from the public bucket.
