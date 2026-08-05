#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose_dir="$(cd -- "${script_dir}/.." && pwd)"
runtime_root="$(mktemp -d /tmp/dgx-hermes-agent.XXXXXX)"
project="dgx-hermes-runtime-${RANDOM}-$$"
api_key_file="${runtime_root}/hermes-api-key"

cleanup() {
    docker compose --project-name "${project}" --env-file "${compose_dir}/tests/test.env" \
        -f "${compose_dir}/compose.yaml" -f "${compose_dir}/compose.step-ca.yaml" \
        down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

mkdir -p "${runtime_root}/data" "${runtime_root}/workspaces" "${runtime_root}/cache"
printf '%s\n' 'runtime-test-key-0000000000000000' >"${api_key_file}"
chmod 600 "${api_key_file}"
export HERMES_DATA_ROOT="${runtime_root}"
export HERMES_API_KEY_FILE="${api_key_file}"
export HERMES_DASHBOARD_ORIGIN="https://hermes.runtime.invalid"

compose=(
    docker compose --project-name "${project}"
    --env-file "${compose_dir}/tests/test.env"
    -f "${compose_dir}/compose.yaml"
    -f "${compose_dir}/compose.step-ca.yaml"
)

"${compose[@]}" build hermes-agent
"${compose[@]}" up -d hermes-agent
container_id="$("${compose[@]}" ps -q hermes-agent)"
[ -n "${container_id}" ] || fail "Hermes container did not start"

for _ in {1..90}; do
    status="$(docker inspect --format '{{.State.Health.Status}}' "${container_id}" 2>/dev/null || true)"
    [ "${status}" = healthy ] && break
    sleep 1
done
[ "${status:-}" = healthy ] || fail "Hermes did not become healthy on 8642 and 9119"

host_config="$(docker inspect --format '{{json .HostConfig}}' "${container_id}")"
jq -e '.ReadonlyRootfs == true and .Privileged == false and ((.CapAdd // []) | length == 0) and ((.Devices // []) | length == 0)' \
    <<<"${host_config}" >/dev/null || fail "Hermes container privilege contract failed"
grep -Fq 'docker.sock' <<<"${host_config}" && fail "docker.sock is mounted"
docker exec "${container_id}" sh -c 'test -n "${API_SERVER_KEY:-}" && touch /workspace/runtime-persistent && touch /opt/data/runtime-persistent'
if docker exec "${container_id}" sh -c 'touch /etc/must-remain-read-only' 2>/dev/null; then
    fail "read-only root was writable"
fi

networks="$(docker inspect --format '{{json .NetworkSettings.Networks}}' "${container_id}")"
jq -e 'keys | sort == ["'"${project}"'_hermes-egress", "'"${project}"'_hermes-inference", "'"${project}"'_tailnet-hermes-edge"] | sort' \
    <<<"${networks}" >/dev/null || fail "Hermes joined an unexpected network"

"${compose[@]}" up -d --force-recreate hermes-agent
container_id="$("${compose[@]}" ps -q hermes-agent)"
docker exec "${container_id}" test -f /workspace/runtime-persistent
docker exec "${container_id}" test -f /opt/data/runtime-persistent

printf '%s\n' "Hermes Agent runtime isolation and persistence checks passed."
