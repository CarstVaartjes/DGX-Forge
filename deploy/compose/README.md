# Source-first control-host deployment

This directory is the operator entry point for running Vonk Forge on a
Docker-capable `linux/amd64` service host. A NAS is convenient but not required.
The initial deployment is source-first: the operator checks out a reviewed
commit, builds the Vonk Forge images locally from that commit, and runs the
checked-in Compose graph. It does not require a platform image release, a
global catalog, Railway, or a hosted recipe registry.

Local PostgreSQL is authoritative for recipe families, authored or imported
recipe revisions, source bundles, installations, cluster mappings, and runs.
Recipe build output and model payloads move directly between approved sources
and GPU nodes; the service host does not become a workload-image distribution
service. The separate signed agent-package and future platform-release paths
remain release boundaries, not prerequisites for this local Compose install.

## Host prerequisites

Install Docker Engine with the Compose plugin, Git, POSIX ACL tools (`setfacl`
and `getfacl`), and local DNS. Reserve a management-LAN address for the service
host. `NAS_LAN_IP=10.0.0.2` means the host's physical LAN address: it is
not the Docker bridge, not a Tailscale `100.x` address, and not the public WAN address.

Prepare one source checkout plus durable data, identity, and secret roots:

```bash
operator_user=$(id -un)
operator_group=$(id -gn)
sudo install -d -m 0755 -o root -g root /srv/vonk-forge
sudo install -d -m 0750 -o "$operator_user" -g "$operator_group" /srv/vonk-forge/source
sudo install -d -m 0755 -o root -g root /srv/vonk-forge/control-identity
sudo install -d -m 0700 -o root -g root /srv/vonk-forge/secrets /srv/vonk-forge/step-ca
git clone https://github.com/CarstVaartjes/vonk-forge.git /srv/vonk-forge/source
cd /srv/vonk-forge/source
git switch --detach REPLACE_WITH_REVIEWED_COMMIT
```

The API mounts the checkout as its platform-policy repository; CONTROL_API writes `.git`
when an administrator accepts a signed repository change. Keep
the operator and container UID `10001` able to update it without making secrets
or host identity writable:

```bash
sudo setfacl -R -m u:"$operator_user":rwX,u:10001:rwX,m::rwX /srv/vonk-forge/source
sudo find /srv/vonk-forge/source -type d -exec setfacl -m \
  u:"$operator_user":rwx,u:10001:rwx,m::rwx,d:u:"$operator_user":rwx,d:u:10001:rwx,d:m::rwx {} +
```

## Build the owned images from the reviewed source

Use canonical local tags so the same Compose variables work with a future
digest-pinned release without changing service names:

```bash
cd /srv/vonk-forge/source
docker build --target api \
  -t ghcr.io/carstvaartjes/vonk-forge-api:local \
  -f control/Dockerfile .
docker build --target worker \
  -t ghcr.io/carstvaartjes/vonk-forge-worker:local \
  -f control/Dockerfile .
docker build \
  -t ghcr.io/carstvaartjes/vonk-forge-hermes:local \
  -f deploy/compose/hermes-agent/Dockerfile deploy/compose/hermes-agent
```

The first two builds are required. Hermes is optional and disabled by default;
building its wrapper is needed only when the `hermes` profile will be enabled.
Do not add `build:` directives or Docker-socket mounts to the production
Compose graph. Building is an explicit operator step with a reviewable source
commit; running services receive only image references.

## Configure the site

Copy the example next to the Compose file and keep it untracked:

```bash
cd /srv/vonk-forge/source/deploy/compose
cp .env.example .env
chmod 0600 .env
```

At minimum, set these image and host values:

```dotenv
COMPOSE_PROJECT_NAME=vonk-forge-control
CONTROL_API_IMAGE=ghcr.io/carstvaartjes/vonk-forge-api:local
CONTROL_WORKER_IMAGE=ghcr.io/carstvaartjes/vonk-forge-worker:local
HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/vonk-forge-hermes:local
REPOSITORY_PATH=/srv/vonk-forge/source
CONTROL_IDENTITY_PATH=/srv/vonk-forge/control-identity
NAS_LAN_IP=10.0.0.2
VONK_BACKEND_PORT=8443
VONK_MANAGEMENT_CIDRS=10.0.0.0/24
VONK_DIRECT_FABRIC_CIDRS=
VONK_CONTROL_HOSTNAME=control.vonk-forge.lan
VONK_AGENT_ENROLL_HOSTNAME=enroll.vonk-forge.lan
VONK_AGENT_HOSTNAME=agents.vonk-forge.lan
VONK_REGISTRY_HOSTNAME=registry.vonk-forge.lan
```

`VONK_PLATFORM_VERSION`, `VONK_PLATFORM_RELEASE_DIGEST`,
`VONK_PLATFORM_BUILD_DIGEST`, `VONK_CONTROL_GENERATION_ID`,
`VONK_DATABASE_REVISION`, and `VONK_CONTROL_START_NONCE` identify the selected
local generation. Use deterministic local values during evaluation; a future
signed platform selector supplies them from verified release metadata.

Resolve `enroll.vonk-forge.lan`, `agents.vonk-forge.lan`, and
`registry.vonk-forge.lan` only on the management LAN. Allow TCP 8443 only from
the configured GPU-node management CIDRs. Human web, Grafana, and inference
traffic enter through Tailscale `svc:vonk-forge`; there is no public or general
LAN fallback.

## Secret files

Every secret setting in `.env` is a host path, never the secret value. Create
regular files under `/srv/vonk-forge/secrets` and use the exact variables in
`.env.example`. In particular:

```text
DATABASE_URL_FILE POSTGRES_PASSWORD_FILE TOKEN_SIGNING_KEY_FILE
METRICS_TOKEN_FILE GIT_SIGNING_KEY_FILE WORKER_API_TOKEN_FILE
WORKLOAD_RELEASES_KEY_FILE WORKLOAD_SNAPSHOT_KEY_FILE WORKLOAD_TIMESTAMP_KEY_FILE
GRAFANA_ADMIN_PASSWORD_FILE LITELLM_MASTER_KEY_FILE LITELLM_UPSTREAM_KEY_FILE
LITELLM_DATABASE_URL_FILE AGENT_CLIENT_CA_FILE AGENT_INTERMEDIATE_CERTIFICATE_FILE
AGENT_PROXY_AUTH_FILE AGENT_CA_CREDENTIAL_FILE AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE
STEP_CA_CONFIG_FILE STEP_CA_ROOT_CERTIFICATE_FILE STEP_CA_INTERMEDIATE_KEY_FILE
STEP_CA_PASSWORD_FILE TAILSCALE_OAUTH_CLIENT_ID_FILE
TAILSCALE_OAUTH_CLIENT_SECRET_FILE HERMES_API_KEY_FILE
```

- token-signing, worker, and agent-proxy secrets contain At least 32 bytes.
- the metrics token contains At least 16 non-whitespace characters.
- control API and worker files are readable by `10001:10001`.
- signer keys are readable by `10003:10001` only.
- LiteLLM files are readable by `10002:10001`; Prometheus uses `65534:65534`;
  Grafana uses `472:472`; and Hermes uses `1100:1100`.
- CA private material remains outside repository and recipe source trees.
- the Hermes API key is mounted from a `root:root 0400` file and read before
  the wrapper drops to `1100:1100`.

The production overlay uses `compose.step-ca.yaml`. The built-in overlay exists
only for bounded development and tests. Follow the
[agent PKI runbook](../../docs/runbooks/agent-pki.md) to create the CA material
and the [Tailscale runbook](../../docs/runbooks/tailscale.md) for OAuth files and
grants.

## Validate and start

Render before every start or update:

```bash
cd /srv/vonk-forge/source/deploy/compose
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml up -d
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml ps
```

The default command starts the required control, database, ingress, inference,
Tailscale, registry, and observability services. Hermes is optional and disabled
by default. Prepare its three data directories and start it explicitly:

```bash
sudo install -d -m 0750 -o 1100 -g 1100 \
  /srv/vonk-forge/hermes/data \
  /srv/vonk-forge/hermes/workspaces \
  /srv/vonk-forge/hermes/cache
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml \
  --profile hermes up -d
```

Interactive Hermes setup additionally enables the `setup` profile. Disabling
Hermes must not prevent the main control service, API, or Tailscale service from
starting.

## Updates, backup, and recovery

For a source-first update, fetch the reviewed commit, rebuild the owned images
with new immutable local tags, render Compose, then recreate the affected
services. Keep the previous commit and image tags until health, database, agent,
and route checks pass. Never use `latest` as rollback authority.

Back up PostgreSQL data, control state, the control identity, the source commit,
site `.env`, CA data, and optional Hermes data. Do not back up model weights as
control-plane state. The host-updater and signed OCI deployment-bundle machinery
remain available for the later release channel, but this guide does not claim
that release publication or a full automated NAS installer exists today.
