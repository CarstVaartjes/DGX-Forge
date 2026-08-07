#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runbook="$repo_root/docs/runbooks/ssh-bootstrap.md"
cleanup_source="$(awk '
  /^# BEGIN validated identity backup cleanup$/ { capture = 1; next }
  /^# END validated identity backup cleanup$/ { capture = 0; exit }
  capture { print }
' "$runbook")"
test -n "$cleanup_source"
eval "$cleanup_source"
declare -F remove_identity_backup >/dev/null

fixture_root="$(mktemp -d)"
fixture_root="$(realpath -- "$fixture_root")"
trap 'chmod -R u+rwx "$fixture_root" 2>/dev/null || true; rm -rf "$fixture_root"' EXIT

backup_files=(
  machine-id
  ssh_host_rsa_key
  ssh_host_rsa_key.pub
  ssh_host_ecdsa_key
  ssh_host_ecdsa_key.pub
  ssh_host_ed25519_key
  ssh_host_ed25519_key.pub
)

make_backup() {
  directory="$1"
  mkdir -p "$directory"
  for name in "${backup_files[@]}"; do
    : > "$directory/$name"
  done
}

expect_reject() {
  label="$1"
  candidate="$2"
  if remove_identity_backup "$candidate" "$fixture_root" >/dev/null 2>&1; then
    printf 'accepted unsafe fixture: %s\n' "$label" >&2
    exit 1
  fi
  test -f "$fixture_root/sentinel"
}

: > "$fixture_root/sentinel"

dotdot="$fixture_root/vonk-identity-backup.Ab1234"
make_backup "$dotdot"
expect_reject '/..' "$dotdot/.."

dotdot_twice="$fixture_root/vonk-identity-backup.Bc2345"
make_backup "$dotdot_twice"
expect_reject '/../..' "$dotdot_twice/../.."

nested_parent="$fixture_root/vonk-identity-backup.Cd3456"
make_backup "$nested_parent"
mkdir "$nested_parent/nested"
expect_reject 'nested path' "$nested_parent/nested"

symlink_target="$fixture_root/vonk-identity-backup.De4567"
make_backup "$symlink_target"
ln -s "$symlink_target" "$fixture_root/vonk-identity-backup.Ef5678"
expect_reject 'symlink' "$fixture_root/vonk-identity-backup.Ef5678"

wrong_basename="$fixture_root/not-vonk-identity-backup.Fg6789"
make_backup "$wrong_basename"
expect_reject 'wrong basename' "$wrong_basename"

unexpected="$fixture_root/vonk-identity-backup.Gh7890"
make_backup "$unexpected"
: > "$unexpected/unexpected"
expect_reject 'unexpected contents' "$unexpected"
test -f "$unexpected/machine-id"
test -f "$unexpected/unexpected"

valid="$fixture_root/vonk-identity-backup.Hi8901"
make_backup "$valid"
remove_identity_backup "$valid" "$fixture_root"
test ! -e "$valid"
test -f "$fixture_root/sentinel"

printf 'backup cleanup fixtures: PASS\n'
