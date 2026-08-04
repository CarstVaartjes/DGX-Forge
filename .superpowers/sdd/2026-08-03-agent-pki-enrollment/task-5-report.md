# Task 5 report — production Smallstep provider, runtime wiring, and recovery

## Status

Implemented the production `StepCertificateAuthority`, corrected the provider
contract to preserve the node-signed CSR, wired the production agent services,
added durable local-first revocation reconciliation, and documented/tested PKI
bootstrap, rotation, backup, restore, recovery, and provider migration.

## Protocol and source basis

The implementation targets official `smallstep/certificates` tag `v0.30.2`.
The protocol assumptions were verified directly in:

- `api/sign.go`: `SignRequest` is `csr`, `ott`, optional `notBefore` and
  `notAfter`; `SignResponse` contains PEM strings `crt`, `ca`, and `certChain`;
  the server validates the CSR signature.
- `api/revoke.go`: revocation accepts decimal/`0x` serial, `ott`, reason,
  reason code, and requires `passive: true`; the response is `{"status":"ok"}`.
- `api/crl.go`: the PEM CRL is available at `/1.0/crl?pem=true` when enabled.
- `authority/provisioner/jwk.go`: the JWK provisioner validates issuer with a
  one-minute leeway, accepts the base sign/revoke audience, and validates token
  subject/SANs against the CSR.
- `authority/provisioner/provisioner.go`: audience fragments apply to other
  provisioners; the default JWK uses the base `/1.0/sign` and `/1.0/revoke`
  audiences.
- `authority/provisioner/sign_options.go` and `claims.go`: Smallstep's private
  extension is disabled by `disableSmallstepExtensions`.

PyJWT `2.13.0` performs ES256/JWK handling and httpx `0.28.1` performs the
bounded HTTPS exchange. Both are exact runtime pins in `control/pyproject.toml`
and `control/uv.lock`.

## Implementation

- `CertificateAuthority` now consumes the already validated signed CSR for
  issue and renewal and has a provider revocation method. The built-in issuer
  follows the same contract.
- The unreleased `0004_agent_enrollment` migration/model persists normalized
  CSR PEM alongside its public-key PEM and SHA-256 fingerprint. Enrollment and
  renewal require Ed25519, exact CN `spk_<32 lowercase hex>`, and exactly the
  corresponding node URI SAN.
- `StepCertificateAuthority` fixes the HTTPS origin, provisioner identity,
  root, intermediate, timeouts, and response cap at construction. It uses a
  regular non-symlink root and private JWK, disables ambient proxies and
  redirects, bounds decoded response bytes, and never includes authorization
  in errors.
- Private and public provisioner JWKs must be the same P-256 key. Both must use
  ES256 and the configured `kid`; the `kid` must equal the RFC 7638 SHA-256
  thumbprint. A copied `kid` on another public key is rejected before startup.
- Returned leaves are checked for the submitted public key, exact subject/SAN,
  client-auth-only EKU and digital-signature-only key usage, 24-hour lifetime,
  clock skew, configured issuer/signature, chain length/order, and only the
  required extensions plus validated SKID/AKID.
- Authorization uses a cryptographically random `jti`, a 60-second token,
  30-second bounded backdating, fixed issuer/audience/subject/SAN, and exact
  requested validity. Smallstep persists JWK token IDs, making the OTT one-use.
- Node revocation commits local retirement and every local certificate denial
  before contacting the CA. `ca_revoked_at` records each confirmed provider
  serial independently; retries send only unconfirmed serials. A typed
  `RemoteRevocationUncertain` maps to 503, invalid node IDs to 422, and unknown
  nodes to 404 without substring-based control flow.
- `build_agent_services` constructs the selected CA, enrollment service,
  agent-job service, and artifact root. Step CA mode performs a bounded health
  probe before serving. Disabled workers load none of the CA/agent secrets.
- The production overlay adds only control-api and step-ca to the internal CA
  network. It mounts the private provisioner JWK only into control-api and only
  generated public config/encrypted intermediate material into step-ca. The
  offline root private key is absent from Compose.

## Provisioner and operational procedure

`deploy/compose/step-ca/ca.json` is a public template, not a shared deployment
identity. `docs/runbooks/agent-pki.md` generates a per-install ES256 pair,
substitutes only the public JWK into a generated `STEP_CA_CONFIG_FILE`, fixes
certificate duration to 24 hours, disables direct step-ca renewal and the
Smallstep extension, and applies a client-auth-only template.

The runbook covers offline root custody, encrypted online intermediate setup,
host permissions, CA health, local-first revocation and remote uncertainty,
manual reconciliation of `issuing`, expiry/key-loss recovery with a fresh
node-bound enrollment grant, intermediate/root overlap, consistent CA DB plus
PostgreSQL backup/restore, and built-in-to-step-ca migration. It explicitly
forbids copying another Spark's identity.

## TDD evidence

### RED

Provider RED:

```text
uv run --project control pytest control/tests/test_step_ca.py -q
ModuleNotFoundError: No module named 'dgx_control.step_ca'
```

Runbook RED after removing an incidental unavailable test dependency:

```text
uv run pytest tests/runbooks/test_agent_pki.py -q
5 failed: agent-pki.md absent, README link absent, and required recovery behavior absent
```

The CSR contract and revocation retry tests were added before their production
changes. They initially failed because enrollment stored/passed only a public
key and because there was no durable CA-revocation confirmation.

### Final GREEN

Required focused control verification:

```text
uv run --project control pytest control/tests/test_pki.py control/tests/test_step_ca.py \
  control/tests/test_enrollment.py control/tests/test_agent_api.py \
  control/tests/security/test_agent_identity.py -q
105 passed in 7.53s
```

Required ingress/runbook verification:

```text
uv run pytest deploy/compose/tests/test_agent_ingress.py tests/runbooks/test_agent_pki.py -q
14 passed (earlier combined required run); runbook alone: 6 passed in 0.10s
```

Full suites after final changes:

```text
uv run --project control pytest control/tests -q
258 passed in 24.28s

uv run pytest deploy/compose/tests -q
17 passed in 6.70s
```

Both required `docker compose ... config --quiet` commands (step-ca and
built-in overlays) exited 0. `git diff --check` exited 0.

## Self-review dispositions

- Confirmed the root private key is absent from every Compose service and the
  private provisioner JWK is absent from step-ca.
- Confirmed normal Step CA and explicit built-in render independently; provider
  mixing still fails closed in settings.
- Confirmed GET `/health` has no request body, no redirects are followed,
  environment proxies are disabled, and response accumulation is bounded.
- Confirmed CA root self-signature/constraints/current validity, intermediate
  signature/path length/current validity, and enough remaining validity for a
  24-hour leaf.
- Confirmed optional AKID/SKID values are validated rather than merely allowed.
- Confirmed invalid node paths cannot become a 500 and remote uncertainty uses
  a typed exception.
- Confirmed public/private JWK coordinates and RFC 7638 `kid` are checked; a
  same-`kid`, different-key regression test fails closed.
- Confirmed disposable runbook tests execute public-JWK config generation with
  `jq`, syntax-check every shell block, render the real Compose overlay, and
  inspect secret/network/port separation.

## Remaining operational concern

Open-source step-ca v0.30.2 revocation is passive. DGX-Forge's database gate is
therefore the immediate revocation boundary, with the residual leaf exposure
bounded to 24 hours if that gate is unavailable. A lost success response from
step-ca can remain remotely uncertain because a repeated passive revoke may be
reported as already revoked; the documented procedure preserves local denial
and requires CA DB/audit reconciliation rather than guessing.
