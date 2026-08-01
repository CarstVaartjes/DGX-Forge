# Direct ConnectX-7 fabric

This runbook configures a two-DGX-Spark, directly cabled ConnectX-7 fabric.
The selected path is the official Spark-side CLI/manual fallback from
`dgx-spark-playbooks` commit `1fb66f059ee427c5a3678b3117ef73aab042b458`.
It deliberately does not use the Mac administration key for node-to-node
access and does not enable SSH agent forwarding. The direct fabric must never
receive a default route.

`inventory/reports/fabric.json` is committed as explicitly-labelled
preconfiguration/staging evidence. Do not populate `inventory/cluster.toml` or
replace its null post-configuration values until the probes below have been
captured.

## Evidence and safety gate

Before applying the manual plan, retain all of the following:

- a photo of the label on the *single* QSFP112 DAC cable, including its part
  number,
- a rear-panel photo proving that one cable joins the same numbered ConnectX-7
  QSFP port on both Sparks, and
- the elevated `ethtool -m` output captured from both ends.

On the current hosts, one physical QSFP connection is exposed as two Linux
interfaces and RDMA HCAs. This is expected on DGX Spark; do **not** treat it as
two cables or select only one function:

| Node | Linux interfaces reported `LOWER_UP` | RDMA HCAs | Observed speed |
| --- | --- | --- | --- |
| Spark 1 | `enp1s0f1np1`, `enP2p1s0f1np1` | `rocep1s0f1`, `roceP2p1s0f1` | 200000 Mb/s each |
| Spark 2 | `enp1s0f1np1`, `enP2p1s0f1np1` | `rocep1s0f1`, `roceP2p1s0f1` | 200000 Mb/s each |

The controller's elevated, read-only `ethtool -m` evidence identifies the
installed cable at both ends as `Amphenol`, OUI `78:a7:14`, vendor PN
`NJAAKK-C106`, revision `B`, serial `APF261610697AC`: a 1 m passive copper,
PAM4 DAC. It agrees at both ends. Amphenol's primary material identifies the
`NJAAKK` family as QSFP 400G, 112G/lane passive DAC, but NVIDIA's current Sync
guide lists `NJAAKK-N911` (not `NJAAKK-C106`) and `Luxshare LMTQF022-SD-R` as
the supported models. Treat `C106` as an undocumented OEM/customer identifier,
not as confirmed supported hardware.

If the evidence must be recaptured, the controller may run this read-only,
elevated probe and attach its output to the change record:

```bash
for host in dgx-spark-1 dgx-spark-2; do
  ssh -o BatchMode=yes -o ForwardAgent=no "$host" \
    'sudo ethtool -m enp1s0f1np1; sudo ethtool -m enP2p1s0f1np1'
done
```

The cable PN remains an undocumented OEM/customer identifier. The selected
manual path may proceed only through the staged preflight below; it requires
both functions to be `UP` at 200000 Mb/s and preserves the management default
route. Any cable or link warning, failed preflight, Netplan error, route
change, or failed postcheck is a hard stop. Do not apply a manual workaround.

## Out-of-scope helpers

NVIDIA Sync/Cluster Assistant is out of scope for this selected Spark-side CLI
rollout. Do not use its generated Netplan, nor run `discover-sparks`: the
current discovery helper copies `~/.ssh/id_ed25519_shared` private material to
every node and appends a `Host * IdentityFile` rule. Both actions violate this
rollout's key separation and password-SSH constraints.

## Selected manual CLI rollout

The official two-Spark playbook assigns the active `f1` function pair to two
point-to-point subnets. The staged plan uses the current Linux MTU/default,
`1500`; no jumbo-MTU value is assumed without primary evidence or a successful
live validation.

| Node | Interface | HCA | Planned IPv4 | MTU |
| --- | --- | --- | --- | --- |
| Spark 1/head | `enp1s0f1np1` | `rocep1s0f1` | `192.168.100.10/24` | 1500 |
| Spark 1/head | `enP2p1s0f1np1` | `roceP2p1s0f1` | `192.168.101.10/24` | 1500 |
| Spark 2/worker | `enp1s0f1np1` | `rocep1s0f1` | `192.168.100.11/24` | 1500 |
| Spark 2/worker | `enP2p1s0f1np1` | `roceP2p1s0f1` | `192.168.101.11/24` | 1500 |

`nodes/bin/configure-direct-fabric` is an audited, idempotent installer. It
only manages `/etc/netplan/99-dgx-spark-direct-fabric.yaml`, refuses to mix
with `99-nvidia-sync-cluster.yaml`, requires both functions to be up at
200000 Mb/s, sets `dhcp4: false` on both fabric interfaces, verifies that the
management default route is not on the fabric, and uses `netplan try` for both
installation and rollback. It has no SSH or private-key handling.

### Stage and inspect the worker first

The following first two commands only stage the reviewed script and run its
read-only preflight. They do not change either Spark. Do not add `-A` or enable
agent forwarding.

```bash
scp -o ForwardAgent=no nodes/bin/configure-direct-fabric \
  dgx-spark-2:/tmp/configure-direct-fabric
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'bash /tmp/configure-direct-fabric --node spark2 --check'
```

If—and only if—the preflight prints a pass result with a Wi-Fi/10 GbE
management default route, the controller/user can make the first approved
change on the worker:

```bash
ssh -t -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'sudo bash /tmp/configure-direct-fabric --node spark2 --apply'
```

Review `netplan try` at the console and accept only if management remains
reachable. If it is not accepted, it automatically rolls back. Do not continue
to Spark 1 if worker application, route preservation, or local validation
fails. Before staging the head, the worker must prove its own addresses, MTU,
RoCEv2 GID-to-netdev binding, and absence of a fabric default route:

```bash
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'sudo bash /tmp/configure-direct-fabric --node spark2 --local-postcheck'
```

After the worker result is recorded, repeat the same staged preflight and
interactive application for the head:

```bash
scp -o ForwardAgent=no nodes/bin/configure-direct-fabric \
  dgx-spark-1:/tmp/configure-direct-fabric
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 \
  'bash /tmp/configure-direct-fabric --node spark1 --check'
ssh -t -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 \
  'sudo bash /tmp/configure-direct-fabric --node spark1 --apply'
```

### Separate head-to-worker cluster key

Only after both Netplan applications and both postchecks pass, generate the
Ed25519 key on Spark 1. The private key never leaves Spark 1; only its public
key is transferred through the controller. This workflow does not copy the Mac
administration key and does not forward an agent.

```bash
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 '
  set -euo pipefail
  key="$HOME/.ssh/dgx_spark_fabric_ed25519"
  test ! -e "$key" && test ! -e "$key.pub"
  umask 077
  ssh-keygen -q -t ed25519 -N "" -f "$key" \
    -C "spark1-to-spark2-fabric"
  cat "$key.pub"
' > /tmp/dgx_spark_fabric_ed25519.pub
```

Install the public key on Spark 2 with the two Spark 1 fabric addresses as
source restriction and OpenSSH's `restrict` option. `restrict` denies agent,
port, X11, and PTY forwarding while still permitting the noninteractive SSH
processes required for cluster work.

```bash
{
  printf 'restrict,from="192.168.100.10,192.168.101.10" '
  cat /tmp/dgx_spark_fabric_ed25519.pub
} | ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 '
  set -euo pipefail
  umask 077
  install -d -m 0700 "$HOME/.ssh"
  cat >> "$HOME/.ssh/authorized_keys"
  chmod 0600 "$HOME/.ssh/authorized_keys"
'
```

On Spark 1, create a narrow fabric-only alias (the user and home path must
match the live account):

```sshconfig
Host dgx-spark-2-fabric
    HostName 192.168.100.11
    User carst
    BindAddress 192.168.100.10
    IdentityFile ~/.ssh/dgx_spark_fabric_ed25519
    IdentitiesOnly yes
    ForwardAgent no
```

Verify from Spark 1 with `ssh -o ForwardAgent=no dgx-spark-2-fabric hostname`.
Do not add `Host *`, do not add the fabric key to an agent, and do not copy its
private component to Spark 2 or the Mac.

### Manual rollback

Run the single reviewed controller sequence from the repository root:

```bash
nodes/bin/rollback-direct-fabric
```

It runs with `set -euo pipefail`, derives the checksum in its own scope,
re-stages `configure-direct-fabric` with `scp -o ForwardAgent=no`, compares
each remote `sha256sum`, and transfers no key material. Spark 2 is a hard
gate: a failed transfer, checksum, worker rollback, or management reconnect
exits before Spark 1 is staged or touched. Only after the worker reconnects
over the management alias does it stage, verify, roll back, and reconnect to
Spark 1.

The rollback retains the managed Netplan file under
`/root/dgx-spark-fabric-rollback/` and uses `netplan try`; it does not remove
the head-only SSH key, which must be removed separately only if the cluster
relationship is intentionally dismantled. After a deliberately completed
rollback, verify both nodes over management and remove the two temporary
`/tmp/configure-direct-fabric` copies.

## Post-success collection and acceptance

### Verified live result

The following runtime state was captured read-only at `2026-08-01T22:34:12Z`.
Spark 2 was applied and locally validated before Spark 1. Both nodes retain the
management default route through `wlP9s9`; neither fabric interface has a
default route.

| Node | Rail | Interface | Fabric IPv4 | HCA | RoCEv2 GID | MTU | Link rate |
| --- | --- | --- | --- | --- | ---: | ---: | ---: |
| Spark 1/head | 100 | `enp1s0f1np1` | `192.168.100.10/24` | `rocep1s0f1` | 3 | 1500 | 200000 Mb/s |
| Spark 1/head | 101 | `enP2p1s0f1np1` | `192.168.101.10/24` | `roceP2p1s0f1` | 3 | 1500 | 200000 Mb/s |
| Spark 2/worker | 100 | `enp1s0f1np1` | `192.168.100.11/24` | `rocep1s0f1` | 3 | 1500 | 200000 Mb/s |
| Spark 2/worker | 101 | `enP2p1s0f1np1` | `192.168.101.11/24` | `roceP2p1s0f1` | 3 | 1500 | 200000 Mb/s |

Each recorded GID is IPv4-mapped, type `RoCE v2`, and has `gid_attrs/ndevs`
bound to the interface in the table. At `2026-08-01T22:34:27Z`, normal and
non-fragmenting `-M do -s 1472` pings succeeded 3/3 with zero loss in both
directions on both rails.

Use these exact values for distributed consumers on both nodes:

```bash
export NCCL_SOCKET_IFNAME='=enp1s0f1np1,enP2p1s0f1np1'
export NCCL_IB_HCA='=rocep1s0f1:1,roceP2p1s0f1:1'
export NCCL_IB_GID_INDEX=3
export TP_SOCKET_IFNAME='enp1s0f1np1,enP2p1s0f1np1'
export GLOO_SOCKET_IFNAME='enp1s0f1np1,enP2p1s0f1np1'
```

The head-only fabric key fingerprint is
`SHA256:xAsqCZnOIq34EVQR2O5+z+qaLlXFIdT7Qp9wreg4rfg`. The worker entry uses
`restrict,from="192.168.100.10,192.168.101.10"`; the private key is not on the
worker or Mac. `dgx-spark-2-fabric` binds `192.168.100.10`, disables password,
keyboard-interactive, and agent forwarding, uses strict host checking, and
returned `spark-2297`. The verified worker host Ed25519 fingerprint is
`SHA256:Q/0cf26vxC6Z+xH6pfB5uoGNXfIEum6KOFVhnl4nngg`.

The controller must capture the following output from both nodes after the
manual configuration completes. It is read-only except for the already-approved
manual operation.

```bash
for host in dgx-spark-1 dgx-spark-2; do
  ssh -o BatchMode=yes -o ForwardAgent=no "$host" '
    set -euo pipefail
    sudo cat /etc/netplan/99-dgx-spark-direct-fabric.yaml
    ip -br link
    ip -br addr
    ip route
    rdma link show
    for d in /sys/class/infiniband/*; do
      for p in "$d"/ports/*; do
        [ -d "$p" ] || continue
        for g in "$p"/gids/[0-9]*; do
          [ -e "$g" ] || continue
          i=${g##*/}
          printf "%s/%s gid[%s]=%s type=%s netdev=%s\\n" \
            "${d##*/}" "${p##*/}" "$i" "$(cat "$g")" \
            "$(cat "$p/gid_attrs/types/$i" 2>/dev/null || true)" \
            "$(cat "$p/gid_attrs/ndevs/$i" 2>/dev/null || true)"
        done
      done
    done
  '
done
```

Run the script's exact bidirectional check from each node after both plans are
accepted; it validates route selection, no fabric default route, IPv4-to-RoCEv2
GID mapping, normal ping, and a non-fragmenting MTU-sized ping.

```bash
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'sudo bash /tmp/configure-direct-fabric --node spark2 --postcheck'
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 \
  'sudo bash /tmp/configure-direct-fabric --node spark1 --postcheck'
```

For every configured interface pair, verify the address listed in Netplan maps
to a non-link-local `RoCE v2` GID for that interface/HCA. Record **both**
interface/HCA/GID combinations: a physical DGX Spark QSFP link has two
functions, so a single `fabric_ip`, `interface`, `hca`, and `gid_index` field
is insufficient until the inventory model is extended or explicitly represents
both consumers.

Run this from each node for each corresponding peer address and interface,
substituting only values captured above. The IPv4 non-fragmenting payload is
the MTU minus the 20-byte IPv4 and 8-byte ICMP headers.

```bash
iface='<configured-interface>'
peer='<corresponding-peer-fabric-ip>'
mtu="$(cat "/sys/class/net/$iface/mtu")"
ping -I "$iface" -c 3 "$peer"
ping -I "$iface" -M do -s "$((mtu - 28))" -c 3 "$peer"
test -z "$(ip route show default dev "$iface")"
```

The verified values above are recorded in `inventory/cluster.toml` and
`inventory/reports/fabric.json`. Do not replace them using the management LAN
or link-local GIDs.
