# SSH hardening and recovery

This runbook disables password-based SSH after the dedicated 1Password-held
`Vonk Forge GPU node Admin` key has been installed and verified. Apply it to GPU node 2
(`vonk-node-2`) first and GPU node 1 (`vonk-node-1`) only after GPU node 2 passes every
check.

The managed settings are:

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
```

`PermitRootLogin prohibit-password` does not grant root access. It prevents a
root password from being used while leaving any separately authorized root key
subject to the rest of the OpenSSH policy. The `carst` account remains the
administrative login for this cluster.

## Safety prerequisites

Complete [SSH bootstrap](ssh-bootstrap.md), including the cloned host-identity
remediation, before using this runbook. Verify the key from a fresh Mac session
on both nodes:

```bash
ssh -o BatchMode=yes \
  -o PubkeyAuthentication=yes \
  -o KbdInteractiveAuthentication=no \
  -o PasswordAuthentication=no \
  vonk-node-1 true
ssh -o BatchMode=yes \
  -o PubkeyAuthentication=yes \
  -o KbdInteractiveAuthentication=no \
  -o PasswordAuthentication=no \
  vonk-node-2 true
```

Keep one already authenticated SSH session open to each node throughout the
change. Also confirm that a keyboard and display can reach a local terminal on
each GPU node. DGX Dashboard by itself is not assumed to provide an emergency
shell; use it as a recovery route only if the installed Dashboard version
explicitly exposes a local console. Do not close the retained session until a
fresh key-only connection succeeds.

The installer has no compiled account, fingerprint, host, or address. Every
invocation supplies the administrator account, already-verified public-key
fingerprint, reviewed drop-in, and action explicitly. Mutations also require a
short-lived root-readable recovery marker created only after a retained console
or SSH session is confirmed. The installer refuses unexpected target files,
runs `sshd -t`, checks effective configuration, reloads rather than restarts
SSH, and removes a newly installed drop-in if validation fails. This effective
check matters because OpenSSH uses the first obtained value for most settings;
a syntactically valid drop-in can otherwise lose to an earlier file.

## Harden GPU node 2

From the repository root on the Mac, upload only the audited installer to
GPU node 2's temporary directory and confirm that the remote copy matches:

```bash
set -euo pipefail
installer='nodes/bin/install-ssh-hardening'
drop_in='nodes/etc/ssh/sshd_config.d/90-vonk-admin.conf'
expected_sha256="$(shasum -a 256 "$installer" | awk '{ print $1 }')"
scp "$installer" vonk-node-2:/tmp/install-ssh-hardening
scp "$drop_in" vonk-node-2:/tmp/90-vonk-admin.conf
actual_sha256="$(ssh -o BatchMode=yes vonk-node-2 \
  'shasum -a 256 /tmp/install-ssh-hardening 2>/dev/null || sha256sum /tmp/install-ssh-hardening' \
  | awk '{ print $1 }')"
test "$actual_sha256" = "$expected_sha256"
```

In the retained GPU node 2 session, set the values observed for this node and key,
create the short-lived recovery proof, run the read-only plan, then apply. Enter
the Linux password only at the terminal prompt:

```bash
admin_user='REPLACE_WITH_NODE_ADMIN_USER'
admin_fingerprint='SHA256:REPLACE_WITH_VERIFIED_PUBLIC_KEY_FINGERPRINT'
printf '%s\n' 'recovery-channel-verified' |
  sudo install -m 0600 /dev/stdin /run/vonk-ssh-recovery-verified
sudo bash /tmp/install-ssh-hardening \
  --admin-user "$admin_user" \
  --admin-key-fingerprint "$admin_fingerprint" \
  --drop-in /tmp/90-vonk-admin.conf \
  --check
sudo bash /tmp/install-ssh-hardening \
  --admin-user "$admin_user" \
  --admin-key-fingerprint "$admin_fingerprint" \
  --drop-in /tmp/90-vonk-admin.conf \
  --recovery-marker /run/vonk-ssh-recovery-verified \
  --apply
```

The check must return `change-required`; apply must return one JSON object with:

```text
"status":"changed"
```

If it reports an error, do not proceed to GPU node 1. Preserve the retained
session and follow the rollback section.

### Verify GPU node 2 from the Mac

First prove that a new connection works using only the public key:

```bash
ssh -o BatchMode=yes \
  -o PubkeyAuthentication=yes \
  -o KbdInteractiveAuthentication=no \
  -o PasswordAuthentication=no \
  vonk-node-2 true
```

Then inspect what authentication methods the server advertises when the client
does not offer a key. A failed login by itself is not proof that password login
is disabled: `BatchMode=yes` can suppress a password prompt even when the
server would accept one. This check requires an unsuccessful connection, no
advertised password or keyboard-interactive method, and an advertised
`publickey` method:

```bash
set -euo pipefail
negative_log="$(mktemp)"
trap 'rm -f "$negative_log"' EXIT
if ssh -vv \
  -o PubkeyAuthentication=no \
  -o KbdInteractiveAuthentication=no \
  -o PasswordAuthentication=yes \
  -o PreferredAuthentications=password \
  -o NumberOfPasswordPrompts=0 \
  vonk-node-2 true > /dev/null 2> "$negative_log"; then
  printf 'ERROR: password-only SSH unexpectedly succeeded\n' >&2
  exit 1
fi
grep -F 'Authentications that can continue: publickey' "$negative_log"
if grep -E 'Authentications that can continue:.*(password|keyboard-interactive)' \
  "$negative_log"; then
  printf 'ERROR: server still advertises password-based authentication\n' >&2
  exit 1
fi
rm -f "$negative_log"
trap - EXIT
```

Only after both checks pass, remove the temporary installer and close the old
GPU node 2 session:

```bash
ssh -o BatchMode=yes vonk-node-2 \
  'sudo rm -f /run/vonk-ssh-recovery-verified; rm -f /tmp/install-ssh-hardening /tmp/90-vonk-admin.conf'
```

## Harden GPU node 1

Repeat the upload, checksum, one-command installation, positive test, and
authentication-advertisement test above with `vonk-node-1`. Do not reuse a
temporary file hosted on GPU node 2. Keep GPU node 1's retained session open until
its fresh key-only test passes, then remove only the staged installer, reviewed
drop-in, and short-lived recovery marker from GPU node 1.

## Recovery and rollback

If the installer fails before reloading SSH, it removes a newly installed
managed drop-in automatically. If a fresh key-only login fails after the
installer reports success, use the retained SSH session or a physically local
terminal. Do not try to bypass host-key checking.

Inspect the exact managed file and current syntax first:

```bash
sudo sed -n '1,20p' /etc/ssh/sshd_config.d/90-vonk-admin.conf
sudo /usr/sbin/sshd -t
```

To restore the pre-task authentication policy, invoke the same reviewed
installer with `--rollback`. It removes the target only when it still matches
the supplied managed drop-in, restores it if the remaining SSH configuration
is invalid, and reloads SSH:

```bash
sudo bash /tmp/install-ssh-hardening \
  --admin-user "$admin_user" \
  --admin-key-fingerprint "$admin_fingerprint" \
  --drop-in /tmp/90-vonk-admin.conf \
  --recovery-marker /run/vonk-ssh-recovery-verified \
  --rollback
```

This rollback does not remove `authorized_keys`, rotate host keys, or change
the machine ID. After recovery, determine why the fresh key-only connection
failed before installing the drop-in again. Never enable
`StrictHostKeyChecking=no`, never forward the 1Password agent, and never place
a Linux password in a command, file, chat, log, or shell history.
