# Hermes Agent replacement design

Date: 2026-08-05

Status: architecture approved; implementation landed on `main`; physical NAS
deployment and external ingress evidence remain release-gated

## Purpose

Replace the generic SSH `ai-devbox` with a persistent, containerized Nous
Research Hermes Agent. Hermes becomes the only user-facing development agent.
There is no standing SSH service, LAN listener, Docker socket, or cloud-model
fallback.

This document supersedes the AI-devbox-specific sections of
`2026-08-05-containerized-nas-access-and-devbox-design.md`. The rest of that
design remains in force: the NAS hosts one Compose project, human ingress is
Tailscale-only, GPU node enrollment/agent/registry traffic uses the restricted LAN
mTLS path, and GPU node identity and inference routing remain repository-driven.

## Decisions and alternatives

Three replacement shapes were considered:

1. Run Hermes directly inside the former SSH devbox. This preserves a shell but
   keeps two overlapping user interfaces and retains unnecessary OpenSSH state.
2. Run Hermes with its Docker terminal backend and mount the host Docker socket.
   This gives flexible sandboxes but makes Hermes effectively root-equivalent on
   the NAS.
3. Run Hermes as the sole agent service and execute its local terminal tools as
   the unprivileged Hermes user inside that container.

The third option is selected. It matches the user's dashboard/chat/API workflow,
removes SSH, and preserves a narrow container boundary. A separate sandbox
service may be designed later, but this implementation does not expose the
Docker daemon to Hermes.

## Resulting architecture

The root Compose project includes `hermes-agent/compose.yaml` instead of
`ai-devbox/compose.yaml`. The old devbox Dockerfile, OpenSSH configuration,
authorized-key example, runtime harness, host-key persistence, and SSH runbook
are removed or replaced in the same change.

The standard service lifecycle remains:

```text
docker compose up -d
```

The relevant data flow is:

```text
authorized GitHub-backed tailnet identity
  -> Tailscale HTTPS Service
  -> Hermes dashboard or authenticated gateway API
  -> Hermes Agent
  -> dedicated internal inference network
  -> LiteLLM model group: hermes-agent
  -> best eligible, already-running local agent model
  -> agent-observed GPU node management address
```

The separate GPU node path remains:

```text
GPU node on reserved 10.0.0.x management LAN
  -> Caddy restricted backend port
  -> mTLS enrollment / agent / registry routes
```

Hermes receives no control-plane administrator token, agent CA material,
registry publishing credential, Tailscale OAuth credential, or Docker socket.

## Hermes container

### Image and process

The implementation uses an immutable, digest-pinned official Hermes Agent
image. A mutable `latest` tag is not accepted. If a derived image is required
to make the dashboard compatible with a read-only root or to install tools that
must exist at startup, its base image remains digest pinned and the derived
Dockerfile is included in supply-chain verification.

The service runs `gateway run` with the official supervision model and enables:

- the gateway API on container port 8642;
- the built-in dashboard on container port 9119;
- an API server key of at least 32 random bytes; and
- unattended tool-loop hard stops.

Neither port is published by Docker. Container health must prove the gateway
and dashboard targets are responding before Tailscale advertises them.

### Identity and privileges

Hermes runs as the image's unprivileged runtime identity, mapped to a
configurable numeric NAS UID/GID when the official image supports `PUID` and
`PGID`. The service has:

- `no-new-privileges`;
- all Linux capabilities dropped initially; a capability may be restored only
  when a running-container test proves the pinned image requires that exact
  capability and the reason is recorded in the threat model;
- no privileged mode, host networking, devices, or Docker socket;
- no control, database, agent-proxy, registry, or GPU node-egress network; and
- bounded CPU, memory, shared-memory, temporary-filesystem, and log limits.

The root filesystem is read-only. If upstream Hermes requires runtime writes
outside `/opt/data`, the implementation supplies only exact writable tmpfs or
named-volume targets. It must not make the entire image root writable merely to
make startup pass.

### Persistent state

One configurable NAS root contains:

- `data`, mounted at `/opt/data`, for configuration, sessions, memory, skills,
  logs, and Hermes-managed credentials;
- `workspaces`, mounted at `/workspace`, for checked-out repositories and agent
  output; and
- `cache`, mounted only at the exact cache path selected by the pinned image.

Hermes' terminal working directory is `/workspace`. The local terminal backend
can change only the container filesystem and these explicit writable mounts.
It cannot administer sibling containers or the NAS host.

No real Hermes configuration, API key, provider token, chat-platform token, or
repository credential is committed. The official one-time setup command runs
through an explicit Compose setup profile against the same `/opt/data` mount
without publishing a port. Secrets written there have owner-only permissions
and belong to the encrypted backup set.

Tailscale's GitHub-backed login authenticates a human to the tailnet only. It
does not give Hermes access to GitHub repositories. If Hermes later needs to
push or open pull requests, a separate least-privilege repository credential is
installed in `/opt/data` or through an explicit read-only credential mount.

## Network boundaries

Hermes joins exactly three networks:

- `tailnet-hermes-edge`, an internal network shared only with the Tailscale
  gateway for inbound dashboard and API proxying;
- `hermes-inference`, an internal network shared only with LiteLLM; and
- `hermes-egress`, an ordinary outbound network for approved web, package, and
  repository access.

The NAS firewall denies the Hermes egress bridge direct access to
`10.0.0.0/24`, the direct-fabric CIDRs, link-local metadata endpoints, and all
Docker control-plane subnets. DNS and ordinary Internet access remain
available. These rules are installed and verified by the repository's one-off
host-hardening workflow; Docker network names alone are not treated as an
egress firewall.

LiteLLM joins `hermes-inference` in addition to its existing networks. Hermes
uses `http://litellm:4000/v1` and a dedicated LiteLLM client key. It never uses
the LiteLLM master key. LiteLLM remains the sole path from Hermes to local model
endpoints.

## Tailscale ingress and authorization

The SSH service and SSH-specific grant are deleted. Two explicit named Services
replace it:

- `svc:hermes-dashboard`, HTTPS port 443 to
  `http://hermes-agent:9119`; and
- `svc:hermes-api`, HTTPS port 443 to `http://hermes-agent:8642`.

The existing `svc:vonk-forge` web Service remains unchanged. The persistent
configurator verifies the complete exported three-Service map, the HTTPS
listener type, and each exact upstream. Any extra, missing, drained, downgraded,
or retargeted Service causes a complete reset and deterministic recreation.

Tailnet grants authorize a dedicated `group:hermes-users`, initially containing
the operator's GitHub-backed Tailscale identity, to the two Hermes Services.
The API still requires its independent API key. Dashboard CORS is restricted to
the actual dashboard Service origin; a wildcard origin is not used.

The scoped Tailscale OAuth client and `tag:vonk-gateway` recovery path remain
unchanged except that auto-approval names the two Hermes Services instead of the
old SSH Service. Loss of Tailscale state may recreate only the exact declared
Services and never opens a LAN fallback.

## Local agent-model selection

Hermes is permanently configured with one OpenAI-compatible model name:
`hermes-agent`. It never names a GPU node, management IP, workload port, cluster
profile, or model vendor directly.

Vonk Forge owns `config/hermes-agent-policy.toml`. Its schema contains an exact
version, alias `hermes-agent`, `local_only = true`, and an ordered candidate
list. Every candidate has only a workload ID, unique integer priority, and
minimum maturity. Unknown fields, duplicate priorities, unknown workloads, and
anything below `accepted` maturity are rejected. Initial ordering prefers:

1. the accepted dual-GPU node DeepSeek agent runtime when that workload is already
   running and healthy;
2. the accepted single-GPU node DeepSeek agent runtime when it is already running
   as part of a single or mixed profile; and
3. future local agent runtimes only after their definitions and exact profile
   have reached the policy's accepted state.

The selection inputs are the pinned Git policy, active reconciliation profile,
accepted workload evidence, authenticated agent presence, repository-declared
port, successful bounded model probe, and the existing short route lease. A
candidate is unavailable if any input is absent, stale, revoked, changed, or
unhealthy.

Only candidates that are already running may be selected. Request failure does
not automatically stop workloads or switch cluster profiles. When multiple
eligible local candidates are simultaneously running, LiteLLM tries them in
repository priority order. Fallback is allowed only for connection failure or
an HTTP 429, 502, 503, or 504 before any completion content has been accepted.
Once response streaming or completion content begins, the request is never
replayed against another model.

The generated LiteLLM configuration contains the `hermes-agent` group only when
at least one candidate is eligible. Its lease expires no later than the oldest
source presence observation. A changed endpoint enters maintenance before the
replacement is probed. If no local candidate remains, the alias is withdrawn
and Hermes receives an unavailable response.

`local_only = true` is an invariant, not a runtime preference. The policy parser
rejects candidates with non-management URLs, cloud provider names, arbitrary
base URLs, or credentials outside the local LiteLLM path. Nous Portal,
OpenRouter, OpenAI, Anthropic, and other remote model providers are never used as
model fallbacks. Hermes may still use ordinary Internet tools when explicitly
invoked; model prompts and agent context remain on the local inference path.

## Startup, setup, and recovery

Normal startup includes Hermes and requires no interactive SSH session. The
initial one-time workflow is:

1. create the external data, workspace, and cache directories with the selected
   UID/GID;
2. run the Compose setup profile interactively against `/opt/data`;
3. configure the local custom model endpoint as
   `http://litellm:4000/v1`, model `hermes-agent`, and the dedicated client key;
4. configure dashboard/API authentication and optional chat platforms;
5. start the normal project and verify both exact Tailscale Services; and
6. confirm the first request reports the selected local workload without
   exposing its management address.

On restart, Hermes resumes configuration, sessions, skills, memory, and logs
from `/opt/data`. The Tailscale reconciler advertises Hermes only after both
local targets are healthy. If Hermes configuration is absent or invalid, the
container and Services remain unavailable rather than entering an unauthenticated
setup mode.

Backups include Hermes data and workspaces alongside Tailscale state and the
existing control-plane data. Cache is optional. Restore testing verifies file
ownership, API-key continuity, session visibility, exact Service mapping, and
that no stale LiteLLM route becomes active before fresh validation.

## Failure behavior

- Tailscale loss removes user access without opening a host or LAN port.
- Hermes API or dashboard failure removes the affected Service from the usable
  path and does not redirect it to control-plane Caddy.
- Missing API authentication, invalid CORS, or absent setup state fails closed.
- LiteLLM or all eligible local models being unavailable yields an explicit
  unavailable result; it never invokes a cloud model.
- A stale GPU node observation, changed management address, failed probe, revoked
  node, or ineligible Git commit withdraws the candidate within the existing
  route-lease window.
- Hermes cannot recover itself by manipulating Docker. Container recovery is
  handled by Compose restart policy and the NAS operator.
- Loss of `/opt/data` is treated as agent identity/state loss and requires the
  setup and credential recovery procedure.

## Verification and acceptance

Structural tests render the complete Compose project and prove:

- the Hermes include replaces the AI-devbox include;
- no SSH service, SSH grant, authorized-key mount, host-key volume, or Docker
  port remains;
- Hermes has no Docker socket, devices, privilege, host networking, dangerous
  capabilities, or private control-plane networks;
- the only Hermes writable paths are explicit state, workspace, cache, and
  bounded temporary mounts;
- Hermes reaches only its Tailscale edge, inference, and controlled egress
  networks;
- LiteLLM shares only the dedicated inference network with Hermes;
- every pulled image is digest pinned and included in supply verification; and
- the complete Tailscale export contains exactly the Vonk Forge, Hermes
  dashboard, and Hermes API Services.

Runtime tests prove:

- setup state survives recreation;
- the gateway and dashboard become healthy with a read-only root;
- the dashboard is reachable only through its authorized Tailscale identity;
- the API rejects absent and invalid keys even for an authorized tailnet user;
- the container cannot reach the Docker socket, NAS management address, GPU node
  management CIDR, direct fabric, or control/data networks;
- workspaces and Hermes memory survive restart; and
- the service runs as the expected non-root UID/GID with no additional
  privileges.

Routing tests prove:

- Hermes always requests `hermes-agent` through LiteLLM;
- accepted healthy dual-GPU node DeepSeek outranks an eligible single-GPU node
  candidate;
- a mixed profile selects its already-running accepted single-GPU node agent;
- an unhealthy primary falls back only to a simultaneously running, accepted,
  fresh local candidate;
- ambiguous partial generations are not replayed;
- a model absent from the pinned policy or below accepted maturity cannot join
  the group;
- loss of all candidates withdraws the group within the lease window; and
- no generated configuration contains a cloud provider or non-management model
  URL.

Deployment acceptance confirms that an authorized GitHub-backed tailnet user
can use the dashboard and separately authenticated API, an unauthorized
tailnet identity is denied, an ordinary LAN client cannot reach Hermes, and the
NAS firewall blocks direct Hermes-to-GPU node traffic while LiteLLM-backed local
inference succeeds.

## Documentation changes

Implementation replaces the AI-devbox runbook with a Hermes Agent runbook and
updates bootstrap, recovery, Tailscale, threat-model, supply-chain, and root
README references. Operator inputs document only non-secret paths, UID/GID,
resource limits, dashboard origin, and secret-file or `/opt/data` locations.

The runbook explicitly distinguishes three identities:

- GitHub-backed Tailscale identity for reaching the Services;
- Hermes API key for invoking the gateway API; and
- optional repository credential for Hermes Git operations.

None implies either of the others.

## Scope boundaries

- Hermes does not administer Docker, Compose, the NAS host, or the GPU node
  control plane.
- Hermes does not receive SSH access to the GPU nodes.
- This change does not alter GPU node mTLS enrollment, agent discovery, registry,
  or manual hardening.
- It does not auto-switch cluster profiles in response to an inference error.
- It does not use remote model providers as fallback.
- It does not introduce a general-purpose SSH recovery shell.
- Adding a stronger sandbox backend, more Hermes profiles, audio devices, chat
  bridges, or cloud execution requires a separate reviewed design.

## Primary references

- [Hermes Agent Docker deployment](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/docker.md)
- [Hermes Agent terminal backends](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/tools.md)
- [Tailscale Services configuration](https://tailscale.com/docs/reference/tailscale-services-configuration-file)
- [LiteLLM proxy and router overview](https://docs.litellm.ai/)
