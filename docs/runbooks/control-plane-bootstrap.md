# Bootstrap the control plane

The control plane runs on any Docker Compose-capable Linux machine. The first
host may be a NAS, but the configuration has no NAS vendor dependency.

1. Copy `deploy/compose/.env.example` to a host-local `.env` and replace every
   image placeholder with a verified digest-pinned reference.
2. Create the database URL, PostgreSQL password, and token-signing-key files
   outside Git. Restrict them to the service administrator.
   Generate the Caddy/control proxy-auth secret as an unpadded base64url token
   of at least 32 characters (an optional final CR/LF is accepted):

   ```bash
   umask 077
   openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' > /srv/dgx-forge/secrets/agent-proxy-auth
   ```

   Spaces, internal line breaks, padding, and other punctuation are rejected
   by both Caddy and the control API.
3. Create the repository and state paths with `bin/dgx-control-offline init`.
4. Start PostgreSQL only, export the `DGX_*_FILE` settings, and run
   `bin/dgx-control-offline migrate`.
5. Run `bin/dgx-control-offline create-admin --subject ADMIN_ID` while the API
   and worker remain stopped.
6. Start the recommended production provider and check `/api/v1/healthz`
   through Caddy:

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

The API and worker share one application image but remain separate services.
PostgreSQL and Caddy are independent containers. Only Caddy publishes a port.
LiteLLM, Prometheus, and Grafana are added as separate services in the later
inference/observability phase.
