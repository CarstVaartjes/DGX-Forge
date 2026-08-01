#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/nodes/bin/disable-earlyoom"
fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "$fixture_dir"' EXIT

cat > "$fixture_dir/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

case "${EARLYOOM_TEST_STATE:?}" in
  absent)
    case "$1" in
      show) printf 'not-found\n'; exit 0 ;;
      is-enabled) printf 'not-found\n'; exit 4 ;;
      is-active) printf 'inactive\n'; exit 4 ;;
    esac
    ;;
  disabled)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'disabled\n'; exit 1 ;;
      is-active) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
  masked)
    case "$1" in
      show) printf 'masked\n'; exit 0 ;;
      is-enabled) printf 'masked\n'; exit 1 ;;
      is-active) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
  enabled)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'enabled\n'; exit 0 ;;
      is-active) printf 'active\n'; exit 0 ;;
    esac
    ;;
  static)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'static\n'; exit 0 ;;
      is-active) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
esac

printf 'unexpected systemctl invocation: %s\n' "$*" >&2
exit 99
EOF

cat > "$fixture_dir/dpkg-query" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${EARLYOOM_TEST_STATE:?}" == absent ]]; then
  printf 'dpkg-query: no packages found matching earlyoom\n' >&2
  exit 1
fi
printf 'installed 1.8.2-1\n'
EOF

chmod +x "$fixture_dir/systemctl" "$fixture_dir/dpkg-query"

run_check() {
  local state="$1"
  local expected_rc="$2"
  local output_file="$fixture_dir/$state.out"
  local rc

  set +e
  PATH="$fixture_dir:$PATH" EARLYOOM_TEST_STATE="$state" \
    bash "$script" --check > "$output_file" 2>&1
  rc=$?
  set -e
  test "$rc" -eq "$expected_rc"
  grep -Fq "classification=$state" "$output_file"
}

run_check absent 0
grep -Fq 'before.enabled.exit_code=4' "$fixture_dir/absent.out"
grep -Fq 'before.active.exit_code=4' "$fixture_dir/absent.out"
grep -Fq 'PASS: earlyoom is absent; no change required' "$fixture_dir/absent.out"

run_check disabled 0
grep -Fq 'PASS: earlyoom is disabled and inactive' "$fixture_dir/disabled.out"

run_check masked 0
grep -Fq 'PASS: earlyoom is masked and inactive' "$fixture_dir/masked.out"

run_check enabled 2
grep -Fq 'CHANGE_REQUIRED: earlyoom must be stopped and disabled' \
  "$fixture_dir/enabled.out"

run_check static 3
grep -Fq 'ERROR: unexpected earlyoom state; refusing to change it' \
  "$fixture_dir/static.out"

grep -Fq 'systemctl stop earlyoom' "$script"
grep -Fq 'systemctl disable earlyoom' "$script"

printf 'earlyoom safeguard: PASS\n'
