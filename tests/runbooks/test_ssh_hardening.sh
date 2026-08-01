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
actual="$(cat "$drop_in")"
test "$actual" = "$expected"

embedded="$(awk '
  /^cat > "\$stage" <<'\''EOF'\''$/ { capture = 1; next }
  capture && /^EOF$/ { capture = 0; exit }
  capture { print }
' "$installer")"
test "$embedded" = "$expected"

grep -Fq 'sshd -t' "$installer"
grep -Fq 'sshd -T' "$installer"
grep -Fq 'systemctl reload ssh' "$installer"
grep -Fq 'installed_this_run' "$installer"
grep -Fq 'Authentications that can continue: publickey' "$runbook"
grep -Fq 'sudo bash /tmp/install-ssh-hardening' "$runbook"

printf 'SSH hardening artifacts: PASS\n'
