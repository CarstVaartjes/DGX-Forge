# Bootstrap the control plane

The control plane runs on any Docker Compose-capable Linux machine. The first
host may be a NAS, but the configuration has no NAS vendor dependency.

Choose a stable LAN address for the Docker service host and set it as
`NAS_LAN_IP` (the variable name is retained for compatibility; the host need
not be a NAS). Create local-only DNS records
`enroll.dgx-forge.lan`, `agents.dgx-forge.lan`, and
`registry.dgx-forge.lan`, all resolving to that address. Set
`DGX_MANAGEMENT_CIDRS` to the actual canonical Spark management network(s), and
permit TCP 8443 to the service-host address only from those networks or the
reserved Spark leases. Do not expose LAN ports for control, inference, Grafana,
or Hermes.
Spark reservations are recommended, but no Spark IP belongs in Compose or fleet
identity: authenticated agent presence supplies the current validated address.

1. Copy `deploy/compose/.env.example` to a host-local `.env` and replace every
   image placeholder with a verified digest-pinned reference.
2. Create the database URL, PostgreSQL password, token-signing-key, Tailscale
   OAuth, and Hermes API-key files outside Git. Restrict them
   to the service administrator. Follow the [Tailscale](tailscale.md) and
   [Hermes Agent](hermes-agent.md) runbooks for their exact preparation.
   Generate the Caddy/control proxy-auth secret as an unpadded base64url token
   of at least 32 characters (an optional final CR/LF is accepted):

   ```bash
   umask 077
   openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' > /srv/dgx-forge/secrets/agent-proxy-auth
   openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' > /srv/dgx-forge/secrets/worker-api-token
   ```

   Spaces, internal line breaks, padding, and other punctuation are rejected
   by both Caddy and the control API.
3. Create the repository and state paths with `bin/dgx-control-offline init`.
4. Start PostgreSQL only, export the `DGX_*_FILE` settings, and run
   `bin/dgx-control-offline migrate`.
5. Run `bin/dgx-control-offline create-admin --subject ADMIN_ID` while the API
   and worker remain stopped.
6. Start the recommended production provider and check `/api/v1/healthz`
   through the `svc:dgx-forge` Tailscale Service:

   ```bash
   cd deploy/compose
   docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml up -d
   ```

   The base file deliberately has no CA provider selection, so it is not a
   runnable production configuration. For local bootstrap or development,
   use the built-in provider instead; it does not require any `STEP_CA_*` or
   `AGENT_CA_CREDENTIAL_FILE` values:

   ```bash
   cd deploy/compose
   docker compose --env-file .env -f compose.yaml -f compose.builtin-ca.yaml up -d
   ```

   Select exactly one provider overlay. Combining both overlays is rejected by
   the control API at startup regardless of their order.

The API and worker are separate targets built from the same release commit and
remain separate services. The API image contains Git/OpenSSH for signed
repository administration. The worker image contains neither Git nor OpenSSH,
mounts no repository or Git key, and has no Spark-facing network.
PostgreSQL, Caddy, LiteLLM, Prometheus, Grafana, Tailscale, and Hermes Agent are
independent containers in this one project. Only Caddy publishes a host port,
and that is the `10.0.0.2:8443` Spark backend. The Tailscale gateway publishes
no Docker port and advertises separate DGX Forge, Hermes dashboard, and Hermes
API Services.

Caddy receives tailnet web traffic on the private `tailnet-web-edge` network.
It sends `/v1/*` to `litellm:4000` on the existing internal `ingress` network.
LiteLLM then reaches only the accepted, fresh agent-derived Spark endpoint via
`cluster-egress`; Docker routes that connection out through the NAS LAN. Model
and tensor runtimes remain on the DGX Sparks, and direct-fabric traffic never
passes through the NAS.

Hermes reaches LiteLLM only through `hermes-inference` and uses the fixed
`hermes-agent` alias. Apply and verify `bin/harden-hermes-egress` after Docker
creates the bridge so terminal tools cannot connect directly to Spark
management/fabric networks or sibling control-plane networks.

The checked-in LiteLLM file is a fail-closed empty bootstrap. The API retains
Git authority and evaluates current-head, eligibility, and commit-pinned Hermes
policy for the repository-less worker over a dedicated internal two-party
network. Requests and short-lived responses are nonce-bound and HMAC-authenticated
with the independent `worker-api-token`; Caddy denies every `/internal/*` path.
After a successful commit-pinned reconciliation, the worker derives the live
config from stable
`spk_` identity, fresh authenticated presence, repository workload ports, and a
successful upstream probe. The worker writes only to the dedicated
`litellm-routes` volume; LiteLLM mounts it read-only and reloads by supervised
process restart. Each generated config has a hash-bound expiry lease. The
supervisor rejects leases from before its own startup and falls back to the
empty bootstrap when a lease expires, so a dead worker or restored route volume
cannot keep an upstream published indefinitely. The worker refreshes the
generation every 60 seconds, so a DHCP change follows the next authenticated
observation and stale presence withdraws the route within the 150-second
window.
