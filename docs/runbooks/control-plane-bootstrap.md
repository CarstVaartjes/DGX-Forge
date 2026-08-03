# Bootstrap the control plane

The control plane runs on any Docker Compose-capable Linux machine. The first
host may be a NAS, but the configuration has no NAS vendor dependency.

1. Copy `deploy/compose/.env.example` to a host-local `.env` and replace every
   image placeholder with a verified digest-pinned reference.
2. Create the database URL, PostgreSQL password, and token-signing-key files
   outside Git. Restrict them to the service administrator.
3. Create the repository and state paths with `bin/dgx-control-offline init`.
4. Start PostgreSQL only, export the `DGX_*_FILE` settings, and run
   `bin/dgx-control-offline migrate`.
5. Run `bin/dgx-control-offline create-admin --subject ADMIN_ID` while the API
   and worker remain stopped.
6. Start the full Compose project and check `/api/v1/healthz` through Caddy.

The API and worker share one application image but remain separate services.
PostgreSQL and Caddy are independent containers. Only Caddy publishes a port.
LiteLLM, Prometheus, and Grafana are added as separate services in the later
inference/observability phase.
