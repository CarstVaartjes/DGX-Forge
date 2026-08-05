# NAS pull-only deployment

This is the authoritative operator entry point for a production NAS deployment.
It deploys released `linux/amd64` images only: the NAS pulls a reviewed,
digest-pinned three-image set and never builds or publishes application images.

## Current release state

No images are currently being published. Repository variable
`DGX_CONTAINER_RELEASES_ENABLED` is deliberately unset (default-off) until the
entire repository is release-ready. A maintainer setting it to `true` is a
deliberate future enablement action; it is not part of NAS setup. Dependabot cannot publish: it only opens weekly dependency-update pull requests, which a
maintainer must review, merge, and deliberately release with a stable version
tag after enablement.

When enabled in the future, GitHub Actions publishes only stable version tags
to these three packages:

```text
ghcr.io/carstvaartjes/dgx-forge-api
ghcr.io/carstvaartjes/dgx-forge-worker
ghcr.io/carstvaartjes/dgx-forge-hermes
```

Do not deploy an individual package or a workflow summary. Deploy only a
complete public GitHub Release containing both `dgx-forge-images.env` and
`dgx-forge-images.env.sha256`.

## Host and network prerequisites

Use a supported `linux/amd64` NAS with Docker Engine plus the Docker Compose
plugin, `curl`, `sha256sum`, local DNS, and persistent storage. Keep the
repository checkout and host-local configuration outside Git, for example:

```bash
sudo install -d -m 0750 -o "$USER" -g "$USER" /srv/dgx-forge
git clone https://github.com/CarstVaartjes/DGX-Forge.git /srv/dgx-forge/repository
sudo install -d -m 0700 /srv/dgx-forge/secrets /srv/dgx-forge/hermes /srv/dgx-forge/step-ca
cd /srv/dgx-forge/repository/deploy/compose
cp .env.example .env
chmod 0600 .env
```

The deployment operator owns the checkout, so the unprivileged `cp`, later
reviewed `git` updates, and edits to `.env` work. Do not use `sudo git clone`
and then try to edit its root-owned result. The secrets, Hermes data, and
step-ca directories stay administrator-owned and are prepared with the
consumer-specific ownership below.

Reserve a host management-LAN address and put it in the host-local `.env`:

```dotenv
NAS_LAN_IP=10.0.0.2
```

`NAS_LAN_IP` is the NAS host's physical management-LAN address: it is not the Docker bridge, not a Tailscale `100.x` address, and not the public WAN address. Resolve these names only on the management LAN:

```text
enroll.dgx-forge.lan   10.0.0.2
agents.dgx-forge.lan   10.0.0.2
registry.dgx-forge.lan 10.0.0.2
```

Allow TCP 8443 to that LAN address only from the canonical Spark management
CIDRs (preferably reserved Spark leases). Human control, Grafana, inference,
and Hermes have no LAN or WAN access: use the exact Tailscale Services in
[the Tailscale runbook](../../docs/runbooks/tailscale.md). There is no LAN
fallback for tailnet-only access.

`control.dgx-forge.lan is not a LAN-accessible human endpoint`: do not create a
general-purpose LAN record or firewall rule for it. Human control reaches the
Tailscale `svc:dgx-forge` Service; the only published NAS LAN listener is the
Spark-restricted TCP 8443 backend.

## Get a complete release image set

After the future release path is enabled and a public version release exists,
download its two assets. Substitute the exact immutable release tag, not a
branch or a floating label:

```bash
release_tag=vX.Y.Z
release_url="https://github.com/CarstVaartjes/DGX-Forge/releases/download/${release_tag}"
curl --fail --location --remote-name "$release_url/dgx-forge-images.env"
curl --fail --location --remote-name "$release_url/dgx-forge-images.env.sha256"
sha256sum -c dgx-forge-images.env.sha256
```

The checksum file must report `dgx-forge-images.env: OK`. Inspect the asset and
copy all three assignments, together, into `/srv/dgx-forge/repository/deploy/compose/.env`:

```bash
grep -E '^(CONTROL_API_IMAGE|CONTROL_WORKER_IMAGE|HERMES_AGENT_IMAGE)=' \
  dgx-forge-images.env
```

This image-only fragment does not replace the host's site-specific `.env`.
Keep the NAS paths, hostnames, network CIDRs, and paths to local secret files
in `.env`; replace only the complete three-reference release set. A public
package needs no NAS GitHub token. For each newly created package, a maintainer
performs the one-time GitHub web setting: package page → **Package settings** →
**Danger Zone** → **Change visibility** → **Set package visibility to Public**.
Never put a GitHub token in `.env` to work around package visibility.

## Host-local `.env` inputs

All values below are host-local configuration. `.env` contains paths and
non-secret configuration only; **no secret value belongs in `.env`**.

### Images

Set the three release values from the verified release asset, each including a
version tag and `@sha256:` digest:

```dotenv
CONTROL_API_IMAGE=ghcr.io/carstvaartjes/dgx-forge-api:X.Y.Z@sha256:REPLACE
CONTROL_WORKER_IMAGE=ghcr.io/carstvaartjes/dgx-forge-worker:X.Y.Z@sha256:REPLACE
HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/dgx-forge-hermes:X.Y.Z@sha256:REPLACE
```

Keep the checked-in upstream image pins (`POSTGRES_IMAGE`, `CADDY_IMAGE`,
`REGISTRY_IMAGE`, `LITELLM_IMAGE`, `PROMETHEUS_IMAGE`, `GRAFANA_IMAGE`,
`STEP_CA_IMAGE`, and `TAILSCALE_IMAGE`) version-and-digest pinned.

### NAS paths and networking

Set `COMPOSE_PROJECT_NAME`, `REPOSITORY_PATH`, `HERMES_DATA_ROOT`,
`NAS_LAN_IP`, `DGX_BACKEND_PORT`, `DGX_MANAGEMENT_CIDRS`, and optional
`DGX_DIRECT_FABRIC_CIDRS`. `REPOSITORY_PATH` is the host checkout mounted into
the API; `HERMES_DATA_ROOT` contains `data`, `workspaces`, and `cache`.

### Hostnames

Set `DGX_CONTROL_HOSTNAME`, `DGX_AGENT_ENROLL_HOSTNAME`,
`DGX_AGENT_HOSTNAME`, and `DGX_REGISTRY_HOSTNAME` to the names served by Caddy.
For the management-LAN example they are `control.dgx-forge.lan`,
`enroll.dgx-forge.lan`, `agents.dgx-forge.lan`, and
`registry.dgx-forge.lan` respectively. Set `HERMES_DASHBOARD_ORIGIN` to the one
exact `svc:hermes-dashboard` HTTPS origin supplied by Tailscale.

### PKI

For the production `compose.step-ca.yaml` overlay set
`AGENT_CLIENT_CA_FILE`, `AGENT_INTERMEDIATE_CERTIFICATE_FILE`,
`AGENT_PROXY_AUTH_FILE`, `AGENT_CA_CREDENTIAL_FILE`,
`AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE`, `AGENT_CA_PROVISIONER_NAME`,
`AGENT_CA_PROVISIONER_KID`, `STEP_CA_CONFIG_FILE`,
`STEP_CA_ROOT_CERTIFICATE_FILE`, `STEP_CA_INTERMEDIATE_KEY_FILE`, and
`STEP_CA_PASSWORD_FILE`. `AGENT_INTERMEDIATE_KEY_FILE` is development-only for
the mutually exclusive built-in CA overlay.

### Required secret-file paths

Set every required secret path in `.env`; these are paths only, never values:
`DATABASE_URL_FILE`, `POSTGRES_PASSWORD_FILE`, `TOKEN_SIGNING_KEY_FILE`,
`METRICS_TOKEN_FILE`, `GIT_SIGNING_KEY_FILE`, `WORKER_API_TOKEN_FILE`,
`LITELLM_MASTER_KEY_FILE`, `LITELLM_UPSTREAM_KEY_FILE`,
`LITELLM_DATABASE_URL_FILE`, `GRAFANA_ADMIN_PASSWORD_FILE`,
`AGENT_CLIENT_CA_FILE`, `AGENT_INTERMEDIATE_CERTIFICATE_FILE`,
`AGENT_PROXY_AUTH_FILE`, `AGENT_CA_CREDENTIAL_FILE`,
`AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE`, `STEP_CA_ROOT_CERTIFICATE_FILE`,
`STEP_CA_INTERMEDIATE_KEY_FILE`, `STEP_CA_PASSWORD_FILE`,
`TAILSCALE_OAUTH_CLIENT_ID_FILE`, `TAILSCALE_OAUTH_CLIENT_SECRET_FILE`, and
`HERMES_API_KEY_FILE`. The development-only built-in overlay additionally needs
`AGENT_INTERMEDIATE_KEY_FILE`.

### Tailscale and Hermes

Set `TAILSCALE_OAUTH_CLIENT_ID_FILE` and
`TAILSCALE_OAUTH_CLIENT_SECRET_FILE` to local OAuth secret files. For the
official published Hermes wrapper, `HERMES_UID=1100` and `HERMES_GID=1100` are
fixed image requirements, not tunable runtime choices. Set
`HERMES_API_KEY_FILE` to the local key file. Optional resource limits
`HERMES_CPUS`, `HERMES_MEMORY_LIMIT`, and `HERMES_MEMORY_RESERVATION` retain
their Compose defaults unless the host is deliberately sized differently.

## Secret files

Create regular files under `/srv/dgx-forge/secrets` and parent directories mode
`0700`. Compose bind-backs each secret file, so host ownership and mode must
allow the **actual consuming container UID** to read it. Do not use a blanket
`root:root 0600` rule: it prevents the explicitly non-root API, worker,
LiteLLM, Prometheus, and Grafana services from reading their secret mounts.

The Compose service users are `10001:10001` for control-api and control-worker,
`10002:10001` for LiteLLM, `65534:65534` for Prometheus, and `472:472` for
Grafana. The pinned step-ca image runs as `1000:1000` (`step`); Hermes' managed
process is fixed at `1100:1100`. PostgreSQL, Caddy, Tailscale, and the Hermes
entrypoint use their image startup identity and can read a `root:root 0400`
secret before dropping privileges. Re-check these identities after changing an
image pin with `docker image inspect IMAGE --format '{{.Config.User}}'` and,
for the pinned step-ca image, `docker run --rm --entrypoint id STEP_CA_IMAGE`.

For a secret with one non-root consumer, use its exact owner and mode `0400`.
For a secret shared by different UIDs, retain `root:root 0400` and grant only
the listed service UIDs read access with POSIX ACLs. For example, the metrics
token is read by both control-api (`10001`) and Prometheus (`65534`):

```bash
sudo chown root:root /srv/dgx-forge/secrets/metrics-token
sudo chmod 0400 /srv/dgx-forge/secrets/metrics-token
sudo setfacl -m u:10001:r,u:65534:r /srv/dgx-forge/secrets/metrics-token
sudo getfacl /srv/dgx-forge/secrets/metrics-token
```

Use one value per file, with a final newline only where the consumer format
permits it; never export a secret into `.env`, shell history, or a Compose
command line.

| `.env` path key → file | Consumer UID(s), host ownership/mode | Required content |
| --- | --- | --- |
| `DATABASE_URL_FILE` → `database-url` | `10001:10001`, `10001:10001 0400` | PostgreSQL URL. |
| `POSTGRES_PASSWORD_FILE` → `postgres-password` | PostgreSQL startup, `root:root 0400` | One PostgreSQL password. |
| `TOKEN_SIGNING_KEY_FILE` → `token-signing-key` | API `10001:10001`, `10001:10001 0400` | At least 32 bytes. |
| `METRICS_TOKEN_FILE` → `metrics-token` | API `10001`, Prometheus `65534`; `root:root 0400` plus ACLs | At least 16 non-whitespace characters. |
| `GIT_SIGNING_KEY_FILE` → `git-signing-key` | API `10001:10001`, `10001:10001 0400` | Private SSH signing key. |
| `WORKER_API_TOKEN_FILE` → `worker-api-token` | API/worker `10001:10001`, `10001:10001 0400` | One unpadded base64url token, at least 32 characters. |
| `LITELLM_MASTER_KEY_FILE`, `LITELLM_UPSTREAM_KEY_FILE`, `LITELLM_DATABASE_URL_FILE` → matching `litellm-*` files | LiteLLM `10002:10001`, `10002:10001 0400` | Respectively the master key, dedicated upstream key, and PostgreSQL URL. |
| `GRAFANA_ADMIN_PASSWORD_FILE` → `grafana-admin-password` | Grafana `472:472`, `472:472 0400` | One Grafana administrator password. |
| `AGENT_CLIENT_CA_FILE` → `agent-client-ca` | API `10001` and Caddy startup; `root:root 0400` plus ACL `u:10001:r` | PEM trust bundle. |
| `AGENT_INTERMEDIATE_CERTIFICATE_FILE` → `agent-intermediate-certificate` | API `10001`, step-ca `1000`; `root:root 0400` plus `u:10001:r,u:1000:r` ACLs | PEM intermediate certificate. |
| `AGENT_PROXY_AUTH_FILE` → `agent-proxy-auth` | API `10001` and Caddy startup; `root:root 0400` plus ACL `u:10001:r` | One unpadded base64url token, at least 32 characters. |
| `AGENT_CA_CREDENTIAL_FILE` → `agent-ca-credential` | API `10001:10001`, `10001:10001 0400` | Private provisioner credential. |
| `AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE` → `agent-ca-public.jwk` | API `10001`, step-ca `1000`; `root:root 0400` plus `u:10001:r,u:1000:r` ACLs | Public provisioner JWK. |
| `STEP_CA_ROOT_CERTIFICATE_FILE` → `step-ca-root-certificate` | API `10001`, step-ca `1000`; `root:root 0400` plus `u:10001:r,u:1000:r` ACLs | PEM root certificate. |
| `STEP_CA_INTERMEDIATE_KEY_FILE`, `STEP_CA_PASSWORD_FILE` → `step-ca-intermediate-key`, `step-ca-password` | step-ca `1000:1000`, `1000:1000 0400` | Encrypted intermediate private key and its one password. |
| Development-only `AGENT_INTERMEDIATE_KEY_FILE` → `agent-intermediate-key` | API `10001:10001`, `10001:10001 0400` | Built-in CA intermediate private key; never combine this overlay with step-ca. |
| `TAILSCALE_OAUTH_CLIENT_ID_FILE`, `TAILSCALE_OAUTH_CLIENT_SECRET_FILE` → matching `tailscale-oauth-*` files | Tailscale startup, `root:root 0400` | One OAuth client ID or secret; neither is a GitHub credential. |
| `HERMES_API_KEY_FILE` → `hermes-api-key` | Hermes entrypoint then managed `1100:1100`; `root:root 0400` | One 32+ character key using only `A-Z`, `a-z`, `0-9`, `_`, `.`, `~`, or `-`. |

The offline root private key never enters this NAS. The generated PKI paths and
permissions in [agent PKI](../../docs/runbooks/agent-pki.md) remain authoritative
for the step-ca material.

## Bootstrap the production step-ca overlay

Follow [agent PKI](../../docs/runbooks/agent-pki.md) first to create the
offline root, online intermediate, provisioner material, generated
`/srv/dgx-forge/step-ca/ca.json`, and all PKI secret files. Then follow
[Tailscale](../../docs/runbooks/tailscale.md) to create the scoped OAuth client,
tailnet policy, and exact Services. Do this in order:

1. Prepare the host paths, local DNS, all `.env` entries, and secret files.
2. Complete the agent-PKI production step-ca material and copy only its online
   artifacts to the paths named in `.env`.
3. Complete Tailscale policy and OAuth secret setup; do not enable a LAN
   fallback.
4. Copy the verified complete three-image release set into `.env`.
5. Pull and render the complete production plan, then initialize PostgreSQL,
   migrate, and create the first administrator while API and worker are stopped.
6. Start the complete project with the one production overlay.

The base file deliberately selects no CA provider. Select exactly one overlay:
`compose.step-ca.yaml` for production, or the built-in CA overlay for local
development—never both.

## Preflight and first start

Run the production commands exactly from the Compose directory:

```bash
cd /srv/dgx-forge/repository/deploy/compose
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml pull
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml up -d postgres
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml run --rm --no-deps \
  --entrypoint python control-api -m dgx_control.offline --state-path /state \
  init --repository /repository
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml run --rm --no-deps \
  --entrypoint python control-api -m dgx_control.offline --state-path /state migrate
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml run --rm --no-deps \
  --entrypoint python control-api -m dgx_control.offline --state-path /state \
  create-admin --subject ADMIN_ID
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml up -d
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml ps
```

In short, the required preflight sequence is `docker compose pull`, then
`docker compose config --quiet`, then PostgreSQL-only `up -d postgres`, offline
migration and administrator creation, before the first full `up -d`. The
one-shot commands use the production control-api image so it reads the same
Compose secrets as the service; API and worker remain stopped. After Docker
creates its bridge, apply and verify the Hermes host-egress rule as
documented in [Hermes Agent](../../docs/runbooks/hermes-agent.md). Check the
Tailscale Service status and verify that ordinary LAN clients cannot reach human
or Hermes endpoints.

## Upgrade and rollback

For an upgrade, stop treating image names as independent settings. Download and
verify the next release assets, review all three digest references, replace all
three values in `.env` as one release set, then pull, render, and recreate:

```bash
cd /srv/dgx-forge/repository/deploy/compose
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml pull
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml up -d
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml ps
```

Retain the previous verified `dgx-forge-images.env` and checksum with the
deployment record. To roll back, restore the previous complete three-reference
set—not just one image—into `.env`, then repeat pull, `config --quiet`, and
`up -d`. Do not deploy a partial publication, a digest copied from a registry
page, or a release without both assets.

## Evaluation-only `latest`

For a disposable, explicitly non-production evaluation only, these public
aliases may be selected:

```text
ghcr.io/carstvaartjes/dgx-forge-api:latest
ghcr.io/carstvaartjes/dgx-forge-worker:latest
ghcr.io/carstvaartjes/dgx-forge-hermes:latest
```

`latest is evaluation-only`. Production must not use these aliases; it requires
the version-and-digest references from one complete release asset. Docker does
not continuously update running containers: changing `latest` remotely has no
effect until an operator explicitly pulls and recreates containers. Evaluation
users must still deliberately pull and recreate, and must not mistake that for
a production update path.
