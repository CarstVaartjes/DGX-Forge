# SSH bootstrap

This runbook establishes administration of both DGX Sparks with the dedicated
`DGX Spark Admin` key held by the 1Password SSH agent. Only the public half of
the key is written to `~/.ssh`; do not export a private key and do not enable
SSH agent forwarding.

## Hosts

| Alias | Address | User |
| --- | --- | --- |
| `dgx-spark-1` | `192.168.1.211` | `carst` |
| `dgx-spark-2` | `192.168.1.212` | `carst` |

## 1. Export and verify the public key

Run these commands on the administering Mac:

```bash
agent_sock="$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
mkdir -p "$HOME/.ssh"
chmod 0700 "$HOME/.ssh"
SSH_AUTH_SOCK="$agent_sock" ssh-add -L \
  | grep ' DGX Spark Admin$' > "$HOME/.ssh/dgx_spark_admin.pub"
chmod 0644 "$HOME/.ssh/dgx_spark_admin.pub"
ssh-keygen -lf "$HOME/.ssh/dgx_spark_admin.pub"
```

The fingerprint must be:

```text
SHA256:66yS2tf5iK+wvPkO44m++PWfI1q1BHS63BRMJqsPaqM
```

The local file is intentionally a public key. There must be no corresponding
unencrypted private-key file at `~/.ssh/dgx_spark_admin`.

## 2. Install the public key

Install the public key on one Spark at a time:

```bash
ssh-copy-id -i "$HOME/.ssh/dgx_spark_admin.pub" carst@192.168.1.211
ssh-copy-id -i "$HOME/.ssh/dgx_spark_admin.pub" carst@192.168.1.212
```

Each command asks for the Linux account password once. Enter it only into the
local terminal prompt; never put it in a command, file, chat, log, or shell
history. Stop if either command reports that no key was added.

Password authentication remains enabled until fresh key-only sessions have
passed on both nodes. The later SSH-hardening task disables it.

## 3. Install the aliases

Copy `config/ssh/dgx-spark.conf.example` to
`~/.ssh/config.d/dgx-spark.conf`, set its mode to `0600`, and ensure the
following directive appears near the top of `~/.ssh/config`, before any
catch-all `Host *` block:

```sshconfig
Include ~/.ssh/config.d/*.conf
```

Inspect the resolved settings without connecting:

```bash
ssh -G dgx-spark-1 | grep -E '^(hostname|user|identityagent|identityfile) '
ssh -G dgx-spark-2 | grep -E '^(hostname|user|identityagent|identityfile) '
```

## 4. Verify key-only sessions

Close any password-authenticated SSH sessions before this test so it proves a
fresh connection works. Unlock 1Password, then run:

```bash
ssh -o BatchMode=yes dgx-spark-1 hostname
ssh -o BatchMode=yes dgx-spark-2 hostname
```

Both commands must print a hostname and exit with status zero. `BatchMode=yes`
prevents a password fallback, although 1Password may still ask for approval to
use the key.

If a check fails, leave password authentication enabled. Confirm that the
public key is one complete line in `~/.ssh/authorized_keys` on the affected
Spark, correct its ownership and permissions if necessary, and repeat the
key-only check before proceeding.
