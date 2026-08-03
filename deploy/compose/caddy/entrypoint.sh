#!/bin/sh
set -eu

: "${DGX_CONTROL_HOSTNAME:?set DGX_CONTROL_HOSTNAME}"
: "${DGX_AGENT_ENROLL_HOSTNAME:?set DGX_AGENT_ENROLL_HOSTNAME}"
: "${DGX_AGENT_HOSTNAME:?set DGX_AGENT_HOSTNAME}"

if [ "$DGX_CONTROL_HOSTNAME" = "$DGX_AGENT_ENROLL_HOSTNAME" ] \
  || [ "$DGX_CONTROL_HOSTNAME" = "$DGX_AGENT_HOSTNAME" ] \
  || [ "$DGX_AGENT_ENROLL_HOSTNAME" = "$DGX_AGENT_HOSTNAME" ]; then
  echo "DGX Caddy SNI hostnames must be distinct" >&2
  exit 64
fi

export DGX_AGENT_PROXY_AUTH="$(cat /run/secrets/agent-proxy-auth)"
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
