# Operate the AI devbox

The AI devbox is a persistent Ubuntu SSH environment inside the main NAS
Compose project. It publishes no host port and is reachable only through the
raw TCP `svc:ai-devbox` Tailscale Service. Sessions always run as `ai-dev`; the
root-started OpenSSH monitor exists only to authenticate and create that
unprivileged session.

## Prepare persistent state and the public key

The default numeric identity is UID/GID 1100. On the NAS:

```bash
install -d -o 1100 -g 1100 -m 0700 \
  /srv/dgx-forge/ai-devbox/home \
  /srv/dgx-forge/ai-devbox/workspaces \
  /srv/dgx-forge/ai-devbox/cache
install -d -o root -g root -m 0700 \
  /srv/dgx-forge/ai-devbox/ssh-host-keys \
  /srv/dgx-forge/secrets
install -o root -g root -m 0600 /dev/null \
  /srv/dgx-forge/secrets/ai-devbox-authorized-keys
```

On the Mac, display the public half only:

```bash
cat ~/.ssh/id_ed25519.pub
```

Copy that complete `ssh-ed25519` line into
`/srv/dgx-forge/secrets/ai-devbox-authorized-keys` on the NAS. Multiple public
keys are allowed, one per line. Never copy `~/.ssh/id_ed25519`, an agent token,
or a Tailscale credential into that file. Startup rejects missing, empty,
symbolic-link, oversized, or malformed input.

Set these `.env` values:

```text
AI_DEVBOX_UID=1100
AI_DEVBOX_GID=1100
AI_DEVBOX_DATA_ROOT=/srv/dgx-forge/ai-devbox
AI_DEVBOX_AUTHORIZED_KEYS_FILE=/srv/dgx-forge/secrets/ai-devbox-authorized-keys
```

Start the normal full project; there is no devbox-only production Compose file:

```bash
cd deploy/compose
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml up -d --build
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml logs ai-devbox
```

## Connect and work

After the Tailscale policy grants your GitHub-backed identity access, connect
to the Service's MagicDNS name:

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 ai-dev@ai-devbox
```

An optional Mac `~/.ssh/config` entry is:

```sshconfig
Host ai-devbox
    HostName ai-devbox
    User ai-dev
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 60
```

Use `tmux new -s main` for sessions that should survive network or laptop
disconnects. A Compose restart or `docker compose down` terminates tmux because
the process lives inside the container; files remain persistent.

Verify the session:

```bash
whoami
id
command -v uv node python rustc
sudo -n true
touch /etc/must-fail
```

Expected results are `ai-dev`, UID/GID 1100, all four tools present, no `sudo`
command, and a read-only-filesystem error under `/etc`. Root, passwords,
keyboard-interactive authentication, agent/TCP/X11 forwarding, tunnels, and
user environment injection are disabled.

To confirm no LAN SSH listener was added:

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml config --format json \
  | jq '.services["ai-devbox"].ports // []'
```

The result must be `[]`. Caddy's `10.0.0.2:8443` Spark backend remains the only
host-published port.

## Keys, rebuilds, and recovery

After changing authorized public keys, recreate the devbox so startup validates
and installs the file:

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml up -d --force-recreate ai-devbox
```

Normal image rebuilds preserve home, workspaces, cache, and SSH host keys:

```bash
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml build --pull ai-devbox
docker compose --env-file .env \
  -f compose.yaml -f compose.step-ca.yaml up -d --force-recreate ai-devbox
```

Include all four directories below `AI_DEVBOX_DATA_ROOT` in encrypted off-host
backups. Home may contain coding-agent credentials and repository tokens, so
treat its backup as secret even when workspace source is public. Preserve
numeric ownership: home/workspaces/cache are 1100:1100; host keys are root-owned
and mode 0600 below a mode 0700 directory.

Restore the four directories before starting the container. Compare the
restored host-key fingerprints with the backup record:

```bash
ssh-keygen -lf /srv/dgx-forge/ai-devbox/ssh-host-keys/ssh_host_ed25519_key.pub
ssh-keygen -lf /srv/dgx-forge/ai-devbox/ssh-host-keys/ssh_host_rsa_key.pub
```

If the host-key directory is lost, startup generates a new identity and every
strict client should report a changed fingerprint. Stop and verify the recovery
event before removing the prior client entry. Never suppress host-key checking
or blindly run `ssh-keygen -R` during unexplained fingerprint changes.
