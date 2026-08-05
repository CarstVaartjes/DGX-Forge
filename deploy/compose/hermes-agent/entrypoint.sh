#!/bin/sh
set -eu

test_root=${HERMES_ENTRYPOINT_TEST_ROOT:-}
secret_path="${test_root}/run/secrets/hermes-api-key"

fail() {
    printf '%s\n' "ERROR: Hermes API key file is invalid" >&2
    exit 1
}

[ ! -L "${secret_path}" ] || fail
[ -f "${secret_path}" ] || fail

secret_size=$(wc -c <"${secret_path}") || fail
[ "${secret_size}" -le 4096 ] || fail

line_count=$(awk 'END { print NR }' "${secret_path}") || fail
[ "${line_count}" -eq 1 ] || fail

API_SERVER_KEY=$(sed 's/\r$//' "${secret_path}") || fail
[ "${#API_SERVER_KEY}" -ge 32 ] || fail
case "${API_SERVER_KEY}" in
    *[!A-Za-z0-9_.~-]*) fail ;;
esac
export API_SERVER_KEY

if [ "${HERMES_ENTRYPOINT_TEST_ONLY:-0}" = "1" ]; then
    exit 0
fi

exec /init /opt/hermes/docker/main-wrapper.sh "$@"
