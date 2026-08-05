#!/bin/sh
set -eu

socket=${TS_SOCKET_PATH:-/var/run/tailscale/tailscaled.sock}
remaining=120
expected_service_map='{"version":"0.0.1","services":{"svc:ai-devbox":{"endpoints":{"tcp:22":"tcp://ai-devbox:22"}},"svc:dgx-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}'

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

serve_is_exact() {
    ts serve status --json >/tmp/tailscale-serve-status.json
    ts serve get-config --all >/tmp/tailscale-serve-config.json
    tr -d '[:space:]' </tmp/tailscale-serve-status.json >/tmp/tailscale-serve-status.compact
    tr -d '[:space:]' </tmp/tailscale-serve-config.json >/tmp/tailscale-serve-config.compact

    grep -Fq '"svc:dgx-forge":{"TCP":{"443":{"HTTPS":true}}' \
        /tmp/tailscale-serve-status.compact \
        && ! grep -Fq '"443":{"HTTP":true}' /tmp/tailscale-serve-status.compact \
        && grep -Fq '"svc:ai-devbox":{"TCP":{"22":{"TCPForward":"ai-devbox:22"}}}' \
            /tmp/tailscale-serve-status.compact \
        && [ "$(cat /tmp/tailscale-serve-config.compact)" = "${expected_service_map}" ]
}

configure_services() {
    # Configuration-file import currently infers the listener protocol from the
    # HTTP upstream and can create plaintext HTTP on port 443. Express the
    # listener protocol explicitly through the CLI instead.
    # Reset the complete map so undeclared services or endpoints cannot survive
    # reconciliation from an earlier gateway configuration.
    ts serve reset
    ts serve --service=svc:dgx-forge --https=443 http://caddy:8080
    ts serve --service=svc:ai-devbox --tcp=22 tcp://ai-devbox:22
    ts serve advertise svc:dgx-forge
    ts serve advertise svc:ai-devbox
}

if ! serve_is_exact; then
    configure_services
fi
if ! serve_is_exact; then
    echo "ERROR: Tailscale Services do not have the exact HTTPS and SSH listeners." >&2
    exit 1
fi

ts status --json >/tmp/tailscale-status.json
if ! grep -Fq 'service-host' /tmp/tailscale-status.json; then
    echo "ERROR: the gateway lacks the Tailscale service-host capability." >&2
    exit 1
fi

if [ "${TS_CONFIGURE_ONCE:-0}" = "1" ]; then
    exit 0
fi

# Persist as a small reconciler. If gateway state is restored or replaced while
# this Compose project stays up, a missing or downgraded listener is repaired
# without an operator having to recreate this container.
while :; do
    sleep 60
    if ! serve_is_exact; then
        configure_services
        serve_is_exact || exit 1
    fi
done
