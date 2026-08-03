#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
drop_in="$repo_root/nodes/etc/ssh/sshd_config.d/90-dgx-admin.conf"
installer="$repo_root/nodes/bin/install-ssh-hardening"
runbook="$repo_root/docs/runbooks/ssh-recovery.md"

expected="$(printf '%s\n' \
  'PasswordAuthentication no' \
  'KbdInteractiveAuthentication no' \
  'PubkeyAuthentication yes' \
  'PermitRootLogin prohibit-password')"
test "$(cat "$drop_in")" = "$expected"
bash -n "$installer"

set +e
bash "$installer" --check > /tmp/dgx-hardening-usage.out 2>&1
usage_rc=$?
set -e
test "$usage_rc" -eq 64
grep -Fq -- '--admin-user USER' /tmp/dgx-hardening-usage.out
grep -Fq -- '--admin-key-fingerprint SHA256:' /tmp/dgx-hardening-usage.out
grep -Fq -- '--drop-in FILE' /tmp/dgx-hardening-usage.out
rm -f /tmp/dgx-hardening-usage.out

grep -Fq -- '--admin-user' "$runbook"
grep -Fq -- '--admin-key-fingerprint' "$runbook"
grep -Fq -- '--recovery-marker' "$runbook"

printf 'SSH hardening artifacts: PASS\n'
