# Operate tailnet-only NAS ingress

The NAS project contains one userspace Tailscale gateway and has no host
Tailscale dependency. Human control, inference, Grafana, and Hermes enter only
through named Tailscale Services. The sole LAN listener is Caddy's restricted
Spark backend at the reserved NAS address.

## Identity and access policy

GitHub login authenticates people to Tailscale. Use the exact
`USERNAME@github` identity shown on the Tailscale Users page. This identity
grants network reachability only: it is not the Hermes API key and gives Hermes
no repository credential.

The gateway never receives a GitHub token. Create a separate OAuth client under
Trust credentials with only `auth_keys` write scope and `tag:dgx-gateway` as its
only tag. Define these exact Services in the admin console:

- `svc:dgx-forge`, endpoint `tcp:443`;
- `svc:hermes-dashboard`, endpoint `tcp:443`; and
- `svc:hermes-api`, endpoint `tcp:443`.

Merge the reviewed sections of `deploy/compose/tailscale/grants.example.hujson`
into tailnet policy after replacing the GitHub-login placeholder. Administrators
reach only the DGX Forge Service through its grant. `group:hermes-users` reaches
only the two Hermes Services. Auto-approval permits only `tag:dgx-gateway` to
advertise the three named Services. Never use `svc:*` or an allow-all ACL.

## Secrets and unattended startup

```bash
umask 077
install -d -m 0700 /srv/dgx-forge/secrets
printf '%s' 'PASTE_TAILSCALE_CLIENT_ID' \
  > /srv/dgx-forge/secrets/tailscale-oauth-client-id
printf '%s' 'PASTE_TAILSCALE_CLIENT_SECRET' \
  > /srv/dgx-forge/secrets/tailscale-oauth-client-secret
chmod 0600 /srv/dgx-forge/secrets/tailscale-oauth-client-*
```

Set only the file paths in `.env`. Start the complete project:

```bash
cd deploy/compose
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml up -d
```

Persisted state and `TS_AUTH_ONCE=true` retain node identity. After clean state
loss, the scoped OAuth client performs unattended tagged enrollment and the
exact auto-approvals restore advertisements. Authentication or approval failure
leaves ingress closed; there is no LAN fallback.

The configurator waits for Caddy and Hermes health. It resets any missing,
extra, downgraded, or retargeted Serve map and deterministically creates:

```text
svc:dgx-forge         HTTPS 443 -> http://caddy:8080
svc:hermes-api        HTTPS 443 -> http://hermes-agent:8642
svc:hermes-dashboard  HTTPS 443 -> http://hermes-agent:9119
```

All listeners use explicit `--https=443`; plaintext HTTP on 443 is rejected.

## LAN boundary

Reserve `10.0.0.2` for the NAS and resolve these only on the management LAN:

```text
enroll.dgx-forge.lan   10.0.0.2
agents.dgx-forge.lan   10.0.0.2
registry.dgx-forge.lan 10.0.0.2
```

Allow TCP 8443 only from `10.0.0.0/24`, preferably narrowed to reserved Spark
leases. Do not allow LAN access to human or Hermes endpoints. Spark DHCP
reservations improve stability, but identity and routing use authenticated
agent presence rather than a hard-coded address.

## Verification

```bash
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml \
  exec tailscale-gateway tailscale \
  --socket=/var/run/tailscale/tailscaled.sock status --json
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml \
  exec tailscale-gateway tailscale \
  --socket=/var/run/tailscale/tailscaled.sock serve status --json
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml \
  exec tailscale-gateway tailscale \
  --socket=/var/run/tailscale/tailscaled.sock serve get-config --all
docker compose --env-file .env -f compose.yaml -f compose.step-ca.yaml \
  logs tailscale-configurator
```

Status must report `HTTPS: true` on all three Services and never `HTTP: true`.
The export must contain exactly the three upstreams above. Test dashboard and
API reachability as an authorized GitHub-backed user, then confirm a user
outside `group:hermes-users` is denied. Even an authorized user must supply the
separate Hermes key to invoke the API. Confirm an ordinary LAN client cannot
reach either Hermes endpoint.

## Drain, revocation, and recovery

`docker compose down` stops the entire application and all tailnet ingress. Back
up `tailscale-state` and the OAuth files with the same encrypted generation as
the control database and Hermes state. Restore state before startup when
possible.

If state cannot be restored, recreate the project with the OAuth files. Verify
exactly one current tagged node advertises all three Services and revoke the
orphan. For compromise, revoke OAuth, the node, and its tag/Service approvals;
then rotate and recover through reviewed policy. Never add a temporary LAN
human endpoint.
