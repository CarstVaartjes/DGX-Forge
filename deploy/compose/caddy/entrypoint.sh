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

if ! invalid_proxy_auth_bytes=$(LC_ALL=C tr -d 'A-Za-z0-9_\r\n-' < /run/secrets/agent-proxy-auth | wc -c); then
  echo "DGX Caddy proxy authentication secret is unavailable" >&2
  exit 1
fi
if [ "$invalid_proxy_auth_bytes" -ne 0 ]; then
  echo "DGX Caddy proxy authentication secret must be one base64url-like token of at least 32 characters" >&2
  exit 1
fi
if ! proxy_auth_raw=$(cat /run/secrets/agent-proxy-auth); then
  echo "DGX Caddy proxy authentication secret is unavailable" >&2
  exit 1
fi

# Command substitution removes final LF bytes. Remove any remaining CR/LF
# terminators explicitly, while preserving (and therefore rejecting) them if
# they occur within the token.
carriage_return=$(printf '\r')
line_feed='
'
while :; do
  case "$proxy_auth_raw" in
    *"$carriage_return") proxy_auth_raw=${proxy_auth_raw%"$carriage_return"} ;;
    *"$line_feed") proxy_auth_raw=${proxy_auth_raw%"$line_feed"} ;;
    *) break ;;
  esac
done
proxy_auth=$proxy_auth_raw
case "$proxy_auth" in
  "" | *[!A-Za-z0-9_-]*)
    echo "DGX Caddy proxy authentication secret must be one base64url-like token of at least 32 characters" >&2
    exit 1
    ;;
esac
if [ "${#proxy_auth}" -lt 32 ]; then
  echo "DGX Caddy proxy authentication secret must be one base64url-like token of at least 32 characters" >&2
  exit 1
fi
export DGX_AGENT_PROXY_AUTH="$proxy_auth"
if [ "$#" -eq 0 ]; then
  set -- caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
fi
exec "$@"
