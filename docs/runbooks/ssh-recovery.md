# SSH hardening and recovery

This runbook disables password-based SSH after the dedicated 1Password-held
`DGX Spark Admin` key has been installed and verified. Apply it to Spark 2
(`dgx-spark-2`) first and Spark 1 (`dgx-spark-1`) only after Spark 2 passes every
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
  dgx-spark-1 true
ssh -o BatchMode=yes \
  -o PubkeyAuthentication=yes \
  -o KbdInteractiveAuthentication=no \
  -o PasswordAuthentication=no \
  dgx-spark-2 true
```

Keep one already authenticated SSH session open to each node throughout the
change. Also confirm that a keyboard and display can reach a local terminal on
each Spark. DGX Dashboard by itself is not assumed to provide an emergency
shell; use it as a recovery route only if the installed Dashboard version
explicitly exposes a local console. Do not close the retained session until a
fresh key-only connection succeeds.

The installer is intentionally cluster-specific. It checks that the expected
1Password public-key fingerprint is present in `carst`'s `authorized_keys`,
refuses to overwrite an unexpected file, runs `sshd -t`, checks the effective
configuration, reloads rather than restarts SSH, and removes the new drop-in if
validation fails. This effective check matters because OpenSSH uses the first
obtained value for most settings; a syntactically valid drop-in can otherwise
lose to an earlier file.

## Harden Spark 2

From the repository root on the Mac, upload only the audited installer to
Spark 2's temporary directory and confirm that the remote copy matches:

```bash
set -euo pipefail
installer='nodes/bin/install-ssh-hardening'
expected_sha256="$(shasum -a 256 "$installer" | awk '{ print $1 }')"
scp "$installer" dgx-spark-2:/tmp/install-ssh-hardening
actual_sha256="$(ssh -o BatchMode=yes dgx-spark-2 \
  'shasum -a 256 /tmp/install-ssh-hardening 2>/dev/null || sha256sum /tmp/install-ssh-hardening' \
  | awk '{ print $1 }')"
test "$actual_sha256" = "$expected_sha256"
```

In the retained Spark 2 session, run exactly this privileged command and enter
the Linux password only at the terminal prompt:

```bash
sudo bash /tmp/install-ssh-hardening
```

It must print the four effective settings followed by:

```text
SSH hardening installed, validated, and reloaded successfully.
```

If it reports an error, do not proceed to Spark 1. Preserve the retained
session and follow the rollback section.

### Verify Spark 2 from the Mac

First prove that a new connection works using only the public key:

```bash
ssh -o BatchMode=yes \
  -o PubkeyAuthentication=yes \
  -o KbdInteractiveAuthentication=no \
  -o PasswordAuthentication=no \
  dgx-spark-2 true
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
  dgx-spark-2 true > /dev/null 2> "$negative_log"; then
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
Spark 2 session:

```bash
ssh -o BatchMode=yes dgx-spark-2 'rm -f /tmp/install-ssh-hardening'
```

## Harden Spark 1

Repeat the upload, checksum, one-command installation, positive test, and
authentication-advertisement test above with `dgx-spark-1`. Do not reuse a
temporary file hosted on Spark 2. Keep Spark 1's retained session open until
its fresh key-only test passes, then remove only
`/tmp/install-ssh-hardening` from Spark 1.

## Recovery and rollback

If the installer fails before reloading SSH, it removes a newly installed
managed drop-in automatically. If a fresh key-only login fails after the
installer reports success, use the retained SSH session or a physically local
terminal. Do not try to bypass host-key checking.

Inspect the exact managed file and current syntax first:

```bash
sudo sed -n '1,20p' /etc/ssh/sshd_config.d/90-dgx-admin.conf
sudo /usr/sbin/sshd -t
```

To restore the pre-task authentication policy, remove only this repository's
managed drop-in, validate the remaining configuration, and reload SSH:

```bash
sudo bash -c '
set -e
target=/etc/ssh/sshd_config.d/90-dgx-admin.conf
test -f "$target"
test ! -L "$target"
rm -f -- "$target"
/usr/sbin/sshd -t
systemctl reload ssh
systemctl is-active --quiet ssh
'
```

This rollback does not remove `authorized_keys`, rotate host keys, or change
the machine ID. After recovery, determine why the fresh key-only connection
failed before installing the drop-in again. Never enable
`StrictHostKeyChecking=no`, never forward the 1Password agent, and never place
a Linux password in a command, file, chat, log, or shell history.
