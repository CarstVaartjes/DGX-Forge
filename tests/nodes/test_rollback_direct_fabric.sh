#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/nodes/bin/rollback-direct-fabric"
fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "$fixture_dir"' EXIT
mkdir -p "$fixture_dir/bin"
printf 'reviewed source\n' > "$fixture_dir/configure-direct-fabric"

cat > "$fixture_dir/bin/shasum" <<'EOF'
#!/usr/bin/env bash
printf '%s  %s\n' "${ROLLBACK_EXPECTED_SHA:?}" "$2"
EOF
cat > "$fixture_dir/bin/scp" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
host="${!#%%:*}"
printf 'stage:%s\n' "$host" >> "${ROLLBACK_ACTION_LOG:?}"
[[ "${ROLLBACK_FAIL_STAGE:-}" != "$host" ]] || exit 7
EOF
cat > "$fixture_dir/bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
args=("$@")
host=''
for arg in "${args[@]}"; do
  case "$arg" in vonk-node-1|vonk-node-2) host="$arg"; break;; esac
done
command="${args[*]}"
if [[ "$command" == *'sha256sum /tmp/configure-direct-fabric'* ]]; then
  printf 'checksum:%s\n' "$host" >> "${ROLLBACK_ACTION_LOG:?}"
  if [[ "${ROLLBACK_BAD_CHECKSUM:-}" == "$host" ]]; then printf 'bad  /tmp/configure-direct-fabric\n'; else printf '%s  /tmp/configure-direct-fabric\n' "${ROLLBACK_EXPECTED_SHA:?}"; fi
elif [[ "$command" == *'--rollback'* ]]; then
  printf 'rollback:%s\n' "$host" >> "${ROLLBACK_ACTION_LOG:?}"
  [[ "${ROLLBACK_FAIL_ROLLBACK:-}" != "$host" ]] || exit 8
elif [[ "$command" == *'hostname'* ]]; then
  printf 'management:%s\n' "$host" >> "${ROLLBACK_ACTION_LOG:?}"
  [[ "${ROLLBACK_FAIL_MANAGEMENT:-}" != "$host" ]] || exit 9
  printf 'reachable\n'
else
  printf 'unexpected ssh command: %s\n' "$command" >&2; exit 99
fi
EOF
chmod +x "$fixture_dir/bin/shasum" "$fixture_dir/bin/scp" "$fixture_dir/bin/ssh"

run_case() {
  local name="$1"
  shift
  : > "$fixture_dir/$name.log"
  set +e
  PATH="$fixture_dir/bin:$PATH" ROLLBACK_EXPECTED_SHA='abc123' \
    ROLLBACK_ACTION_LOG="$fixture_dir/$name.log" \
    "$@" "$script" --configurer "$fixture_dir/configure-direct-fabric" > "$fixture_dir/$name.out" 2>&1
  local rc=$?
  set -e
  printf '%s\n' "$rc"
}

rc="$(run_case stage-failure env ROLLBACK_FAIL_STAGE=vonk-node-2)"
test "$rc" -ne 0
test "$(cat "$fixture_dir/stage-failure.log")" = 'stage:vonk-node-2'

rc="$(run_case checksum-failure env ROLLBACK_BAD_CHECKSUM=vonk-node-2)"
test "$rc" -ne 0
test "$(cat "$fixture_dir/checksum-failure.log")" = $'stage:vonk-node-2\nchecksum:vonk-node-2'

rc="$(run_case worker-failure env ROLLBACK_FAIL_ROLLBACK=vonk-node-2)"
test "$rc" -ne 0
test "$(cat "$fixture_dir/worker-failure.log")" = $'stage:vonk-node-2\nchecksum:vonk-node-2\nrollback:vonk-node-2'

rc="$(run_case management-failure env ROLLBACK_FAIL_MANAGEMENT=vonk-node-2)"
test "$rc" -ne 0
test "$(cat "$fixture_dir/management-failure.log")" = $'stage:vonk-node-2\nchecksum:vonk-node-2\nrollback:vonk-node-2\nmanagement:vonk-node-2'

rc="$(run_case success env)"
test "$rc" -eq 0
test "$(cat "$fixture_dir/success.log")" = $'stage:vonk-node-2\nchecksum:vonk-node-2\nrollback:vonk-node-2\nmanagement:vonk-node-2\nstage:vonk-node-1\nchecksum:vonk-node-1\nrollback:vonk-node-1\nmanagement:vonk-node-1'

printf 'fabric rollback hard gate: PASS\n'
