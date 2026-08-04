#!/usr/bin/env bash
set -Eeuo pipefail

readonly USER_NAME="ai-dev"
readonly TEST_ROOT="${AI_DEVBOX_TEST_ROOT:-}"
readonly MAX_AUTHORIZED_KEYS_BYTES=262144

root_path() {
    printf '%s%s' "${TEST_ROOT}" "$1"
}

if [[ -n "${TEST_ROOT}" ]]; then
    USER_OWNER="${AI_DEVBOX_TEST_UID:?AI_DEVBOX_TEST_UID is required in test mode}"
    GROUP_OWNER="${AI_DEVBOX_TEST_GID:?AI_DEVBOX_TEST_GID is required in test mode}"
else
    USER_OWNER="${USER_NAME}"
    GROUP_OWNER="${USER_NAME}"
fi
readonly USER_OWNER GROUP_OWNER

readonly HOME_DIR="$(root_path "/home/${USER_NAME}")"
readonly WORKSPACES_DIR="$(root_path /workspaces)"
readonly CACHE_DIR="$(root_path /cache)"
readonly RUNTIME_DIR="$(root_path /run)"
readonly SOURCE="$(root_path /run/config/authorized_keys)"
readonly HOST_KEY_DIR="$(root_path /var/lib/ai-devbox/ssh-host-keys)"
readonly SKEL_DIR="$(root_path /etc/ai-devbox/skel)"
readonly TEMP_DIR="$(root_path /tmp)"

mkdir -p \
    "${HOME_DIR}/.ssh" \
    "${HOME_DIR}/.config" \
    "${HOME_DIR}/.cache" \
    "${HOME_DIR}/.local/bin" \
    "${HOME_DIR}/.local/share" \
    "${WORKSPACES_DIR}" \
    "${CACHE_DIR}/uv" \
    "${CACHE_DIR}/pip" \
    "${CACHE_DIR}/npm" \
    "${RUNTIME_DIR}/sshd" \
    "${HOST_KEY_DIR}" \
    "${TEMP_DIR}"

# Only mount roots and service-owned directories are adjusted. In particular,
# existing workspace contents are deliberately not traversed.
chown "${USER_OWNER}:${GROUP_OWNER}" \
    "${HOME_DIR}" \
    "${HOME_DIR}/.ssh" \
    "${HOME_DIR}/.config" \
    "${HOME_DIR}/.cache" \
    "${HOME_DIR}/.local" \
    "${HOME_DIR}/.local/bin" \
    "${HOME_DIR}/.local/share" \
    "${WORKSPACES_DIR}" \
    "${CACHE_DIR}" \
    "${CACHE_DIR}/uv" \
    "${CACHE_DIR}/pip" \
    "${CACHE_DIR}/npm" \
    "${HOST_KEY_DIR}"
chmod 700 "${HOME_DIR}/.ssh" "${HOST_KEY_DIR}"

if [[ ! -f "${SOURCE}" || -L "${SOURCE}" ]]; then
    echo "ERROR: authorized_keys must be a regular, non-symbolic-link file." >&2
    exit 1
fi

exec {source_fd}<"${SOURCE}"
if [[ "$(stat -Lc %F "/proc/self/fd/${source_fd}")" != "regular file" ]]; then
    echo "ERROR: authorized_keys is not a regular file." >&2
    exit 1
fi

source_size="$(stat -Lc %s "/proc/self/fd/${source_fd}")"
if (( source_size == 0 || source_size > MAX_AUTHORIZED_KEYS_BYTES )); then
    echo "ERROR: authorized_keys must contain 1-${MAX_AUTHORIZED_KEYS_BYTES} bytes." >&2
    exit 1
fi

staged_keys="$(mktemp "${TEMP_DIR}/authorized_keys.XXXXXX")"
cleanup() {
    rm -f -- "${staged_keys}"
}
trap cleanup EXIT
cp -- "/proc/self/fd/${source_fd}" "${staged_keys}"
exec {source_fd}<&-
chmod 600 "${staged_keys}"

valid_key_count=0
while IFS= read -r line || [[ -n "${line}" ]]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    if [[ -z "${trimmed}" || "${trimmed}" == \#* ]]; then
        continue
    fi
    key_probe="$(mktemp "${TEMP_DIR}/authorized_key_line.XXXXXX")"
    printf '%s\n' "${line}" >"${key_probe}"
    if ! ssh-keygen -lf "${key_probe}" >/dev/null 2>&1; then
        rm -f -- "${key_probe}"
        echo "ERROR: authorized_keys contains a malformed public key." >&2
        exit 1
    fi
    rm -f -- "${key_probe}"
    ((valid_key_count += 1))
done <"${staged_keys}"

if (( valid_key_count == 0 )); then
    echo "ERROR: authorized_keys contains no public keys." >&2
    exit 1
fi

install \
    --owner="${USER_OWNER}" \
    --group="${GROUP_OWNER}" \
    --mode=600 \
    "${staged_keys}" \
    "${HOME_DIR}/.ssh/authorized_keys"

for seed in .bashrc .tmux.conf; do
    if [[ ! -e "${HOME_DIR}/${seed}" ]]; then
        install \
            --owner="${USER_OWNER}" \
            --group="${GROUP_OWNER}" \
            --mode=600 \
            "${SKEL_DIR}/${seed}" \
            "${HOME_DIR}/${seed}"
    fi
done

if [[ ! -f "${HOST_KEY_DIR}/ssh_host_ed25519_key" ]]; then
    ssh-keygen -q -t ed25519 -N '' -f "${HOST_KEY_DIR}/ssh_host_ed25519_key"
fi
if [[ ! -f "${HOST_KEY_DIR}/ssh_host_rsa_key" ]]; then
    ssh-keygen -q -t rsa -b 4096 -N '' -f "${HOST_KEY_DIR}/ssh_host_rsa_key"
fi
chmod 600 "${HOST_KEY_DIR}/ssh_host_ed25519_key" "${HOST_KEY_DIR}/ssh_host_rsa_key"
chmod 644 "${HOST_KEY_DIR}/ssh_host_ed25519_key.pub" "${HOST_KEY_DIR}/ssh_host_rsa_key.pub"

if [[ -n "${TEST_ROOT}" ]]; then
    exit 0
fi

/usr/sbin/sshd -t -f /etc/ssh/sshd_config.d/ai-devbox.conf
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config.d/ai-devbox.conf
