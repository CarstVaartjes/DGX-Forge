# SSH bootstrap

This runbook establishes administration of both Vonk Forge GPU nodes with the dedicated
`Vonk Forge GPU node Admin` key held by the 1Password SSH agent. Only the public half of
the key is written to `~/.ssh`; do not export a private key and do not enable
SSH agent forwarding.

Do not enter either GPU node's Linux password until the host-identity gate in
section 2 has passed. An SSH host-key prompt is not sufficient verification.

## Hosts

| Alias | Address | User |
| --- | --- | --- |
| `vonk-node-1` | `192.168.1.211` | `carst` |
| `vonk-node-2` | `192.168.1.212` | `carst` |

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
key_stage="$(mktemp "$HOME/.ssh/.vonk_node_admin.pub.XXXXXX")"
trap 'rm -f "$key_stage"' EXIT
SSH_AUTH_SOCK="$agent_sock" ssh-add -L \
  | grep ' Vonk Forge GPU node Admin$' > "$key_stage"
test "$(awk 'END {print NR}' "$key_stage")" -eq 1
actual_admin_fp="$(ssh-keygen -lf "$key_stage" | awk '{print $2}')"
test "$actual_admin_fp" = "$expected_admin_fp"
chmod 0644 "$key_stage"
mv -f "$key_stage" "$HOME/.ssh/vonk_node_admin.pub"
trap - EXIT
ssh-keygen -lf "$HOME/.ssh/vonk_node_admin.pub"
test ! -e "$HOME/.ssh/vonk_node_admin"
```

The final command must report fingerprint
`SHA256:66yS2tf5iK+wvPkO44m++PWfI1q1BHS63BRMJqsPaqM`. The local file is
intentionally a public key; there must be no unencrypted private-key file at
`~/.ssh/vonk_node_admin`.

## 2. Repair and verify host identities

[NVIDIA CVE-2026-24218](https://nvidia.custhelp.com/app/answers/detail/a_id/5835)
documents that pre-OTA0 Vonk Forge GPU node factory images can clone SSH host keys and
`/etc/machine-id`. Both GPU nodes in this cluster had the same machine ID and the
same configured RSA, ECDSA, and Ed25519 host keys. Treat all of those original
identifiers as compromised; neither GPU node is a trusted identity anchor.

Do this from a keyboard and display attached to each physical GPU node. Before
repairing identity, use DGX Dashboard to install every offered system update
and reboot. The dashboard must report the update as installed with no pending
update. At the local console, verify that `/etc/vonk-release` contains the OTA
history and record it in the private inventory:

```bash
sudo awk -F= '/^(VONK_SWBUILD_VERSION|VONK_OTA_VERSION)=/ { print }' \
  /etc/vonk-release
```

The security bulletin calls the fixed update `OTA0`; this is the update level,
not necessarily the literal value printed in `VONK_OTA_VERSION`. If the
dashboard cannot confirm that OTA0 or a later update is installed, stop before
entering a password or enabling SSH access.

### Regenerate one physical GPU node at a time

Complete this procedure and the console-to-network fingerprint check for GPU node
1 before touching GPU node 2. Identify the device by the serial printed on its
chassis and by `/sys/class/dmi/id/product_serial`; never identify it by one of
the cloned keys. Keep the local console open until a fresh SSH session has
passed.

Start a root Bash shell. Set `expected_serial` and `expected_ip` from the
physical device and the Hosts table above. The script requires the three
standard configured host keys, creates a root-only recovery directory, rotates
the machine ID and all three keys, validates the new material, and reloads
OpenSSH. It prints only fingerprints, never private keys.

```bash
sudo bash
set -euo pipefail
expected_serial='PASTE_SERIAL_FROM_THIS_PHYSICAL_CHASSIS'
expected_ip='PASTE_EXPECTED_LAN_ADDRESS'
actual_serial="$(tr -d '\n' < /sys/class/dmi/id/product_serial)"
test "$actual_serial" = "$expected_serial"
ip -brief address | grep -F "$expected_ip"

host_keys=(
  /etc/ssh/ssh_host_rsa_key
  /etc/ssh/ssh_host_ecdsa_key
  /etc/ssh/ssh_host_ed25519_key
)
mapfile -t configured_keys < <(
  sshd -T | awk 'tolower($1) == "hostkey" { print $2 }'
)
test "${#configured_keys[@]}" -eq "${#host_keys[@]}"
for key in "${host_keys[@]}"; do
  printf '%s\n' "${configured_keys[@]}" | grep -Fxq "$key"
  test -s "$key"
  test -s "$key.pub"
done

backup_dir="$(mktemp -d /root/vonk-identity-backup.XXXXXX)"
chmod 0700 "$backup_dir"
cp -p /etc/machine-id "$backup_dir/machine-id"
for key in "${host_keys[@]}"; do
  cp -p "$key" "$key.pub" "$backup_dir/"
done
printf 'Temporary recovery directory: %s\n' "$backup_dir"

restore_identity() {
  cp -p "$backup_dir/machine-id" /etc/machine-id
  for key in "${host_keys[@]}"; do
    cp -p "$backup_dir/$(basename "$key")" "$key"
    cp -p "$backup_dir/$(basename "$key").pub" "$key.pub"
  done
}

old_machine_id="$(cat "$backup_dir/machine-id")"
if ! truncate -s 0 /etc/machine-id || ! systemd-machine-id-setup; then
  restore_identity
  exit 1
fi
new_machine_id="$(cat /etc/machine-id)"
[[ "$new_machine_id" =~ ^[0-9a-f]{32}$ ]]
test "$new_machine_id" != "$old_machine_id"

for key in "${host_keys[@]}"; do
  rm -f -- "$key" "$key.pub"
done
if ! ssh-keygen -A || ! sshd -t; then
  restore_identity
  sshd -t
  exit 1
fi
for key in "${host_keys[@]}"; do
  old_fp="$(ssh-keygen -lf "$backup_dir/$(basename "$key").pub" \
    | awk '{ print $2 }')"
  new_fp="$(ssh-keygen -lf "$key.pub" | awk '{ print $2 }')"
  test "$new_fp" != "$old_fp"
  ssh-keygen -lf "$key.pub"
done
if ! systemctl reload ssh; then
  restore_identity
  sshd -t
  systemctl reload ssh
  exit 1
fi
```

Record the new machine ID and the three new `SHA256:...` fingerprints outside
the repository. Leave the printed recovery directory in place only while the
fresh-session checks in section 5 are pending. If validation fails, restore it
from the still-open local console and investigate before proceeding.

### Compare console identities with filtered network scans

After regenerating each node, copy its final machine ID and three fingerprints
from its trusted console into the variables below on the Mac. This captures one
key record per requested algorithm. The `awk` filter is mandatory because
macOS `ssh-keyscan` can write banner comments to standard output.

```bash
set -euo pipefail
node1_machine_id='PASTE_VONK_1_MACHINE_ID'
node2_machine_id='PASTE_VONK_2_MACHINE_ID'
node1_rsa_fp='PASTE_VONK_1_RSA_SHA256_FINGERPRINT'
node1_ecdsa_fp='PASTE_VONK_1_ECDSA_SHA256_FINGERPRINT'
node1_ed25519_fp='PASTE_VONK_1_ED25519_SHA256_FINGERPRINT'
node2_rsa_fp='PASTE_VONK_2_RSA_SHA256_FINGERPRINT'
node2_ecdsa_fp='PASTE_VONK_2_ECDSA_SHA256_FINGERPRINT'
node2_ed25519_fp='PASTE_VONK_2_ED25519_SHA256_FINGERPRINT'

[[ "$node1_machine_id" =~ ^[0-9a-f]{32}$ ]]
[[ "$node2_machine_id" =~ ^[0-9a-f]{32}$ ]]
test "$node1_machine_id" != "$node2_machine_id"
for fp in \
  "$node1_rsa_fp" "$node1_ecdsa_fp" "$node1_ed25519_fp" \
  "$node2_rsa_fp" "$node2_ecdsa_fp" "$node2_ed25519_fp"; do
  case "$fp" in SHA256:*) ;; *) exit 1 ;; esac
done
test "$node1_rsa_fp" != "$node2_rsa_fp"
test "$node1_ecdsa_fp" != "$node2_ecdsa_fp"
test "$node1_ed25519_fp" != "$node2_ed25519_fp"

mkdir -p "$HOME/.ssh"
chmod 0700 "$HOME/.ssh"
scan_dir="$(mktemp -d "$HOME/.ssh/.vonk-host-key-scan.XXXXXX")"
known_stage="$(mktemp "$HOME/.ssh/.known_hosts.XXXXXX")"
cleanup_scan() {
  rm -f "$scan_dir"/node1-* "$scan_dir"/node2-* \
    "$known_stage" "$known_stage.old"
  rmdir "$scan_dir" 2>/dev/null || true
}
trap cleanup_scan EXIT

capture_key() {
  ip="$1"
  scan_type="$2"
  wire_type="$3"
  destination="$4"
  ssh-keyscan -T 5 -t "$scan_type" "$ip" 2>/dev/null \
    | awk -v host="$ip" -v type="$wire_type" \
        '$1 == host && $2 == type && NF >= 3 { print $1, $2, $3 }' \
    > "$destination"
  test "$(awk 'END { print NR }' "$destination")" -eq 1
}

capture_key 192.168.1.211 rsa ssh-rsa "$scan_dir/node1-rsa"
capture_key 192.168.1.211 ecdsa ecdsa-sha2-nistp256 \
  "$scan_dir/node1-ecdsa"
capture_key 192.168.1.211 ed25519 ssh-ed25519 \
  "$scan_dir/node1-ed25519"
capture_key 192.168.1.212 rsa ssh-rsa "$scan_dir/node2-rsa"
capture_key 192.168.1.212 ecdsa ecdsa-sha2-nistp256 \
  "$scan_dir/node2-ecdsa"
capture_key 192.168.1.212 ed25519 ssh-ed25519 \
  "$scan_dir/node2-ed25519"

fingerprint() {
  ssh-keygen -lf "$1" | awk '{ print $2 }'
}
test "$(fingerprint "$scan_dir/node1-rsa")" = "$node1_rsa_fp"
test "$(fingerprint "$scan_dir/node1-ecdsa")" = "$node1_ecdsa_fp"
test "$(fingerprint "$scan_dir/node1-ed25519")" = "$node1_ed25519_fp"
test "$(fingerprint "$scan_dir/node2-rsa")" = "$node2_rsa_fp"
test "$(fingerprint "$scan_dir/node2-ecdsa")" = "$node2_ecdsa_fp"
test "$(fingerprint "$scan_dir/node2-ed25519")" = "$node2_ed25519_fp"

if test -f "$HOME/.ssh/known_hosts"; then
  cp -p "$HOME/.ssh/known_hosts" "$known_stage"
else
  : > "$known_stage"
fi
ssh-keygen -R 192.168.1.211 -f "$known_stage" >/dev/null
rm -f "$known_stage.old"
ssh-keygen -R 192.168.1.212 -f "$known_stage" >/dev/null
rm -f "$known_stage.old"
cat "$scan_dir"/node1-* "$scan_dir"/node2-* >> "$known_stage"
chmod 0600 "$known_stage"
mv -f "$known_stage" "$HOME/.ssh/known_hosts"
rm -f "$scan_dir"/node1-* "$scan_dir"/node2-*
rmdir "$scan_dir"
trap - EXIT
```

Only the exact records for the two LAN addresses are replaced. Do not use
`StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`, or accept a changed
host key interactively.

## 3. Install the public key

Only after section 2 passes, install the public key on one GPU node at a time:

```bash
ssh-copy-id -f -i "$HOME/.ssh/vonk_node_admin.pub" carst@192.168.1.211
ssh-copy-id -f -i "$HOME/.ssh/vonk_node_admin.pub" carst@192.168.1.212
```

`-f` is required here because `IdentityFile` contains only the public half while
1Password holds the private half. Each command asks for the Linux account
password once. Enter it only into the local terminal prompt; never put it in a
command, file, chat, log, or shell history. Stop if either command reports that
no key was added.

Password authentication remains enabled until fresh key-only sessions have
passed on both nodes. The later SSH-hardening task disables it.

## 4. Install the aliases

From the repository root, run this idempotent installation. It creates all
paths, installs the committed alias file, removes duplicate managed `Include`
lines (including the quoted equivalent), and inserts exactly one before the
first active `Host` or `Match` block, or at the end when neither exists.

```bash
set -euo pipefail
mkdir -p "$HOME/.ssh/config.d"
chmod 0700 "$HOME/.ssh" "$HOME/.ssh/config.d"
install -m 0600 config/ssh/vonk-node.conf.example \
  "$HOME/.ssh/config.d/vonk-node.conf"
touch "$HOME/.ssh/config"
chmod 0600 "$HOME/.ssh/config"
config_stage="$(mktemp "$HOME/.ssh/.config.XXXXXX")"
trap 'rm -f "$config_stage"' EXIT
awk '
  function managed_include(path) {
    return path == "~/.ssh/config.d/*.conf" || \
      path == "\"~/.ssh/config.d/*.conf\""
  }
  tolower($1) == "include" && managed_include($2) && \
    (NF == 2 || substr($3, 1, 1) == "#") { next }
  !included && (tolower($1) == "host" || tolower($1) == "match") {
    print "Include ~/.ssh/config.d/*.conf"
    included = 1
  }
  { print }
  END {
    if (!included) {
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
ssh -G vonk-node-1 | grep -E \
  '^(hostname|user|identityagent|identityfile|hostkeyalgorithms) '
ssh -G vonk-node-2 | grep -E \
  '^(hostname|user|identityagent|identityfile|hostkeyalgorithms) '
```

## 5. Verify key-only sessions

Close any password-authenticated SSH sessions before this test so it proves a
fresh connection works. Unlock 1Password, then run:

```bash
ssh -o BatchMode=yes -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no vonk-node-1 hostname
ssh -o BatchMode=yes -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no vonk-node-2 hostname
```

Both commands must print a hostname and exit with status zero. `BatchMode=yes`
prevents a password fallback, although 1Password may still ask for approval to
use the key.

If a check fails, leave password authentication enabled. Confirm from a trusted
local console that the expected public key is one complete line in
`~/.ssh/authorized_keys` on the affected GPU node, correct its ownership and
permissions if necessary, and repeat the key-only check before proceeding.

Also verify that the Ed25519 records installed in `known_hosts` still match the
console values used in section 2:

```bash
known_ed25519_fp() {
  ssh-keygen -F "$1" -f "$HOME/.ssh/known_hosts" \
    | awk '$2 == "ssh-ed25519" { print }' \
    | ssh-keygen -lf - \
    | awk '{ print $2 }'
}
test "$(known_ed25519_fp 192.168.1.211)" = "$node1_ed25519_fp"
test "$(known_ed25519_fp 192.168.1.212)" = "$node2_ed25519_fp"
```

Only after both fresh key-only sessions and both fingerprint checks pass,
remove the short-lived recovery directory from each trusted local console, one
GPU node at a time. Substitute the exact path printed during regeneration; the
guard deliberately rejects broader paths:

```bash
sudo bash
set -euo pipefail
backup_dir='/root/vonk-identity-backup.PASTE_EXACT_SUFFIX'
# BEGIN validated identity backup cleanup
remove_identity_backup() {
  candidate="$1"
  trusted_parent="$2"
  test -d "$candidate" || return 1
  test ! -L "$candidate" || return 1

  canonical="$(realpath -- "$candidate")" || return 1
  canonical_parent="$(dirname -- "$canonical")"
  canonical_name="$(basename -- "$canonical")"
  test "$candidate" = "$canonical" || return 1
  test "$canonical_parent" = "$trusted_parent" || return 1
  [[ "$canonical_name" =~ ^vonk-identity-backup\.[A-Za-z0-9]{6}$ ]] \
    || return 1

  expected_files=(
    machine-id
    ssh_host_rsa_key
    ssh_host_rsa_key.pub
    ssh_host_ecdsa_key
    ssh_host_ecdsa_key.pub
    ssh_host_ed25519_key
    ssh_host_ed25519_key.pub
  )
  entries=()
  while IFS= read -r -d '' entry; do
    entries+=("$entry")
  done < <(find "$canonical" -mindepth 1 -maxdepth 1 -print0)
  test "${#entries[@]}" -eq "${#expected_files[@]}" || return 1

  for name in "${expected_files[@]}"; do
    test -f "$canonical/$name" || return 1
    test ! -L "$canonical/$name" || return 1
  done
  for entry in "${entries[@]}"; do
    entry_name="$(basename -- "$entry")"
    found=false
    for name in "${expected_files[@]}"; do
      if test "$entry_name" = "$name"; then
        found=true
        break
      fi
    done
    test "$found" = true || return 1
  done

  rm -f -- \
    "$canonical/machine-id" \
    "$canonical/ssh_host_rsa_key" \
    "$canonical/ssh_host_rsa_key.pub" \
    "$canonical/ssh_host_ecdsa_key" \
    "$canonical/ssh_host_ecdsa_key.pub" \
    "$canonical/ssh_host_ed25519_key" \
    "$canonical/ssh_host_ed25519_key.pub"
  rmdir -- "$canonical"
}
# END validated identity backup cleanup
remove_identity_backup "$backup_dir" /root
test ! -e "$backup_dir"
```

This deletion is mandatory: the backups contain the old shared private host
keys and are a recovery aid only for the bounded console-backed validation
window.
