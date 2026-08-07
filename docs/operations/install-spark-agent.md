# Install the Vonk Spark agent

Each DGX Spark runs one outbound-only Rust service. The NAS/controller never
opens SSH for routine work and the Spark exposes no Vonk listener. The agent
connects to the controller over mTLS, reports capacity and installed/running
recipes, then claims only operations matching its advertised capabilities.

## Prerequisites

- Ubuntu 24.04 ARM64 on the DGX Spark.
- NVIDIA driver and NVIDIA Container Toolkit with a working CDI device list:
  `nvidia-ctk cdi list` must include `nvidia.com/gpu=all`.
- A route from the Spark to the NAS agent endpoint. The NAS may use its
  Spark-only management-LAN listener; human and inference access remain
  Tailscale-only.
- The controller CA certificate and its independently verified SHA-256 digest.

Do not add the service user to `docker`, `sudo`, or an NVIDIA administration
group. The package runs rootless Podman in a single-UID namespace with
`fuse-overlayfs`, `slirp4netns`, NVIDIA CDI devices, and an allow-listed
InfiniBand device class for multi-node recipes. Vonk images must run as root
inside that namespace; this maps only to the unprivileged `vonk-agent` account
on the host. Images declaring another OCI user are rejected before install.

## Install

Install the archive key and apt source as described in
[agent package release operations](agent-package-release.md#consumer-installation),
then run:

```bash
sudo apt update
sudo apt install vonk-forge-agent
```

The maintainer script creates the unprivileged account, single-UID rootless
container storage, signed A/B slots, and disabled network state. It performs no
download, pairing, or service start. The single-UID boundary deliberately keeps
`NoNewPrivileges=yes`; no setuid `newuidmap`/`newgidmap` helper is available to
the long-running agent. Because `vonk-agent` is a package-dedicated account,
installation removes only that account's `/etc/subuid` and `/etc/subgid`
mappings if an earlier prerelease or host policy created them.

Copy the CA and edit the four bootstrap values:

```bash
sudo install -o root -g vonk-agent -m 0640 controller-ca.pem \
  /etc/vonk-forge/controller-ca.pem
sudoedit /etc/vonk-forge/agent.toml
```

Set `controller_url`, `ca_sha256`, and the controller-created `node_id`.
Keep `data_dir` at `/var/lib/vonk-forge/agent`.

In the admin interface, create a new-node pairing grant for that node. Supply
the one-use token through standard input so it never appears in shell history:

```bash
sudo -u vonk-agent -- \
  /var/lib/vonk-forge/supervisor/current/vonk-agent pair \
  --controller https://agents.example.internal/ \
  --ca-sha256 REPLACE_WITH_64_LOWERCASE_HEX \
  --token-stdin < /run/secrets/vonk-enrollment-token
```

Approve the displayed enrollment in the admin interface, then repeat the
exact command once to collect the issued certificate. Delete the token file.

## Validate and start

```bash
sudo -u vonk-agent env \
  HOME=/var/lib/vonk-forge/agent \
  XDG_DATA_HOME=/var/lib/vonk-forge/agent \
  XDG_RUNTIME_DIR=/run/vonk-forge-agent \
  CONTAINERS_STORAGE_CONF=/etc/vonk-forge/containers-storage.conf \
  podman info
sudo systemctl enable --now vonk-agent-helper.socket
sudo systemctl enable --now vonk-agent-supervisor.service
sudo systemctl status vonk-agent.service vonk-agent-supervisor.service
```

The controller must show `Rust agent`, migration `complete`, protocol 3, the
signed runtime identity, inventory, and only the four recipe capabilities.
Install/start admission remains controller-side: disk is checked before image
and weight installation, RAM/VRAM and current workloads before start, and all
participants/fabric links before a multi-node start.

## Boundary checks

```bash
sudo -u vonk-agent podman ps
sudo ss -lntup
systemd-analyze security vonk-agent.service
```

The agent must have no listening TCP socket. `podman ps` must work without a
Docker socket. A recipe container receives only declared mounts, limits,
network mode, and NVIDIA CDI devices; the image is always pulled and inspected
by immutable digest.
