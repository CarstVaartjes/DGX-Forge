# Tailscale gateway component

This included Compose model exposes two named tailnet Services from one tagged,
userspace Tailscale node:

- `svc:dgx-forge` forwards tailnet TCP 443 to Caddy's private port 8080.
- `svc:ai-devbox` forwards tailnet TCP 22 to the devbox's OpenSSH port 22.

It publishes no Docker host port, routes no LAN subnet, and receives no tunnel
device or network capability. OAuth client ID and secret values are read from
Compose secret files. State persists in `tailscale-state`; the configurator
continuously reconciles and advertises only the two explicit Services. It uses
the explicit `--https=443` CLI form for the web listener and verifies that
Serve status reports HTTPS, never plaintext HTTP, on port 443.

Before use, define both Services in the Tailscale admin console, apply a
reviewed version of `grants.example.hujson`, and replace the GitHub-login
placeholder with the exact identity shown by Tailscale. Create an OAuth client
with only `auth_keys` write scope for `tag:dgx-gateway`. The OAuth client is for
unattended gateway enrollment; it is not the operator's GitHub credential.

See [the gateway runbook](../../../docs/runbooks/tailscale.md) for setup,
verification, backup, and recovery.
