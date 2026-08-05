# Operate tailnet-only NAS ingress

The NAS stack has one containerized Tailscale gateway and no host Tailscale
dependency. Human control, inference, Grafana, and devbox SSH enter only through
named Tailscale Services. The only LAN listener is Caddy's Spark backend on the
reserved NAS address.

## Identity and access policy

GitHub login works for people. A GitHub-backed Tailscale user is represented in
policy as `USERNAME@github`; use the exact login shown on the Tailscale Users
page. This identity grants network reachability only. OpenSSH still requires the
public key installed for `ai-dev`.

The gateway never receives a GitHub token. Create a separate Tailscale OAuth
client under **Trust credentials** with only:

- `auth_keys` write scope; and
- `tag:dgx-gateway` as its only permitted tag.

Define `tag:dgx-gateway`, `svc:dgx-forge` with endpoint `tcp:443`, and
`svc:ai-devbox` with endpoint `tcp:22` in the admin console. Start from
`deploy/compose/tailscale/grants.example.hujson`, replace
`replace-with-your-login@github`, then merge its sections into the tailnet
policy. Keep the web and SSH grants separate and retain exact
`autoApprovers.services` entries. Never replace them with `svc:*` or restore the
default allow-all ACL.

This is raw TCP forwarding to ordinary OpenSSH, not Tailscale SSH. No `ssh`
policy stanza and no `tailscale up --ssh` setting is required.

## Secret files and startup

On the NAS, write one credential value per regular file:

```bash
umask 077
install -d -m 0700 /srv/dgx-forge/secrets
printf '%s' 'PASTE_TAILSCALE_CLIENT_ID' \
  > /srv/dgx-forge/secrets/tailscale-oauth-client-id
printf '%s' 'PASTE_TAILSCALE_CLIENT_SECRET' \
  > /srv/dgx-forge/secrets/tailscale-oauth-client-secret
chmod 0600 /srv/dgx-forge/secrets/tailscale-oauth-client-*
```

Do not put either value in `.env`, shell history, Git, or a GitHub secret used by
the NAS. Set only their file paths in `.env`, then start the one Compose project:

```bash
cd deploy/compose
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml up -d
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml ps
```

The gateway uses persisted state, `TS_AUTH_ONCE=true`, and file-backed OAuth
credentials. Ordinary restarts retain node identity. If state is absent during
a clean host rebuild, the scoped OAuth client creates the tagged node without a
human login and the two exact service auto-approvals restore advertisements.
Failure to authenticate or approve leaves ingress closed; it never opens a LAN
fallback.

The configurator remains running as a reconciler. It waits for Caddy and the
devbox to be healthy, configures the web Service with the explicit
`--https=443` CLI flag, and refuses a status that reports plaintext HTTP on port
443. This deliberately avoids the ambiguous Services configuration-file import
path when the TLS listener proxies to a local HTTP upstream.

## LAN DNS and firewall

Reserve `10.0.0.2` for the NAS and map these local-only records to it:

```text
enroll.dgx-forge.lan   10.0.0.2
agents.dgx-forge.lan   10.0.0.2
registry.dgx-forge.lan 10.0.0.2
```

Allow TCP 8443 to `10.0.0.2` only from the Spark management network
`10.0.0.0/24`, or preferably from the reserved Spark leases within that CIDR.
Do not allow LAN access to ports 22, 443, or 8080. DHCP reservations remain an
operational convenience; Spark identity and routing do not depend on a fixed
address.

## Verification

Run these checks after startup or policy changes:

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml exec tailscale-gateway \
  tailscale --socket=/var/run/tailscale/tailscaled.sock status --json
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml exec tailscale-gateway \
  tailscale --socket=/var/run/tailscale/tailscaled.sock serve status --json
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml logs tailscale-configurator
```

The status must show `HTTPS: true` for `svc:dgx-forge` port 443, no `HTTP: true`
on that port, the raw TCP forward for `svc:ai-devbox` port 22, and the tagged
service-host capability. From an authorized tailnet device, open the
`dgx-forge` Service and connect to
`ai-devbox` on TCP 22. Repeat from a tailnet identity outside the SSH group and
confirm port 22 is denied. From an ordinary LAN client, confirm the human and
SSH endpoints are unreachable.

## Drain, revocation, backup, and recovery

`docker compose down` stops the complete application, including active devbox
SSH sessions and all tailnet ingress. For planned work, announce the outage,
save work, stop new jobs, and then bring down the project.

Back up the `tailscale-state` Docker volume with the same encrypted,
authenticated off-host backup set as the control database and devbox state.
Record the gateway node ID and service status separately without credentials.
Restore the state volume before startup when possible; this preserves the node
identity.

If the state cannot be restored, keep the OAuth files in place and recreate the
project. Verify that exactly one current `tag:dgx-gateway` node advertises both
Services, then revoke the orphaned prior node. A changed Tailscale node identity
is expected after state loss; a changed devbox SSH host fingerprint is not.

For compromise, revoke the OAuth client, revoke the gateway node, and remove or
disable its tag/service approvals. Tailnet ingress stops immediately. Rotate
the OAuth client and recover through a reviewed policy; never add a temporary
LAN human listener.
