# Task 4 report — Caddy mTLS boundary and Compose secrets

## Status

Complete. The accompanying implementation commit is
`feat: authenticate outbound agents through Caddy`.

## Files and behavior

- `deploy/compose/Caddyfile` defines three environment-configured SNI hosts on
  the single published HTTPS listener. The ordinary control host returns 404
  for every `/agent/v1/*` path; the enrollment host exposes only
  `/agent/v1/enroll`; and the agent host requires `require_and_verify` client
  authentication. The rendered Caddy JSON test verifies that the ordinary
  deny handler is ordered before its control-api fallback.
- The agent host strips every `X-DGX-Agent-*` request header before setting the
  TLS-derived node, serial, fingerprint, and verified headers. A strict Caddy
  `map` accepts only a subject exactly equal to `CN=spk_<32 lowercase hex>`.
- `control/src/dgx_control/auth.py` replaces address/name based proxy trust
  with a default-empty proxy-auth secret. It constant-time compares that secret
  before creating the typed identity and strips all forwarded agent headers
  before application code sees the request.
- `control/src/dgx_control/settings.py` requires regular secret files in
  production for the client CA, intermediate certificate, CA credential, and
  proxy authentication secret. Production requires `DGX_AGENT_CA_PROVIDER` to
  be `step-ca`.
- `deploy/compose/compose.yaml` adds the private `agent-proxy` network shared
  only by Caddy and control-api, mounts the high-entropy proxy secret only into
  those services, and has no Docker IP/CIDR/name trust decision. It adds a
  digest-pinned, unexposed `step-ca` service on a separate internal CA network,
  mounts only root/intermediate certificates, encrypted intermediate key,
  password, read-only config, and persistent CA DB.
- `deploy/compose/compose.builtin-ca.yaml` is the explicit development-only
  override that adds the built-in intermediate key to control-api. The default
  production rendering never mounts that key.
- `deploy/compose/step-ca/ca.json` is tracked and contains no secret. Offline
  root/intermediate/provisioner initialization remains an explicit deployment
  operation; no root private key appears in Compose.
- `control/src/dgx_control/agent_api.py` adds a fixed global 20-per-60-second
  enrollment admission limiter. It has injected monotonic-clock support and
  rejects with 429 before reading a request body, without per-client state.

## TDD evidence

### RED

Initial boundary/settings RED command:

```sh
uv run pytest deploy/compose/tests/test_agent_ingress.py control/tests/test_settings.py -v
```

Expected and observed output (exit 1): 5 failed, 5 passed. Failures were the
absent three-SNI/mTLS listener, absent private agent-proxy/step-ca/explicit
built-in rendering, and missing agent settings fields.

The required application limiter was introduced test-first as well:

```sh
uv run --project control pytest tests/test_agent_api.py::test_enrollment_rate_limit_rejects_before_reading_request_body -v
```

Expected and observed RED output (exit 2): collection failed with
`ImportError: cannot import name 'EnrollmentRateLimiter'`.

An additional rendered-Caddy ordering test correctly failed while the ordinary
host's generic `handle` route preceded its agent deny route (`assert 4 < 0`).
The configuration was then changed to mutually-exclusive `handle` blocks.

### GREEN and regressions

```sh
uv run pytest deploy/compose/tests/test_agent_ingress.py deploy/compose/tests/test_networking.py deploy/compose/tests/test_observability.py -v \
  && docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml config --quiet \
  && docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml -f deploy/compose/compose.builtin-ca.yaml config --quiet
```

Output: `10 passed in 0.97s`; both default and explicit built-in Compose
renderings exited 0.

```sh
uv run --project control pytest -q
```

Output: `215 passed in 26.28s`.

## Caddy and Compose validation evidence

The rendered-config test runs the pinned Caddy image's `caddy adapt` and
asserts the three hosts, mTLS client-auth mode, TLS identity placeholders,
proxy secret forwarding, and deny-before-fallback route ordering.

An explicit container validation also passed with a temporary valid test CA:

```sh
docker run --rm ... caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Output included `Valid configuration` (exit 0). Caddy also reported strict
SNI/Host enforcement because client authentication is configured.

## step-ca image digest provenance

The multi-platform OCI manifest-list digest was queried directly from Docker
Hub rather than guessed:

```sh
TOKEN=$(curl -fsSL 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:smallstep/step-ca:pull' | jq -r .token)
curl -fsSI -H "Authorization: Bearer $TOKEN" -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://registry-1.docker.io/v2/smallstep/step-ca/manifests/0.30.2 | tr -d '\r' | rg -i 'docker-content-digest|content-type'
```

Output:

```text
content-type: application/vnd.oci.image.index.v1+json
docker-content-digest: sha256:a2b17872915c193259b75a5474c398326f41bd199f0842093e52cf4182bc8270
```

## Self-review

- Confirmed Caddy routing order in adapted JSON, not only source text.
- Confirmed all client-supplied agent metadata is removed in Caddy and again
  at the ASGI boundary.
- Confirmed proxy authentication uses `hmac.compare_digest`, defaults empty,
  and does not trust an address, CIDR, or hostname.
- Confirmed only Caddy publishes a port; `step-ca` publishes none; default
  control-api excludes the built-in intermediate key; and control-worker does
  not receive agent-only secrets.
- Ran `git diff --check` before commit.

## Concerns and deferred items

- Task 5 must implement `StepCertificateAuthority` and the operational CA
  provisioning/recovery workflow. This task deliberately does not implement
  that provider.
- The enrollment limiter is process-local by design to avoid attacker-keyed
  memory. If control-api is horizontally scaled later, Task 5/deployment work
  must replace it with a bounded shared limiter while retaining the
  pre-body-read property.

## Review round 1 fixes

- Built-in issuer serial metadata is now persisted as the decimal X.509 serial
  representation forwarded by Caddy 2.10.2, rather than lowercase hex. A real
  certificate issued by `BuiltinCertificateAuthority` is passed through the
  proxy middleware using Caddy's decimal serial and SHA-256 fingerprint; its
  validator receives exactly the persisted values and raw headers remain
  stripped.
- Production still fails closed when `DGX_AGENT_CA_PROVIDER` is absent. It
  accepts `step-ca`, or `builtin` only with the explicit
  `DGX_AGENT_BUILTIN_CA_BOOTSTRAP=1` guard. The built-in setting retains only
  a regular non-symlink `agent_intermediate_key_path`; it never reads the key
  into `Settings`. The Compose override supplies both the guard and key mount.
  Control-worker explicitly declares `step-ca` with
  `DGX_AGENT_RUNTIME=disabled`, so it satisfies the production provider guard
  without receiving agent secrets.
- Control-api now joins the internal `ca` network; step-ca remains unexposed.
- Adapted Caddy JSON tests now associate `require_and_verify` specifically
  with agent SNI, prove enrollment SNI exposes only `/agent/v1/enroll`, prove
  ordinary `/agent/v1/*` denial precedes the fallback, and check exact request
  header deletion/replacement placeholders.
- A middleware regression sends all identity headers from an arbitrary network
  peer with a wrong proxy secret and proves it cannot populate ASGI identity
  scope. This intentionally preserves the no-IP/CIDR/hostname trust design:
  Caddy/control-api are the only members of the internal `agent-proxy` network,
  and the constant-time high-entropy secret is mounted only into those two
  services.
- Task 5's plan now explicitly owns production `AgentApiServices` creation,
  selected CA-provider wiring, step-ca provisioner/configuration and
  authenticated issuance, CA network reachability, and host-side secret
  permission/init instructions. It still does not instantiate the future
  `StepCertificateAuthority` in Task 4.

### Review RED

```sh
uv run --project control pytest tests/test_pki.py::test_issued_certificate_is_short_lived_and_node_bound tests/test_pki.py::test_caddy_serial_and_fingerprint_of_a_real_issued_certificate_reach_the_proxy_validator tests/test_settings.py::test_production_builtin_bootstrap_requires_and_loads_the_mounted_intermediate_key -v
```

Observed: 3 failed. The issuer returned hexadecimal serial metadata while the
real certificate/Caddy path used decimal; the middleware rejected that real
identity with 401; and production rejected the builtin override before its
guard/key could be validated.

```sh
uv run --project control pytest tests/test_settings.py::test_production_worker_settings_can_explicitly_disable_agent_runtime -v
```

Observed: 1 failed because a provider-less production worker was allowed by an
intermediate draft. The final design instead requires `step-ca` plus an
explicit disabled runtime role.

### Review GREEN / final verification

```sh
uv run --project control pytest -q
```

Output: `223 passed in 25.82s`.

```sh
uv run pytest deploy/compose/tests/test_agent_ingress.py deploy/compose/tests/test_networking.py deploy/compose/tests/test_observability.py -v \
  && docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml config --quiet \
  && docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml -f deploy/compose/compose.builtin-ca.yaml config --quiet
```

Output: `10 passed in 0.99s`; both Compose renderings exited 0.

The pinned Caddy container validation was re-run with a temporary valid CA and
reported `Valid configuration`; `git diff --check` exited 0.
