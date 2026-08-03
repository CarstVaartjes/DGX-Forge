#!/bin/sh
set -eu

: "${DGX_CONTROL_HOSTNAME:?set DGX_CONTROL_HOSTNAME}"
: "${DGX_AGENT_ENROLL_HOSTNAME:?set DGX_AGENT_ENROLL_HOSTNAME}"
: "${DGX_AGENT_HOSTNAME:?set DGX_AGENT_HOSTNAME}"

normalize_hostname() {
  hostname=$1
  case "$hostname" in
    *[!A-Za-z0-9.-]*)
      echo "DGX Caddy SNI hostname is invalid: $hostname" >&2
      exit 64
      ;;
  esac
  normalized=$(printf '%s' "$hostname" | tr '[:upper:]' '[:lower:]')
  normalized=${normalized%.}
  case "$normalized" in
    "" | .* | *..* | *.)
      echo "DGX Caddy SNI hostname is invalid: $hostname" >&2
      exit 64
      ;;
  esac
  saved_ifs=$IFS
  IFS=.
  set -- $normalized
  IFS=$saved_ifs
  for label in "$@"; do
    case "$label" in
      -* | *-)
        echo "DGX Caddy SNI hostname is invalid: $hostname" >&2
        exit 64
        ;;
    esac
  done
  printf '%s' "$normalized"
}

control_hostname=$(normalize_hostname "$DGX_CONTROL_HOSTNAME")
enrollment_hostname=$(normalize_hostname "$DGX_AGENT_ENROLL_HOSTNAME")
agent_hostname=$(normalize_hostname "$DGX_AGENT_HOSTNAME")

if [ "$control_hostname" = "$enrollment_hostname" ] \
  || [ "$control_hostname" = "$agent_hostname" ] \
  || [ "$enrollment_hostname" = "$agent_hostname" ]; then
  echo "DGX Caddy SNI hostnames must be distinct" >&2
  exit 64
fi

if ! proxy_auth_bytes=$(wc -c < /run/secrets/agent-proxy-auth); then
  echo "DGX Caddy proxy authentication secret is unavailable" >&2
  exit 1
fi
if ! proxy_auth=$(cat /run/secrets/agent-proxy-auth); then
  echo "DGX Caddy proxy authentication secret is unavailable" >&2
  exit 1
fi
if [ -z "$proxy_auth" ] || [ "$proxy_auth_bytes" -lt 32 ]; then
  echo "DGX Caddy proxy authentication secret must contain at least 32 bytes" >&2
  exit 1
fi
export DGX_AGENT_PROXY_AUTH="$proxy_auth"
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
