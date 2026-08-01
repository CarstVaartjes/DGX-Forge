# SSH bootstrap

This runbook establishes administration of both DGX Sparks with the dedicated
`DGX Spark Admin` key held by the 1Password SSH agent. Only the public half of
the key is written to `~/.ssh`; do not export a private key and do not enable
SSH agent forwarding.

Do not enter either Spark's Linux password until the host-identity gate in
section 2 has passed. An SSH host-key prompt is not sufficient verification.

## Hosts

| Alias | Address | User |
| --- | --- | --- |
| `dgx-spark-1` | `192.168.1.211` | `carst` |
| `dgx-spark-2` | `192.168.1.212` | `carst` |

## 1. Export and verify the public key

Run these commands on the administering Mac. The agent output is staged beside
the destination, and the known-good file is replaced atomically only after
exactly one matching key and its fingerprint have been verified.

```bash
set -euo pipefail
agent_sock="$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
expected_admin_fp='SHA256:66yS2tf5iK+wvPkO44m++PWfI1q1BHS63BRMJqsPaqM'
mkdir -p "$HOME/.ssh"
chmod 0700 "$HOME/.ssh"
key_stage="$(mktemp "$HOME/.ssh/.dgx_spark_admin.pub.XXXXXX")"
trap 'rm -f "$key_stage"' EXIT
SSH_AUTH_SOCK="$agent_sock" ssh-add -L \
  | grep ' DGX Spark Admin$' > "$key_stage"
test "$(awk 'END {print NR}' "$key_stage")" -eq 1
actual_admin_fp="$(ssh-keygen -lf "$key_stage" | awk '{print $2}')"
test "$actual_admin_fp" = "$expected_admin_fp"
chmod 0644 "$key_stage"
mv -f "$key_stage" "$HOME/.ssh/dgx_spark_admin.pub"
trap - EXIT
ssh-keygen -lf "$HOME/.ssh/dgx_spark_admin.pub"
test ! -e "$HOME/.ssh/dgx_spark_admin"
```

The final command must report fingerprint
`SHA256:66yS2tf5iK+wvPkO44m++PWfI1q1BHS63BRMJqsPaqM`. The local file is
intentionally a public key; there must be no unencrypted private-key file at
`~/.ssh/dgx_spark_admin`.

## 2. Verify distinct host identities

This is a mandatory preflight. Use each device's trusted physical console or
its local DGX Dashboard console, not SSH. On Spark 1 and then Spark 2, run:

```bash
hostnamectl --static
ip -brief address
sudo cat /sys/class/dmi/id/product_serial
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Confirm locally that the displayed hostname, LAN address, and device serial
identify the intended physical Spark. Record the final command's full
`SHA256:...` value for each device. On the Mac, enter those two values and
assert that they are present and unique:

```bash
spark1_host_fp='PASTE_SPARK_1_CONSOLE_SHA256_FINGERPRINT'
spark2_host_fp='PASTE_SPARK_2_CONSOLE_SHA256_FINGERPRINT'
case "$spark1_host_fp" in SHA256:*) ;; *) exit 1 ;; esac
case "$spark2_host_fp" in SHA256:*) ;; *) exit 1 ;; esac
test "$spark1_host_fp" != "$spark2_host_fp"
```

If the fingerprints match, stop. Do not connect by SSH and do not enter a
password. Duplicate host keys cannot distinguish the two machines.

### Correct a duplicate host key safely

Keep Spark 1 unchanged as the identity anchor. Open a trusted local console on
Spark 2, confirm that it shows LAN address `192.168.1.212`, and keep that
console open throughout the repair. Start a root shell, paste Spark 1's trusted
console fingerprint into `spark1_host_fp`, and run:

```bash
sudo -i
set -euo pipefail
spark1_host_fp='PASTE_SPARK_1_CONSOLE_SHA256_FINGERPRINT'
case "$spark1_host_fp" in SHA256:*) ;; *) exit 1 ;; esac
ip -brief address
backup_dir="$(mktemp -d /root/dgx-ed25519-host-key-backup.XXXXXX)"
chmod 0700 "$backup_dir"
cp -p /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key.pub \
  "$backup_dir/"
new_dir="$(mktemp -d /etc/ssh/dgx-ed25519-host-key-new.XXXXXX)"
chmod 0700 "$new_dir"
ssh-keygen -q -t ed25519 -N '' -f "$new_dir/ssh_host_ed25519_key"
new_fp="$(ssh-keygen -lf "$new_dir/ssh_host_ed25519_key.pub" | awk '{print $2}')"
test "$new_fp" != "$spark1_host_fp"
install -o root -g root -m 0600 "$new_dir/ssh_host_ed25519_key" \
  /etc/ssh/ssh_host_ed25519_key
install -o root -g root -m 0644 "$new_dir/ssh_host_ed25519_key.pub" \
  /etc/ssh/ssh_host_ed25519_key.pub
if ! sshd -t; then
  install -o root -g root -m 0600 "$backup_dir/ssh_host_ed25519_key" \
    /etc/ssh/ssh_host_ed25519_key
  install -o root -g root -m 0644 "$backup_dir/ssh_host_ed25519_key.pub" \
    /etc/ssh/ssh_host_ed25519_key.pub
  exit 1
fi
if ! systemctl reload ssh; then
  install -o root -g root -m 0600 "$backup_dir/ssh_host_ed25519_key" \
    /etc/ssh/ssh_host_ed25519_key
  install -o root -g root -m 0644 "$backup_dir/ssh_host_ed25519_key.pub" \
    /etc/ssh/ssh_host_ed25519_key.pub
  sshd -t
  systemctl reload ssh
  exit 1
fi
rm -f "$new_dir/ssh_host_ed25519_key" "$new_dir/ssh_host_ed25519_key.pub"
rmdir "$new_dir"
printf 'Backup retained at %s\nNew fingerprint: %s\n' "$backup_dir" "$new_fp"
```

Do not delete the root-only backup. If a later check fails, use the still-open
local console to reinstall both backed-up files with their respective modes,
run `sshd -t`, and reload `ssh`. Repeat the four identity commands on both
trusted consoles and do not continue until the two Ed25519 fingerprints are
unique.

### Record only the console-verified keys

Back on the Mac, set `spark1_host_fp` and `spark2_host_fp` to the final values
read from the trusted consoles. The following obtains the public keys without
trusting the network response, compares their fingerprints with the trusted
values, and only then atomically updates `known_hosts`:

```bash
set -euo pipefail
spark1_host_fp='PASTE_FINAL_SPARK_1_CONSOLE_SHA256_FINGERPRINT'
spark2_host_fp='PASTE_FINAL_SPARK_2_CONSOLE_SHA256_FINGERPRINT'
test "$spark1_host_fp" != "$spark2_host_fp"
mkdir -p "$HOME/.ssh"
chmod 0700 "$HOME/.ssh"
scan_dir="$(mktemp -d "$HOME/.ssh/.dgx-host-key-scan.XXXXXX")"
scan1="$scan_dir/spark1"
scan2="$scan_dir/spark2"
known_stage="$(mktemp "$HOME/.ssh/.known_hosts.XXXXXX")"
trap 'rm -f "$scan1" "$scan2" "$known_stage" "$known_stage.old"; rmdir "$scan_dir" 2>/dev/null || true' EXIT
ssh-keyscan -T 5 -t ed25519 192.168.1.211 2>/dev/null > "$scan1"
ssh-keyscan -T 5 -t ed25519 192.168.1.212 2>/dev/null > "$scan2"
test "$(awk 'END {print NR}' "$scan1")" -eq 1
test "$(awk 'END {print NR}' "$scan2")" -eq 1
actual1="$(ssh-keygen -lf "$scan1" | awk '{print $2}')"
actual2="$(ssh-keygen -lf "$scan2" | awk '{print $2}')"
test "$actual1" = "$spark1_host_fp"
test "$actual2" = "$spark2_host_fp"
test "$actual1" != "$actual2"
if test -f "$HOME/.ssh/known_hosts"; then
  cp -p "$HOME/.ssh/known_hosts" "$known_stage"
else
  : > "$known_stage"
fi
ssh-keygen -R 192.168.1.211 -f "$known_stage" >/dev/null
rm -f "$known_stage.old"
ssh-keygen -R 192.168.1.212 -f "$known_stage" >/dev/null
rm -f "$known_stage.old"
cat "$scan1" "$scan2" >> "$known_stage"
chmod 0600 "$known_stage"
mv -f "$known_stage" "$HOME/.ssh/known_hosts"
rm -f "$scan1" "$scan2"
rmdir "$scan_dir"
trap - EXIT
```

## 3. Install the public key

Only after section 2 passes, install the public key on one Spark at a time:

```bash
ssh-copy-id -i "$HOME/.ssh/dgx_spark_admin.pub" carst@192.168.1.211
ssh-copy-id -i "$HOME/.ssh/dgx_spark_admin.pub" carst@192.168.1.212
```

Each command asks for the Linux account password once. Enter it only into the
local terminal prompt; never put it in a command, file, chat, log, or shell
history. Stop if either command reports that no key was added.

Password authentication remains enabled until fresh key-only sessions have
passed on both nodes. The later SSH-hardening task disables it.

## 4. Install the aliases

From the repository root, run this idempotent installation. It creates all
paths, installs the committed alias file, removes duplicate managed `Include`
lines, and inserts exactly one before the first `Host` block (therefore also
before every catch-all `Host *` block), or at the end when no block exists.

```bash
set -euo pipefail
mkdir -p "$HOME/.ssh/config.d"
chmod 0700 "$HOME/.ssh" "$HOME/.ssh/config.d"
install -m 0600 config/ssh/dgx-spark.conf.example \
  "$HOME/.ssh/config.d/dgx-spark.conf"
touch "$HOME/.ssh/config"
chmod 0600 "$HOME/.ssh/config"
config_stage="$(mktemp "$HOME/.ssh/.config.XXXXXX")"
trap 'rm -f "$config_stage"' EXIT
awk '
  $1 == "Include" && $2 == "~/.ssh/config.d/*.conf" { next }
  !included && $1 == "Host" {
    print "Include ~/.ssh/config.d/*.conf"
    included = 1
  }
  { print }
  END {
    if (!included) {
      print ""
      print "Include ~/.ssh/config.d/*.conf"
    }
  }
' "$HOME/.ssh/config" > "$config_stage"
chmod 0600 "$config_stage"
mv -f "$config_stage" "$HOME/.ssh/config"
trap - EXIT
```

Inspect the resolved settings without connecting:

```bash
ssh -G dgx-spark-1 | grep -E \
  '^(hostname|user|identityagent|identityfile|hostkeyalgorithms) '
ssh -G dgx-spark-2 | grep -E \
  '^(hostname|user|identityagent|identityfile|hostkeyalgorithms) '
```

## 5. Verify key-only sessions

Close any password-authenticated SSH sessions before this test so it proves a
fresh connection works. Unlock 1Password, then run:

```bash
ssh -o BatchMode=yes dgx-spark-1 hostname
ssh -o BatchMode=yes dgx-spark-2 hostname
```

Both commands must print a hostname and exit with status zero. `BatchMode=yes`
prevents a password fallback, although 1Password may still ask for approval to
use the key.

If a check fails, leave password authentication enabled. Confirm from a trusted
local console that the expected public key is one complete line in
`~/.ssh/authorized_keys` on the affected Spark, correct its ownership and
permissions if necessary, and repeat the key-only check before proceeding.
