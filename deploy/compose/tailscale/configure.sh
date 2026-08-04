#!/bin/sh
set -eu

socket=/var/run/tailscale/tailscaled.sock
remaining=120

ts() {
    tailscale --socket="${socket}" "$@"
}

while [ "${remaining}" -gt 0 ]; do
    if [ -S "${socket}" ] && ts status --json >/tmp/tailscale-status.json 2>/dev/null; then
        break
    fi
    sleep 2
    remaining=$((remaining - 2))
done

if [ "${remaining}" -le 0 ]; then
    echo "ERROR: Tailscale did not become ready within 120 seconds." >&2
    exit 1
fi

ts serve set-config --all /config/serve.json
ts serve advertise svc:dgx-forge
ts serve advertise svc:ai-devbox

ts serve status --json >/tmp/tailscale-serve-status.json
ts status --json >/tmp/tailscale-status.json

if ! grep -Fq 'svc:dgx-forge' /tmp/tailscale-serve-status.json; then
    echo "ERROR: svc:dgx-forge is absent from Tailscale Serve status." >&2
    exit 1
fi
if ! grep -Fq 'svc:ai-devbox' /tmp/tailscale-serve-status.json; then
    echo "ERROR: svc:ai-devbox is absent from Tailscale Serve status." >&2
    exit 1
fi
if ! grep -Fq 'service-host' /tmp/tailscale-status.json; then
    echo "ERROR: the gateway lacks the Tailscale service-host capability." >&2
    exit 1
fi
