#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose_dir="$(cd -- "${script_dir}/.." && pwd)"
test_root="$(mktemp -d /tmp/dgx-ai-devbox.XXXXXX)"
project="dgx-ai-devbox-test-${RANDOM}-$$"
override="${test_root}/runtime.override.yaml"
private_key="${test_root}/id_ed25519"
unrelated_key="${test_root}/unrelated_ed25519"
known_hosts="${test_root}/known_hosts"
agent_started=false
SSH_CLIENT_BIN="${SSH_CLIENT_BIN:-/usr/bin/ssh}"

cleanup() {
    if [[ "${agent_started}" == true ]]; then
        ssh-agent -k >/dev/null 2>&1 || true
    fi
    docker compose \
        --project-name "${project}" \
        --env-file "${compose_dir}/tests/test.env" \
        -f "${compose_dir}/compose.yaml" \
        -f "${compose_dir}/compose.step-ca.yaml" \
        -f "${override}" \
        down --remove-orphans >/dev/null 2>&1 || true
    if [[ "${test_root}" == /tmp/dgx-ai-devbox.* ]]; then
        docker run --rm \
            -v "${test_root}:/cleanup" \
            --entrypoint /usr/bin/find \
            local/ai-devbox:managed \
            /cleanup -mindepth 1 -delete >/dev/null 2>&1 || true
        rmdir -- "${test_root}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

mkdir -p \
    "${test_root}/data/home" \
    "${test_root}/data/workspaces" \
    "${test_root}/data/cache" \
    "${test_root}/data/ssh-host-keys"
ssh-keygen -q -t ed25519 -N '' -f "${private_key}"
ssh-keygen -q -t ed25519 -N '' -f "${unrelated_key}"

printf '%s\n' \
    'services:' \
    '  ai-devbox:' \
    '    ports:' \
    '      - target: 22' \
    '        published: "0"' \
    '        host_ip: 127.0.0.1' \
    '        protocol: tcp' \
    >"${override}"

export AI_DEVBOX_DATA_ROOT="${test_root}/data"
export AI_DEVBOX_AUTHORIZED_KEYS_FILE="${private_key}.pub"
export AI_DEVBOX_UID=1100
export AI_DEVBOX_GID=1100

compose=(
    docker compose
    --project-name "${project}"
    --env-file "${compose_dir}/tests/test.env"
    -f "${compose_dir}/compose.yaml"
    -f "${compose_dir}/compose.step-ca.yaml"
    -f "${override}"
)

"${compose[@]}" build ai-devbox
"${compose[@]}" up -d ai-devbox

refresh_endpoint() {
    local port_line host_key_name
    for _ in {1..60}; do
        port_line="$("${compose[@]}" port ai-devbox 22 2>/dev/null || true)"
        container_id="$("${compose[@]}" ps -q ai-devbox 2>/dev/null || true)"
        if [[ "${port_line}" =~ ^127\.0\.0\.1:([0-9]+)$ ]] \
            && [[ -n "${container_id}" ]] \
            && docker exec "${container_id}" \
                test -s /var/lib/ai-devbox/ssh-host-keys/ssh_host_ed25519_key.pub \
                2>/dev/null \
            && docker exec "${container_id}" \
                test -s /var/lib/ai-devbox/ssh-host-keys/ssh_host_rsa_key.pub \
                2>/dev/null; then
            ssh_port="${BASH_REMATCH[1]}"
            break
        fi
        sleep 1
    done
    if [[ -z "${ssh_port:-}" ]]; then
        "${compose[@]}" ps -a >&2 || true
        "${compose[@]}" logs --no-color ai-devbox >&2 || true
        fail "SSH did not acquire a loopback test port"
    fi

    : >"${known_hosts}"
    for host_key_name in ssh_host_ed25519_key.pub ssh_host_rsa_key.pub; do
        docker exec "${container_id}" \
            cat "/var/lib/ai-devbox/ssh-host-keys/${host_key_name}" \
            | awk -v host="[127.0.0.1]:${ssh_port}" \
                '{print host " " $1 " " $2}' >>"${known_hosts}"
    done
    chmod 600 "${known_hosts}"
}

refresh_endpoint

ssh_base=(
    "${SSH_CLIENT_BIN}"
    -p "${ssh_port}"
    -i "${private_key}"
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="${known_hosts}"
    -o ConnectTimeout=3
    -o BatchMode=yes
)

for _ in {1..60}; do
    if "${ssh_base[@]}" ai-dev@127.0.0.1 true 2>/dev/null; then
        break
    fi
    sleep 1
done
"${ssh_base[@]}" ai-dev@127.0.0.1 true

"${ssh_base[@]}" ai-dev@127.0.0.1 '
    test "$(whoami)" = ai-dev
    test "$(id -u)" = 1100
    test "$(id -g)" = 1100
    command -v uv >/dev/null
    command -v node >/dev/null
    command -v python >/dev/null
    command -v rustc >/dev/null
    touch /workspaces/runtime-workspace
    touch "$HOME/runtime-home"
    if touch /etc/ai-devbox-must-remain-read-only 2>/dev/null; then
        exit 91
    fi
    if sudo -n true 2>/dev/null; then
        exit 92
    fi
'

if "${ssh_base[@]}" root@127.0.0.1 true >/dev/null 2>&1; then
    fail "root login unexpectedly succeeded"
fi

if "${SSH_CLIENT_BIN}" \
    -p "${ssh_port}" \
    -i "${unrelated_key}" \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="${known_hosts}" \
    -o BatchMode=yes \
    ai-dev@127.0.0.1 true >/dev/null 2>&1; then
    fail "an unrelated key unexpectedly authenticated"
fi

if "${SSH_CLIENT_BIN}" \
    -p "${ssh_port}" \
    -o PubkeyAuthentication=no \
    -o PasswordAuthentication=yes \
    -o KbdInteractiveAuthentication=yes \
    -o PreferredAuthentications=password,keyboard-interactive \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="${known_hosts}" \
    -o BatchMode=yes \
    ai-dev@127.0.0.1 true >/dev/null 2>&1; then
    fail "password or keyboard-interactive authentication unexpectedly succeeded"
fi

eval "$(ssh-agent -s)" >/dev/null
agent_started=true
ssh-add "${private_key}" >/dev/null
"${ssh_base[@]}" -A ai-dev@127.0.0.1 'test -z "${SSH_AUTH_SOCK:-}"'

if "${ssh_base[@]}" \
    -o ExitOnForwardFailure=yes \
    -R 0:127.0.0.1:22 \
    ai-dev@127.0.0.1 true >/dev/null 2>&1; then
    fail "remote TCP forwarding unexpectedly succeeded"
fi

effective_sshd="$(docker exec "${container_id}" /usr/sbin/sshd -T -f /etc/ssh/sshd_config.d/ai-devbox.conf)"
grep -Fxq 'allowtcpforwarding no' <<<"${effective_sshd}" || fail "AllowTcpForwarding no is not effective"
grep -Fxq 'allowagentforwarding no' <<<"${effective_sshd}" || fail "agent forwarding denial is not effective"
grep -Fxq 'x11forwarding no' <<<"${effective_sshd}" || fail "X11 forwarding denial is not effective"

host_config="$(docker inspect --format '{{json .HostConfig}}' "${container_id}")"
jq -e '
    .Privileged == false
    and ((.CapAdd // []) | length == 0)
    and ((.Devices // []) | length == 0)
' <<<"${host_config}" >/dev/null || fail "unexpected privilege, CapAdd, or Devices configuration"
if grep -Fq 'docker.sock' <<<"${host_config}"; then
    fail "docker.sock is unexpectedly mounted"
fi

"${ssh_base[@]}" ai-dev@127.0.0.1 '
    printf "%s\n" "# runtime-user-customization" >>"$HOME/.bashrc"
    printf "%s\n" "persistent workspace" >/workspaces/persistent
    printf "%s\n" "persistent home" >"$HOME/persistent"
'

fingerprints_before="$(
    for key in ssh_host_ed25519_key.pub ssh_host_rsa_key.pub; do
        docker exec "${container_id}" \
            ssh-keygen -lf "/var/lib/ai-devbox/ssh-host-keys/${key}"
    done | sort
)"

"${compose[@]}" build ai-devbox
"${compose[@]}" up -d --force-recreate ai-devbox
unset ssh_port
refresh_endpoint
ssh_base=(
    "${SSH_CLIENT_BIN}"
    -p "${ssh_port}"
    -i "${private_key}"
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="${known_hosts}"
    -o ConnectTimeout=3
    -o BatchMode=yes
)
for _ in {1..60}; do
    if "${ssh_base[@]}" ai-dev@127.0.0.1 true 2>/dev/null; then
        break
    fi
    sleep 1
done
"${ssh_base[@]}" ai-dev@127.0.0.1 '
    grep -Fq "# runtime-user-customization" "$HOME/.bashrc"
    grep -Fxq "persistent workspace" /workspaces/persistent
    grep -Fxq "persistent home" "$HOME/persistent"
'

fingerprints_after="$(
    for key in ssh_host_ed25519_key.pub ssh_host_rsa_key.pub; do
        docker exec "${container_id}" \
            ssh-keygen -lf "/var/lib/ai-devbox/ssh-host-keys/${key}"
    done | sort
)"
[[ "${fingerprints_before}" == "${fingerprints_after}" ]] \
    || fail "SSH host-key fingerprints changed across recreation"

echo "AI devbox runtime isolation and persistence checks passed."
